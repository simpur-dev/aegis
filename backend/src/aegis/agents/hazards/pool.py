"""灾种模块的阻塞线程池：一条**共享腿池** + 本体自带的**专用执行器**。

【为什么腿共享一条池】
研判/仿真/预警/审计四条腿都会调 LLM 或跑图，单次占用 11–20s。默认的 `asyncio.to_thread`
用的是全进程共享执行器：真模型一次占一个槽，会和检索/分析旁路的 to_thread 抢线程，
结果是"谁的活都变慢"，而且慢的原因不在自己身上。六个腿本体各开一个池等于把线程数
乘六，而它们本来就在同一条闭环上串行跑——共享池的容量由部署键
`AEGIS_HAZARD_POOL_WORKERS` 决定。

【为什么闭环编排不在共享池里跑】
闭环（`HazardWorkflowAgent`）要占一个线程**等**五条腿回来，而每条腿都要向池要线程。
编排器若也用共享池，池容量 1 时它占住唯一的 worker、腿永远排队——闭环 100% 全腿
超时；并发闭环数达到池容量时整批自饿。所以编排器自带**专用执行器**（容量独立配置），
与腿池物理隔离：编排器占多少个线程，腿池都还有完整的容量接腿。

**共享池的关闭由装配层负责**（`shutdown_hazard_pool`），腿本体自己不关共享资源——
某个本体先停下来就把池关掉，会连累还在跑的兄弟；专用执行器随本体停机关（谁建谁关）。
"""

from __future__ import annotations

import asyncio
import functools
import threading
from concurrent.futures import Executor, ThreadPoolExecutor
from contextvars import copy_context
from typing import Any

#: 共享腿池。`_POOL_LOCK` 守护创建/换容/关闭三件事：
#: 不加锁时 `configure_pool` 的"置 None"与另一线程的"按旧容量建新池"会交错，
#: 产生一个容量错配的池并被永久沿用；首次并发的两个 `hazard_pool()` 还会各建一个池。
#: 默认容量。测试会改小池做隔离验证，结束后恢复到这个值——池容量是模块级全局，
#: 不恢复的话排在后面的用例会继承别人的小池。
DEFAULT_POOL_SIZE = 8

_POOL: ThreadPoolExecutor | None = None
_POOL_SIZE = DEFAULT_POOL_SIZE
_POOL_LOCK = threading.Lock()


def configure_pool(size: int) -> None:
    """设定共享腿池容量（装配层调用）。

    容量 0 直接拒绝：起不来的池会在第一条请求上才暴露，而那时候看上去像是"研判超时"。
    容量变了就重建池——已经建好的池不会因为改了配置就自己变宽，那样"配了没生效"。
    """
    global _POOL, _POOL_SIZE
    value = int(size)
    if value < 1:
        raise ValueError(f"共享池容量必须 ≥1，收到 {value}")
    with _POOL_LOCK:
        if _POOL is not None and value != _POOL_SIZE:
            stale, _POOL = _POOL, None
            stale.shutdown(wait=False, cancel_futures=True)
        _POOL_SIZE = value


def hazard_pool() -> ThreadPoolExecutor:
    """共享腿池；容量由 `configure_pool` 决定，默认 8。"""
    global _POOL
    with _POOL_LOCK:
        if _POOL is None:
            _POOL = ThreadPoolExecutor(max_workers=_POOL_SIZE, thread_name_prefix="aegis-hazard")
        return _POOL


def shutdown_hazard_pool() -> None:
    """关闭共享腿池（装配层在停机时调用）。"""
    global _POOL
    with _POOL_LOCK:
        pool, _POOL = _POOL, None
    if pool is not None:
        pool.shutdown(wait=False, cancel_futures=True)


async def run_blocking(func: Any, /, *args: Any, **kwargs: Any) -> Any:
    """在**共享腿池**里跑同步函数，并像 `asyncio.to_thread` 一样把请求上下文带进线程。

    同步图/LLM 调用在事件循环里跑会堵死整个环——总线上的其他事务都会被拖住。
    """
    return await run_blocking_on(hazard_pool(), func, *args, **kwargs)


async def run_blocking_on(executor: Executor, func: Any, /, *args: Any, **kwargs: Any) -> Any:
    """在**指定执行器**里跑同步函数（上下文随行）。

    闭环编排用这条：它的执行器是本体自带的专用池，与腿池隔离（见模块 docstring）。
    """
    loop = asyncio.get_running_loop()
    ctx = copy_context()
    return await loop.run_in_executor(executor, functools.partial(ctx.run, func, *args, **kwargs))


__all__ = [
    "DEFAULT_POOL_SIZE",
    "configure_pool",
    "hazard_pool",
    "run_blocking",
    "run_blocking_on",
    "shutdown_hazard_pool",
]
