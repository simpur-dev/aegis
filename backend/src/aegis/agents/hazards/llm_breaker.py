"""外部实现的 LLM 熔断：连续失败达阈值后，冷却窗口内直接跳过 LLM 调用。

## 它解决什么问题（2026-11-10 实测）

剖析 ext 研判链路时量到：`openai/_base_client.py::_sleep_for_retry` 的指数退避
`time.sleep` 占掉 10 轮共 32.5s 里的 **26.3s（81%）**，其余是 6.2s 的 SSL 读。
根因是 SDK 默认 `max_retries=2`：上游返回 HTTP 500 时，SDK 会**盲重试**两次
（0.5s、1.0s 退避），一次逻辑调用变成三次 HTTP 往返。

盲重试本身不是错——它救的是"偶发抖动"。错的是：**上游持续不可用时，每一次调用
仍然要付满三次往返**，链路吞吐被拖垮，而节点早就有确定性兜底在等着。

所以这里加一层熔断：失败连续达阈值 → 打开；冷却窗口内**直接跳过 LLM**，
让节点立刻走确定性兜底；窗口到期放行一次试探，成功即恢复。

## 为什么不能并进核心、什么时候可以移除

- **为什么不能并进核心**：熔断是"跨切关注点"（失败计数、冷却窗口、试探名额），
  与"LLM 调用"是两件事。它独立成文件后是**纯逻辑、无 IO、可直接单测**的；
  并进 `llm.py` 会把一条状态机埋进一个已经很大的兼容层文件里。
- **什么时候可以移除**：一旦外部实现的 LLM 改走平台网关
  （`aegis/services/llm/llm_gateway.py` 已有自己的按模型熔断账），本文件即可整体删除。
  在那之前，本模块与平台网关是**两份互不依赖的熔断**（外部模块不得依赖平台内部实现，
  见 `tests/unit/test_agents_ext_boundaries.py`）。

## 与平台网关熔断的差别（不是重复实现）

平台网关的熔断**没有恢复窗口**：模型失败达阈值就永久跳过它，靠 fallback 模型接力。
外部实现**只有一个模型**，照抄那条口径会让熔断变成永久失效——上游恢复了也永远用不上。
所以这里带冷却窗口：`closed →（连续失败达阈值）→ open →（窗口到期）→ half_open
→（试探成功）→ closed`。

## 边界

- **只管"能不能调"，不管"调得对不对"**：模型返回了非法 JSON 属于语义失败，
  不计入熔断（那该由提示词与解析层修，不是熔断能修的）。
- **半开只放行一次试探**：不是彻底封死，否则恢复不了；也不是无限放行，否则恢复瞬间会被打穿。
- **状态是进程内的**：多副本各自计数。平台的网关熔断也是进程内的，同一取舍。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Literal

State = Literal["closed", "open", "half_open"]


class LlmUnavailableError(RuntimeError):
    """LLM 当前不可用（熔断打开）：调用方应当走确定性兜底，而不是等它。"""


class LlmBreaker:
    """连续失败计数 + 冷却窗口的熔断状态机。

    线程安全：研判在专用线程池里跑（`agents_ext/risk_agent/agent.py`），
    而路由与装配在主线程，计数会被两边同时碰。

    `clock` 可注入：冷却窗口是时间相关的状态机，边界（"窗口刚到期" /
    "窗口没过半"）必须能**确定性地**测出来，不能靠 sleep 赌。
    默认用 `time.monotonic`（单调钟，不受系统时间调整影响）。
    """

    def __init__(
        self,
        *,
        threshold: int = 3,
        cooldown_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._threshold = max(1, int(threshold))
        self._cooldown = max(0.0, float(cooldown_seconds))
        self._clock = clock
        self._failures = 0
        self._state: State = "closed"
        self._opened_at = 0.0
        self._probe_in_flight = False
        self._lock = threading.Lock()

    # ---------- 只读视图 ----------

    @property
    def threshold(self) -> int:
        return self._threshold

    @property
    def cooldown_seconds(self) -> float:
        return self._cooldown

    @property
    def failures(self) -> int:
        with self._lock:
            return self._failures

    def state(self) -> State:
        """当前状态（纯读，不改状态机）。"""
        with self._lock:
            return self._state

    # ---------- 决策与记账 ----------

    def allow(self) -> bool:
        """此刻该不该发起 LLM 调用。

        - `closed`：放行。
        - `open`：冷却窗口内**拒绝**（调用方应立即走确定性兜底，不该等它）。
        - `half_open`（窗口已到期）：放行**一次**试探；并发调用里只有第一个拿到名额，
          其余继续拒绝——恢复瞬间不该被打穿。
        """
        moment = self._clock()
        with self._lock:
            if self._state == "closed":
                return True
            if self._state == "open":
                if moment - self._opened_at < self._cooldown:
                    return False
                # 冷却窗口到期：转半开，本次调用占用唯一那个试探名额。
                self._state = "half_open"
                self._probe_in_flight = True
                return True
            # half_open：试探名额已被占用 → 继续拒绝。
            if self._probe_in_flight:
                return False
            self._probe_in_flight = True
            return True

    def note_failure(self) -> None:
        """记一次失败。

        - `closed` 且未达阈值 → 只累加计数。
        - 达阈值 → 打开熔断，并**从此刻起**算冷却窗口。
        - `half_open`（试探失败）→ 重新打开，冷却窗口重新计时。
        """
        moment = self._clock()
        with self._lock:
            self._failures += 1
            self._probe_in_flight = False
            if self._state == "half_open" or self._failures >= self._threshold:
                self._state = "open"
                self._opened_at = moment

    def note_success(self) -> None:
        """记一次成功：计数归零、熔断关闭。"""
        with self._lock:
            self._failures = 0
            self._state = "closed"
            self._opened_at = 0.0
            self._probe_in_flight = False

    def reset(self) -> None:
        """手动复位（测试与运维用）。"""
        self.note_success()


__all__ = ["LlmBreaker", "LlmUnavailableError", "State"]
