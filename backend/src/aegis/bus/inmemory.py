"""进程内总线实现：单测、离线开发、一致性测试驱动用；语义与 NATS 对齐（通配匹配 + 队列组 + 请求回执）。"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from aegis.bus.subjects import subject_matches
from aegis.bus.transport import BusTransport, RawCallback, Subscription

log = logging.getLogger("aegis.bus.memory")


@dataclass
class _Entry:
    subject: str
    callback: RawCallback
    subscription: Subscription
    queue: str | None = None
    cursor: int = 0


@dataclass
class _QueueGroup:
    entries: list[_Entry] = field(default_factory=list)
    cursor: int = 0


class InMemoryBus(BusTransport):
    name = "memory"

    def __init__(self) -> None:
        super().__init__()
        self._entries: list[_Entry] = []
        self._groups: dict[str, _QueueGroup] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._seq = 0
        self.published = 0
        self.delivered = 0
        self.dropped_no_subscriber = 0

    async def connect(self) -> None:
        self._connected = True

    async def close(self) -> None:
        """关停：给在途投递一个短暂宽限，随后取消卡住的回调——关停不得被下游拖死。"""
        self._connected = False
        tasks = list(self._tasks)
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=0.2)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        self._tasks.clear()
        self._entries.clear()
        self._groups.clear()

    async def _publish_raw(self, subject: str, payload: bytes) -> None:
        self.published += 1
        targets: list[_Entry] = []
        for entry in list(self._entries):
            if subject_matches(entry.subject, subject):
                targets.append(entry)

        if not targets:
            self.dropped_no_subscriber += 1
            log.debug("无订阅者，消息丢弃", extra={"subject": subject})
            return

        # 队列组内工作池语义：同组只投递给一个实例（轮询）；非队列订阅全部投递
        grouped: dict[str | None, list[_Entry]] = {}
        for entry in targets:
            grouped.setdefault(entry.queue, []).append(entry)

        for queue, entries in grouped.items():
            if queue is None:
                chosen = entries
            else:
                group = self._groups.setdefault(queue, _QueueGroup())
                live = [e for e in group.entries if e in entries] or entries
                idx = group.cursor % len(live)
                group.cursor += 1
                chosen = [live[idx]]
            for entry in chosen:
                self._spawn(entry, payload)

    def _spawn(self, entry: _Entry, payload: bytes) -> None:
        async def _run() -> None:
            try:
                await entry.callback(payload)
                self.delivered += 1
                entry.subscription.delivered += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # 订阅方异常不得影响发布方
                entry.subscription.errors += 1
                log.exception("订阅回调异常", extra={"subject": entry.subject, "error": str(exc)})

        task = asyncio.create_task(_run(), name=f"bus-{entry.subject}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _subscribe_raw(
        self,
        subject: str,
        callback: RawCallback,
        *,
        queue: str | None = None,
        durable: str | None = None,
    ) -> Subscription:
        self._seq += 1
        sub = Subscription(
            id=f"sub_{self._seq:06d}",
            subject=subject,
            queue=queue,
            durable=durable,
        )
        entry = _Entry(subject=subject, callback=callback, subscription=sub, queue=queue)

        async def _cancel() -> None:
            if entry in self._entries:
                self._entries.remove(entry)
            group = self._groups.get(queue) if queue else None
            if group and entry in group.entries:
                group.entries.remove(entry)

        sub._cancel = _cancel
        self._entries.append(entry)
        if queue:
            self._groups.setdefault(queue, _QueueGroup()).entries.append(entry)
        return sub

    async def idle(self, *, timeout: float = 5.0) -> None:
        """等待所有在途投递完成——测试与压测计时前必须调用，避免竞态导致的假阴性。

        看门狗到期即抛 TimeoutError，不会被卡死的订阅回调拖住。
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while self._tasks:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError(f"总线未在 {timeout}s 内静默，仍在途任务数={len(self._tasks)}")
            pending = [t for t in list(self._tasks) if not t.done()]
            if not pending:
                await asyncio.sleep(0)
                continue
            _, still = await asyncio.wait(pending, timeout=remaining)
            if still:
                raise TimeoutError(f"总线未在 {timeout}s 内静默，仍在途任务数={len(still)}")

    @property
    def subscription_count(self) -> int:
        return len(self._entries)
