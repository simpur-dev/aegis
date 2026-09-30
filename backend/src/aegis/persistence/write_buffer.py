"""有界异步写缓冲：热路径只入队，落库由后台刷写任务承担。

弱网/边缘自治的硬约束在这里落地三件事：

1. `submit()` 是同步的且永不抛：入队不产生 await 点，因此既不会被库不可达拖住，
   也不会在中途被其他协程插入 —— 溢出判定是原子的。
2. 溢出策略显式为 drop-oldest：牺牲最旧的遥测行，而不是最新到达的告警事实。
3. 库不可达时按指数退避 + 抖动重试；退避期间批次留在队首，既不漏数据也不放大并发。

本模块不含任何 SQL：它只搬运 `WriteRequest`，如何落库由 `postgres.py` 注入的 sink 决定。
"""

from __future__ import annotations

import asyncio
import contextlib
import random
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from aegis.persistence.errors import WriteFailedError, describe

DEFAULT_CAPACITY_ROWS = 20_000
DEFAULT_BATCH_ROWS = 500
DEFAULT_FLUSH_INTERVAL_MS = 250.0
DEFAULT_BACKOFF_BASE_MS = 100.0
DEFAULT_BACKOFF_CAP_MS = 8_000.0
DEFAULT_BACKOFF_FACTOR = 2.0
DEFAULT_JITTER_RATIO = 0.25


@dataclass(frozen=True, slots=True)
class WriteRequest:
    """一次逻辑写出：kind 只是可观测标签，rows 的内容由调用方定义（本模块从不解释）。"""

    kind: str
    rows: tuple[Any, ...]

    @property
    def size(self) -> int:
        return len(self.rows)


Sink = Callable[[list[WriteRequest]], Awaitable[None]]


@dataclass(slots=True)
class BufferCounters:
    """计数口径统一为"行"；`retries` 是 sink 失败批次次数。"""

    buffered: int = 0
    flushed: int = 0
    dropped: int = 0
    retries: int = 0
    last_error: str | None = field(default=None, repr=False)

    def as_dict(self) -> dict[str, object]:
        return {
            "buffered": self.buffered,
            "flushed": self.flushed,
            "dropped": self.dropped,
            "retries": self.retries,
            "last_error": self.last_error,
        }


def backoff_delay_ms(
    attempt: int,
    *,
    base_ms: float = DEFAULT_BACKOFF_BASE_MS,
    factor: float = DEFAULT_BACKOFF_FACTOR,
    cap_ms: float = DEFAULT_BACKOFF_CAP_MS,
    jitter_ratio: float = DEFAULT_JITTER_RATIO,
    rand: Callable[[], float] = random.random,
) -> float:
    """第 attempt 次失败后应等待的毫秒数：指数增长封顶，再叠加 ±jitter_ratio 抖动。

    逐次翻倍而非幂运算：attempt 来自持续增长的重试计数，`factor ** attempt` 会浮点溢出。
    抖动是必要的 —— 数十个边缘站点会在同一次闪断后同步重连，不加抖动就把网络故障
    放大成对数据库的周期性冲击。
    """
    if attempt < 1:
        raise ValueError("attempt 从 1 起算")
    raw = base_ms
    for _ in range(attempt - 1):
        if raw >= cap_ms:
            break
        raw = min(raw * factor, cap_ms)
    return max(0.0, raw + raw * jitter_ratio * (2.0 * rand() - 1.0))


