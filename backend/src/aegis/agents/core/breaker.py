"""LLM 熔断：连续失败达阈值 → 冷却窗口内跳过调用 → 窗口到期放行一次试探。

## 为什么是共享内核（而不是两侧各一份）

平台网关与灾种模块此前是**两份互不依赖的熔断**，语义还不同：

- 平台网关（`services/llm/llm_gateway.py`）：按模型名分账，一开闸就**永久**跳过——
  `_open[model]=True` 之后该模型永不入候选，`_note_success` 因此不可达，**上游恢复了
  也永远用不上**。
- 灾种模块（`agents/hazards/llm_breaker.py`）：带冷却窗口与半开试探。

同一个"上游挂了"的判断，两侧给出两种行为，这是"两套系统"最直接的证据。现在只有
这一份实现。

## 它解决什么问题（2026-11-10 实测）

剖析灾种研判链路时量到：`openai/_base_client.py::_sleep_for_retry` 的指数退避
`time.sleep` 占掉 10 轮共 32.5s 里的 **26.3s（81%）**，其余是 6.2s 的 SSL 读。
根因是 SDK 默认 `max_retries=2`：上游返回 HTTP 500 时，SDK 会**盲重试**两次
（0.5s、1.0s 退避），一次逻辑调用变成三次 HTTP 往返。

盲重试本身不是错——它救的是"偶发抖动"。错的是：**上游持续不可用时，每一次调用
仍然要付满三次往返**，链路吞吐被拖垮，而节点早就有确定性兜底在等着。

所以熔断接管这个判断：失败连续达阈值 → 打开；冷却窗口内**直接跳过 LLM**，
让节点立刻走确定性兜底；窗口到期放行一次试探，成功即恢复。

## 边界

- **只管"能不能调"，不管"调得对不对"**：模型返回了非法 JSON 属于语义失败，
  不计入熔断（那该由提示词与解析层修，不是熔断能修的）。4xx（除 429）同理——
  请求本身错了（参数、鉴权、路径），重试没有意义，却会让模型被下架。
- **半开只放行一次试探**：不是彻底封死，否则恢复不了；也不是无限放行，否则恢复瞬间
  会被打穿。
- **状态是进程内的**：多副本各自计数。两侧原本也都是进程内计数，同一取舍。

## 按键分账

一个实例可以同时给多个键（模型名 / 端点+凭据）各记一账：平台按模型名分，
灾种按端点+凭据分。不传 `key` 时用默认键（单键场景的简写）。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from aegis.errors import AegisError, ErrorCode

State = Literal["closed", "open", "half_open"]

#: 默认键：单键场景（灾种模块：一条客户端一条账）的简写。
DEFAULT_KEY = ""


class LlmUnavailableError(AegisError):
    """LLM 当前不可用（熔断打开）：调用方应当走确定性兜底，而不是等它。"""

    code = ErrorCode.NOT_READY


@dataclass(slots=True)
class _Entry:
    """一个键的熔断账。"""

    failures: int = 0
    state: State = "closed"
    opened_at: float = 0.0
    probe_in_flight: bool = False


class LlmBreaker:
    """连续失败计数 + 冷却窗口的熔断状态机（可按键分账）。

    线程安全：研判在专用线程池里跑，而路由与装配在主线程，计数会被两边同时碰。

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
        self._entries: dict[str, _Entry] = {}
        self._lock = threading.Lock()

    # ---------- 只读视图 ----------

    def _entry(self, key: str) -> _Entry:
        return self._entries.setdefault(key, _Entry())

    @property
    def failures(self) -> int:
        """默认键上的连续失败计数。"""
        with self._lock:
            return self._entry(DEFAULT_KEY).failures

    def failures_for(self, key: str) -> int:
        with self._lock:
            return self._entry(key).failures

    def state(self, key: str = DEFAULT_KEY) -> State:
        """当前状态（纯读，不改状态机）。"""
        with self._lock:
            return self._entry(key).state

    # ---------- 决策与记账 ----------

    def available(self, key: str = DEFAULT_KEY) -> bool:
        """此刻**是否可能**可以发起调用（纯查询，不改状态、不占名额）。

        平台网关要先筛候选再逐个尝试，若用 `allow()` 筛选就会在筛选阶段消耗掉试探
        名额（`available` 与 `acquire` 拆开就是为了这个）。
        """
        moment = self._clock()
        with self._lock:
            entry = self._entry(key)
            if entry.state == "closed":
                return True
            if entry.state == "open":
                return moment - entry.opened_at >= self._cooldown
            # half_open：试探名额还空着才可能
            return not entry.probe_in_flight

    def acquire(self, key: str = DEFAULT_KEY) -> bool:
        """占用一次调用名额。

        - `closed`：放行。
        - `open`：冷却窗口内**拒绝**（调用方应立即走确定性兜底，不该等它）；
          窗口已过 → 转半开并占用**唯一**那个试探名额。
        - `half_open`：名额已被占用 → 拒绝（恢复瞬间不该被打穿）。
        """
        moment = self._clock()
        with self._lock:
            entry = self._entry(key)
            if entry.state == "closed":
                return True
            if entry.state == "open":
                if moment - entry.opened_at < self._cooldown:
                    return False
                entry.state = "half_open"
                entry.probe_in_flight = True
                return True
            if entry.probe_in_flight:
                return False
            entry.probe_in_flight = True
            return True

    def allow(self, key: str = DEFAULT_KEY) -> bool:
        """`acquire` 的别名——单键场景的惯用叫法。"""
        return self.acquire(key)

    def note_failure(self, key: str = DEFAULT_KEY) -> None:
        """记一次失败。

        - `closed` 且未达阈值 → 只累加计数。
        - 达阈值 → 打开熔断，并**从此刻起**算冷却窗口。
        - `half_open`（试探失败）→ 重新打开，冷却窗口重新计时。
        """
        moment = self._clock()
        with self._lock:
            entry = self._entry(key)
            entry.failures += 1
            entry.probe_in_flight = False
            if entry.state == "half_open" or entry.failures >= self._threshold:
                entry.state = "open"
                entry.opened_at = moment

    def note_success(self, key: str = DEFAULT_KEY) -> None:
        """记一次成功：计数归零、熔断关闭。"""
        with self._lock:
            entry = self._entry(key)
            entry.failures = 0
            entry.state = "closed"
            entry.opened_at = 0.0
            entry.probe_in_flight = False

    def reset(self, key: str = DEFAULT_KEY) -> None:
        """手动复位（测试与运维用）。"""
        self.note_success(key)


def is_transport_failure(exc: BaseException) -> bool:
    """这次失败该不该计入熔断？

    只认"服务不可用"这一类：连接错误、超时、5xx、429（限流）——这些是"等一会儿可能
    会好"的失败，值得熔断后冷却再试。

    **不计入**的两类：
    - 4xx（429 除外）：请求本身错了（参数、鉴权、路径），重试没有意义，却会让模型
      被下架——把"调用方写错了"变成"服务不可用"是最贵的误判。
    - 语义错误（模型返回了非法 JSON）：那是提示词与解析层要修的，不是熔断能修的。

    平台网关此前把**一切** provider 异常都计入熔断；灾种模块只认上面这一类。统一到
    后者（ADR-0017）。
    """
    import openai

    if isinstance(exc, openai.APIConnectionError):
        # APITimeoutError 是 APIConnectionError 的子类，一并覆盖。
        return True
    if isinstance(exc, openai.APIStatusError):
        status = getattr(exc, "status_code", None)
        return status == 429 or (status is not None and int(status) >= 500)
    return bool(isinstance(exc, TimeoutError))


__all__ = ["DEFAULT_KEY", "LlmBreaker", "LlmUnavailableError", "State", "is_transport_failure"]
