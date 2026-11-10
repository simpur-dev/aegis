"""灾种模块的专用阻塞线程池。

研判/仿真/预警三条腿都会调 LLM 或跑图，单次占用 11–20s。默认的 `asyncio.to_thread`
用的是全进程共享执行器：真模型一次研判占一个槽，会和检索/分析旁路的 to_thread 抢
线程，结果是"谁的活都变慢"，而且慢的原因不在自己身上（见 `risk_agent/agent.py`
关于专用池的那段说明）。

池是**模块级共享**的：六个本体各开一个池等于把线程数乘六，而它们本来就在同一条
闭环上串行跑，谁也不会跟谁并发。共享池的容量由部署键 `AEGIS_HAZARD_POOL_WORKERS`
决定。

**关闭由装配层负责**（`shutdown_hazard_pool`），本体自己不关共享资源——某个本体
先停下来就把池关掉，会连累还在跑的兄弟。
"""

from __future__ import annotations

import asyncio
import functools
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from typing import Any, TypeVar

_T = TypeVar("_T")

_POOL: ThreadPoolExecutor | None = None
_POOL_SIZE = 8


def configure_pool(size: int) -> None:
    """设定共享池容量（装配层调用，必须在第一次用池之前）。

    容量 0 直接拒绝：起不来的池会在第一条请求上才暴露，而那时候看上去像是"研判超时"。
    容量变了就重建池——已经建好的池不会因为改了配置就自己变宽，那样"配了没生效"。
    """
    global _POOL, _POOL_SIZE
    value = int(size)
    if value < 1:
        raise ValueError(f"共享池容量必须 ≥1，收到 {value}")
    if _POOL is not None and value != _POOL_SIZE:
        stale, _POOL = _POOL, None
        stale.shutdown(wait=False, cancel_futures=True)
    _POOL_SIZE = value


def hazard_pool() -> ThreadPoolExecutor:
    """共享线程池；容量由 `configure_pool` 决定，默认 8。"""
    global _POOL
    if _POOL is None:
        _POOL = ThreadPoolExecutor(max_workers=_POOL_SIZE, thread_name_prefix="aegis-hazard")
    return _POOL


def shutdown_hazard_pool() -> None:
    """关闭共享池（装配层在停机时调用）。"""
    global _POOL
    pool, _POOL = _POOL, None
    if pool is not None:
        pool.shutdown(wait=False, cancel_futures=True)


async def run_blocking(func: Any, /, *args: Any, **kwargs: Any) -> Any:
    """在共享池里跑同步函数，并像 `asyncio.to_thread` 一样把请求上下文带进线程。

    同步图/LLM 调用在事件循环里跑会堵死整个环——总线上的其他事务都会被拖住。
    """
    loop = asyncio.get_running_loop()
    ctx = copy_context()
    return await loop.run_in_executor(hazard_pool(), functools.partial(ctx.run, func, *args, **kwargs))


__all__ = ["configure_pool", "hazard_pool", "run_blocking", "shutdown_hazard_pool"]