class WriteBuffer:
    """单事件循环内的有界写缓冲。深度以"行"计，因为热路径一次提交的是一批读数。"""

    def __init__(
        self,
        sink: Sink,
        *,
        capacity_rows: int = DEFAULT_CAPACITY_ROWS,
        batch_rows: int = DEFAULT_BATCH_ROWS,
        flush_interval_ms: float = DEFAULT_FLUSH_INTERVAL_MS,
        backoff_base_ms: float = DEFAULT_BACKOFF_BASE_MS,
        backoff_cap_ms: float = DEFAULT_BACKOFF_CAP_MS,
        backoff_factor: float = DEFAULT_BACKOFF_FACTOR,
        jitter_ratio: float = DEFAULT_JITTER_RATIO,
        rng: random.Random | None = None,
    ) -> None:
        if capacity_rows <= 0 or batch_rows <= 0:
            raise ValueError("capacity_rows 与 batch_rows 必须为正")
        if batch_rows > capacity_rows:
            raise ValueError("batch_rows 不得大于 capacity_rows：单批即溢出会让刷写永久落空")
        if not 0.0 <= jitter_ratio < 1.0:
            raise ValueError("jitter_ratio 取值区间为 [0, 1)")
        self._sink = sink
        self._capacity = capacity_rows
        self._batch_rows = batch_rows
        self._interval_s = flush_interval_ms / 1000.0
        self._backoff_base_ms = backoff_base_ms
        self._backoff_cap_ms = backoff_cap_ms
        self._backoff_factor = backoff_factor
        self._jitter_ratio = jitter_ratio
        self._rand = rng.random if rng is not None else random.random
        self._queue: list[WriteRequest] = []
        self._depth = 0
        self._counters = BufferCounters()
        self._send_lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._closed = False
        self._task: asyncio.Task[None] | None = None

    # ---------- 观测面（供 metrics 模块抓取） ----------

    @property
    def depth(self) -> int:
        return self._depth

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def batch_rows(self) -> int:
        return self._batch_rows

    @property
    def counters(self) -> BufferCounters:
        return self._counters

    @property
    def metrics(self) -> dict[str, object]:
        return self._counters.as_dict()

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    # ---------- 热路径入口（同步、永不抛） ----------

    def submit(self, kind: str, rows: Sequence[Any]) -> int:
        """入队并返回实际接收的行数；被丢弃的行只计数，不向上表达为异常。"""
        batch = tuple(rows)
        if not batch:
            return 0
        request = WriteRequest(kind=kind, rows=batch)
        if self._closed:
            self._counters.dropped += request.size
            self._counters.last_error = "缓冲已关闭，写出被丢弃"
            return 0
        if request.size > self._capacity:
            # 单批即超容量：丢新批而非清空历史。先驱逐旧数据会让上游看到"队列正常"，
            # 把持续溢出的信号抹掉；也让一批畸形大请求有能力冲掉整段待写历史。
            self._counters.dropped += request.size
            self._counters.last_error = f"单批 {request.size} 行超出容量 {self._capacity}"
            return 0
        while self._depth + request.size > self._capacity and self._queue:
            victim = self._queue.pop(0)
            self._depth -= victim.size
            self._counters.dropped += victim.size
        self._queue.append(request)
        self._depth += request.size
        self._counters.buffered += request.size
        self._wake.set()
        return request.size

    # ---------- 生命周期 ----------

    def start(self) -> None:
        """拉起后台刷写任务。库未就绪也无妨：失败退避重试，不影响热路径。"""
        if self.running:
            return
        self._task = asyncio.create_task(self._run(), name="aegis-write-buffer")

    async def flush(self, *, timeout_ms: float | None = None) -> int:
        """把当前队列尽量推给 sink，返回本次成功落库行数；sink 失败抛 WriteFailedError。

        与后台刷写共用 `_send_lock`，同一条数据不会被两边各发一次。
        """
        return await asyncio.wait_for(self._drain_once(), timeout=None if timeout_ms is None else timeout_ms / 1000.0)

    async def drain(self) -> int:
        """排到队列空为止（可跨多批），供 close 的 grace 期使用。"""
        total = 0
        while self._depth:
            sent = await self._drain_once()
            if sent == 0:
                break
            total += sent
        return total

    async def close(self, *, grace_ms: float = 2_000.0) -> dict[str, object]:
        """关停：停接收 -> 取消刷写任务 -> grace 内排空 -> 余量按丢弃计数（绝不悬挂进程）。"""
        self._closed = True
        self._wake.set()
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        try:
            if grace_ms > 0:
                await asyncio.wait_for(self.drain(), timeout=grace_ms / 1000.0)
        except Exception as exc:  # 关停路径不得抛：库此时不可达就按丢弃口径收口
            self._counters.last_error = f"关停排空未完成：{type(exc).__name__}"
        if self._depth:
            self._counters.dropped += self._depth
        self._queue.clear()
        self._depth = 0
        return self.metrics

    # ---------- 发送 ----------

    async def _drain_once(self) -> int:
        async with self._send_lock:
            batch = self._take_locked()
            if not batch:
                return 0
            try:
                await self._sink(batch)
            except asyncio.CancelledError:
                # 关停取消不能吞批次：放回队首，交由 close 的 grace 期重试
                self._hold_for_retry(batch)
                raise
            except Exception as exc:
                self._counters.retries += 1
                self._hold_for_retry(batch)
                raise WriteFailedError(f"写出失败：{batch[0].kind}", detail=describe(exc)) from exc
            return self._account_flushed(batch)

    def _take_locked(self) -> list[WriteRequest]:
        taken: list[WriteRequest] = []
        rows = 0
        while self._queue and rows < self._batch_rows:
            request = self._queue[0]
            if taken and rows + request.size > self._batch_rows:
                break
            self._queue.pop(0)
            self._depth -= request.size
            rows += request.size
            taken.append(request)
        return taken

    def _hold_for_retry(self, batch: list[WriteRequest]) -> None:
        """批次放回队首：保持时间序，避免旧数据被新数据插队后永久落后。"""
        self._queue[:0] = batch
        self._depth += sum(request.size for request in batch)

    def _account_flushed(self, batch: list[WriteRequest]) -> int:
        rows = sum(request.size for request in batch)
        self._counters.flushed += rows
        return rows

    # ---------- 后台刷写 ----------

    async def _run(self) -> None:
        attempt = 0
        while not (self._closed and self._depth == 0):
            if attempt:
                await asyncio.sleep(
                    backoff_delay_ms(
                        attempt,
                        base_ms=self._backoff_base_ms,
                        factor=self._backoff_factor,
                        cap_ms=self._backoff_cap_ms,
                        jitter_ratio=self._jitter_ratio,
                        rand=self._rand,
                    )
                    / 1000.0,
                )
            self._wake.clear()
            if self._depth < self._batch_rows:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), timeout=self._interval_s)
            try:
                await self._drain_once()
            except Exception as exc:  # 刷写任务永不因单次写出失败而退出：退避后继续
                attempt += 1
                self._counters.last_error = str(exc)[:512]
                continue
            attempt = 0
