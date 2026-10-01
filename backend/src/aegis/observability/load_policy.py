"""压测/容量量测的阈值口径：与 SLA 告警规则同源，报告与告警不可能各说一套。

为什么住在产品包里而不是 `tests/load/locustfile.py` 里：locust 一被 import 就会做
gevent monkey-patch，测试进程里 import 它会在 ssl/anyio 已加载后炸出 RecursionError。
于是判定逻辑留在这里（可被单测直接覆盖），压测档案只做"打端点 + 上报结果"的驱动层。
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from aegis.config import Settings, get_settings


def read_budget_ms(settings: Settings | None = None) -> float:
    """只读端点预算：调度响应阈值的双倍，且不低于 1s（大屏轮询不应比链路更松）。"""
    current = settings or get_settings()
    return max(float(current.sla_schedule_ms) * 2.0, 1_000.0)


def drill_budget_ms(settings: Settings | None = None) -> float:
    """一次演练跑完感知→研判→规划→执行→反馈：预算取同步与调度两条阈值之和。"""
    current = settings or get_settings()
    return float(current.sla_sync_ms) + float(current.sla_schedule_ms)


def elapsed_ms(response: Any) -> float:
    """响应耗时的毫秒数。

    locust 2.46 底下层是 httpx，`response.elapsed` 是 `timedelta`：`float()` 会抛
    `TypeError`，而 locust 把任务异常记成 task error 之后汇总仍是
    "0 requests、0.00% 失败、退出码 0"——一次什么都没打的压测看起来是绿的。
    """
    elapsed = response.elapsed
    if isinstance(elapsed, timedelta):
        return elapsed.total_seconds() * 1000.0
    # 替身响应可能直接给秒数：口径仍是"秒 → 毫秒"
    return float(elapsed) * 1000.0


def over_budget(name: str, took_ms: float, budget_ms: float) -> str | None:
    """阈值判定的唯一口径：等于预算算合格（考核写的是"≤"，不是"<"）。"""
    return None if took_ms <= budget_ms else f"{name} 超阈值: {took_ms:.0f}ms > {budget_ms:.0f}ms"


__all__ = ["drill_budget_ms", "elapsed_ms", "over_budget", "read_budget_ms"]
