"""两个 sink 共用的有界异步批处理缓冲：热路径只入队，批量落库交给后台 flusher。

复用 `persistence.buffer.WriteBuffer` 的**模式**（有界队列、满则丢最旧并计数、封顶指数退避、
显式 flush 才抛错），但这是一份独立实现——`analytics` 与 `persistence` 是并行演进的两个模块，
不应互相 import 未定型的内部类型。跨模块共享的是"契约"而非"实现"：本文件用 `BatchingPort`
协议定义批处理端，两个 sink 都消费该协议（默认注入 `BoundedBatcher`），落库动作由注入的
`send` 回调承担（ClickHouse → 工作线程批量 insert；DuckDB → 工作线程 executemany），
因此批处理逻辑可完全离线单测。

不变式：
1. `offer()` 是同步方法、内部不 await：只往 `deque` 塞行，永不阻塞、永不触网；
2. 缓冲满按 `drop_policy` 丢弃并计入 `dropped_overflow`，丢弃是显式策略而非静默丢数据；
3. `send` 抛错时失败批次回灌队首（保序），回灌不下的部分计入 `dropped_overflow`；
4. 只有显式 `flush()` 把 `send` 的类型化错误上抛给调用方；后台循环吞错但保留数据与计数。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import deque
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import asdict, dataclass
from typing import Generic, Protocol, TypeVar, runtime_checkable

from aegis.analytics.port import DropPolicy

T = TypeVar("T")
# 协议里 T 只出现在入参位置（Sequence[T]），必须是逆变：用不变 TypeVar 会被 mypy 判为
# "Invariant type variable used in protocol where contravariant one is expected"。
T_contra = TypeVar("T_contra", contravariant=True)

Send = Callable[[Sequence[T]], Awaitable[int]]

logger = logging.getLogger("aegis.analytics.buffer")

DEFAULT_MAX_BATCH = 1_000
DEFAULT_BUFFER_LIMIT = 20_000
DEFAULT_FLUSH_INTERVAL_SECONDS = 5.0
DEFAULT_CLOSE_TIMEOUT_SECONDS = 10.0


@dataclass(slots=True)
class BufferCounters:
    """缓冲台账（只增计数，供 sink.stats 与 /metrics 同源）。"""

    accepted: int = 0
    inserted: int = 0
    batches: int = 0
    failures: int = 0
    flushes: int = 0
    dropped_overflow: int = 0
    dropped_closed: int = 0
    requeued: int = 0

    def as_dict(self) -> dict[str, int]:
        return asdict(self)

    def loss_rate(self) -> float:
        if self.accepted <= 0:
            return 0.0
        return (self.dropped_overflow + self.dropped_closed) / self.accepted


@runtime_checkable
class BatchingPort(Protocol[T_contra]):
    """批处理缓冲的最小契约：sink 只依赖它，测试用替身替换它。

    `offer` 必须同步、不 await、不抛错（只入队+溢出计数）；`flush` 落尽缓冲并在最终失败时抛出；
    `aclose` 在 grace 内尽力排空后关停后台任务；`stats` 是纯内存快照。
    """

    def offer(self, rows: Sequence[T_contra]) -> int: ...

    async def start(self) -> None: ...

    async def flush(self) -> None: ...

    async def aclose(self, grace_ms: int) -> None: ...

    def stats(self) -> dict[str, object]: ...


class BoundedBatcher(Generic[T]):
    """有界缓冲 + 定时/满批 flush 的批处理引擎（`BatchingPort` 的默认实现）。"""

    def __init__(
        self,
        send: Send[T],
        *,
        name: str = "aegis-analytics-batch",
        max_batch: int = DEFAULT_MAX_BATCH,
        buffer_limit: int = DEFAULT_BUFFER_LIMIT,
        flush_interval: float = DEFAULT_FLUSH_INTERVAL_SECONDS,
        drop_policy: DropPolicy = "oldest",
        close_timeout: float = DEFAULT_CLOSE_TIMEOUT_SECONDS,
    ) -> None:
        if max_batch <= 0:
            raise ValueError(f"max_batch 必须为正: {max_batch}")
        if buffer_limit < max_batch:
            raise ValueError(f"buffer_limit({buffer_limit}) 不得小于 max_batch({max_batch})")
        if flush_interval <= 0:
            raise ValueError(f"flush_interval 必须为正: {flush_interval}")
        if drop_policy not in ("oldest", "newest"):
            raise ValueError(f"不支持的丢弃策略: {drop_policy}")

        self._send = send
        self.name = name
        self.max_batch = max_batch
        self.buffer_limit = buffer_limit
        self.flush_interval = flush_interval
        self.drop_policy: DropPolicy = drop_policy
        self.close_timeout = close_timeout

        self._buffer: deque[T] = deque()
        self._lock = asyncio.Lock()
        self._kick = asyncio.Event()
        self._closing = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._counters = BufferCounters()
        self._last_error: BaseException | None = None
        self._warned_idle = False

    # --- 只读状态 ---

    @property
    def counters(self) -> BufferCounters:
        return self._counters

    @property
    def buffered(self) -> int:
        return len(self._buffer)

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def closing(self) -> bool:
        return self._closing.is_set()

    @property
    def last_error(self) -> BaseException | None:
        return self._last_error

    # --- 生命周期 ---

    async def start(self) -> None:
        """启动后台 flush 循环（幂等）。"""
        if self._closing.is_set():
            raise RuntimeError(f"{self.name} 已关闭，不能再次启动")
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._serve(), name=self.name)

    def offer(self, rows: Sequence[T]) -> int:
        """把行放进有界缓冲：满批踢一次后台 flush。永不等待、永不抛错（同步方法）。"""
        items = list(rows)
        if not items:
            return 0
        # 受理数先计入再判定：关停后的拒收同样是"上游交给了我们"的行，
        # 漏记会让 accepted < inserted + dropped + buffered，损失率被算小。
        self._counters.accepted += len(items)
        if self._closing.is_set():
            self._counters.dropped_closed += len(items)
            logger.warning("分析缓冲已在关停，丢弃行", extra={"sink": self.name, "dropped": len(items)})
            return 0
        if not self.running and not self._warned_idle:
            self._warned_idle = True
            logger.warning("分析缓冲未 start()，行仅在内存积压", extra={"sink": self.name, "buffered": len(self._buffer)})
        dropped = self._offer_extend(items)
        self._counters.dropped_overflow += dropped
        if dropped:
            logger.warning(
                "分析缓冲溢出，按策略丢弃",
                extra={"sink": self.name, "drop_policy": self.drop_policy, "dropped": dropped, "buffer_limit": self.buffer_limit},
            )
        if self.running and len(self._buffer) >= self.max_batch:
            self._kick.set()
        return len(items) - dropped

    async def flush(self) -> None:
        """显式落尽缓冲（分批）。成功行数读 counters.inserted；`send` 的最终失败原样上抛。"""
        await self._drain()
        self._counters.flushes += 1

    async def aclose(self, grace_ms: int) -> None:
        """幂等关停：停后台任务 → 在 grace 内尽力 flush → 余量计入 dropped_closed 并清零。"""
        if self._closing.is_set():
            return
        self._closing.set()
        self._kick.set()
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        grace = grace_ms / 1000.0 if grace_ms > 0 else 0.0
        try:
            if grace > 0:
                await asyncio.wait_for(self._drain(), timeout=grace)
        except asyncio.CancelledError:
            self._discard_buffer()
            raise  # 调用方取消关停：仍然要把"尽力而为"的余量记账，但绝不吞掉取消
        except BaseException as exc:  # 关停尽力而为：落不下的行不阻塞关停，转为可见计数
            self._record_error(exc)
            logger.warning(
                "分析缓冲关停时仍有未落盘数据", extra={"sink": self.name, "buffered": len(self._buffer), "err": type(exc).__name__}
            )
        self._discard_buffer()

    def _discard_buffer(self) -> None:
        """把排不空的余量转成可见计数（而非静默丢数据），并清空缓冲。"""
        remaining = len(self._buffer)
        if remaining:
            self._counters.dropped_closed += remaining
            self._buffer.clear()

    # --- 内部 ---

    def _offer_extend(self, rows: Sequence[T]) -> int:
        """入队并按策略裁剪，返回被丢弃行数（同步、无 await，因此天然原子）。"""
        overflow = len(self._buffer) + len(rows) - self.buffer_limit
        if overflow <= 0:
            self._buffer.extend(rows)
            return 0
        if self.drop_policy == "newest":
            room = max(0, self.buffer_limit - len(self._buffer))
            self._buffer.extend(rows[:room])
            return len(rows) - room
        drop_old = min(overflow, len(self._buffer))
        for _ in range(drop_old):
            self._buffer.popleft()
        drop_new = overflow - drop_old
        self._buffer.extend(rows[drop_new:])
        return overflow

    def _take_batch(self) -> list[T]:
        batch: list[T] = []
        while self._buffer and len(batch) < self.max_batch:
            batch.append(self._buffer.popleft())
        return batch

    def _requeue(self, batch: Iterable[T]) -> None:
        """失败批次回到队首（保序）；缓冲已无空间的部分计入溢出。"""
        head = list(batch)
        room = self.buffer_limit - len(self._buffer)
        if room <= 0:
            self._counters.dropped_overflow += len(head)
            return
        keep = head[:room]
        self._counters.dropped_overflow += len(head) - len(keep)
        self._counters.requeued += len(keep)
        for row in reversed(keep):
            self._buffer.appendleft(row)

    async def _drain(self) -> int:
        """在锁内逐批写空缓冲；失败批次回灌队首并抛出原始类型化错误。返回本次落库行数。"""
        inserted = 0
        async with self._lock:
            while True:
                batch = self._take_batch()
                if not batch:
                    break
                try:
                    written = await self._send(batch)
                except BaseException as exc:
                    self._requeue(batch)
                    self._record_error(exc)
                    self._counters.failures += 1
                    raise
                inserted += written
                self._counters.inserted += written
                self._counters.batches += 1
        return inserted

    async def _safe_drain(self) -> None:
        try:
            await self._drain()
        except asyncio.CancelledError:
            # 关停信号必须原样传播：吞掉它会让后台 flusher 变成"杀不掉的任务"，
            # 于是 asyncio.run() 收尾与平台优雅退出都会永久卡住。
            raise
        except Exception as exc:  # 后台循环吞业务异常：数据已回灌，下轮或下次 offer 再试
            logger.warning(
                "分析缓冲后台 flush 失败，数据保留", extra={"sink": self.name, "buffered": len(self._buffer), "err": type(exc).__name__}
            )

    def _record_error(self, exc: BaseException) -> None:
        self._last_error = exc

    async def _serve(self) -> None:
        while not self._closing.is_set():
            with contextlib.suppress(TimeoutError):  # 到点即 flush，超时是正常路径
                await asyncio.wait_for(self._kick.wait(), timeout=self.flush_interval)
            self._kick.clear()
            if self._closing.is_set():
                break
            await self._safe_drain()

    def stats(self) -> dict[str, object]:
        """给 sink.stats /metrics 复用的纯内存快照（不触网、不读盘）。"""
        snapshot: dict[str, object] = {
            "buffered": len(self._buffer),
            "buffer_limit": self.buffer_limit,
            "max_batch": self.max_batch,
            "drop_policy": self.drop_policy,
            "running": self.running,
            "loss_rate": round(self._counters.loss_rate(), 6),
            "last_error": type(self._last_error).__name__ if self._last_error is not None else None,
        }
        snapshot.update(self._counters.as_dict())
        return snapshot


__all__ = [
    "DEFAULT_BUFFER_LIMIT",
    "DEFAULT_CLOSE_TIMEOUT_SECONDS",
    "DEFAULT_FLUSH_INTERVAL_SECONDS",
    "DEFAULT_MAX_BATCH",
    "BatchingPort",
    "BoundedBatcher",
    "BufferCounters",
    "Send",
]
