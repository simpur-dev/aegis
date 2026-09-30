"""站点侧有界边缘缓冲：断链期存、复链期按序重放，并可被 store/query 查询。

纯逻辑 + 线程安全，不导入 zenoh：zenoh 的 queryable 回调在独立线程里读本缓冲区，
因此这里用 RLock 而不是假设单线程事件循环。
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from aegis.bus.subjects import subject_matches

OverflowPolicy = Literal["drop_oldest", "reject"]
PutStatus = Literal["stored", "duplicate", "expired", "rejected"]
LinkState = Literal["unknown", "up", "down"]

DEFAULT_MAX_ITEMS = 10_000
DEFAULT_MAX_BYTES = 32 * 1024 * 1024
DEFAULT_TTL_SECONDS = 30.0
DEFAULT_MAX_SEEN_IDS = 50_000


@dataclass(frozen=True, slots=True)
class BufferedSample:
    seq: int
    subject: str
    payload: bytes
    enqueued_at: float
    expires_at: float | None
    msg_id: str | None = None

    def is_expired(self, now: float) -> bool:
        return self.expires_at is not None and self.expires_at <= now


@dataclass(frozen=True, slots=True)
class PutResult:
    status: PutStatus
    seq: int | None = None
    evicted: int = 0
    reason: str | None = None


@dataclass(slots=True)
class BufferStats:
    accepted: int = 0
    duplicates: int = 0
    dropped_expired: int = 0
    dropped_overflow: int = 0
    rejected: int = 0
    replayed: int = 0
    requeued: int = 0
    link_flips: int = 0
    reconnects: int = 0
    blocked_drains: int = 0
    subjects: dict[str, int] = field(default_factory=dict)

    @property
    def offered(self) -> int:
        """put() 调用数：dropped_overflow 不计入，它是入队之后的淘汰而非入队拒绝。"""
        return self.accepted + self.duplicates + self.dropped_expired + self.rejected


class EdgeOfflineBuffer:
    """按 FIFO 存待发消息，序列号单调递增；重放严格保序，未确认的在途样本可回炉。"""

    def __init__(
        self,
        *,
        station_id: str = "edge01",
        max_items: int = DEFAULT_MAX_ITEMS,
        max_bytes: int = DEFAULT_MAX_BYTES,
        default_ttl_seconds: float | None = DEFAULT_TTL_SECONDS,
        max_seen_ids: int = DEFAULT_MAX_SEEN_IDS,
        overflow_policy: OverflowPolicy = "drop_oldest",
        clock: Callable[[], float] | None = None,
    ) -> None:
        if max_items <= 0 or max_bytes <= 0:
            raise ValueError("max_items / max_bytes 必须为正")
        if overflow_policy not in ("drop_oldest", "reject"):
            raise ValueError(f"未知溢出策略: {overflow_policy}")
        self.station_id = station_id
        self.max_items = max_items
        self.max_bytes = max_bytes
        self.default_ttl_seconds = default_ttl_seconds
        self.max_seen_ids = max_seen_ids
        self.overflow_policy = overflow_policy
        self._clock = clock or time.monotonic
        self._lock = threading.RLock()
        self._pending: deque[BufferedSample] = deque()
        self._in_flight: list[BufferedSample] = []
        self._seen: OrderedDict[str, int] = OrderedDict()
        self._seq = 0
        self._pending_bytes = 0
        self._link_state: LinkState = "unknown"
        self._link_since = self._clock()
        self.stats = BufferStats()

    # ---------- 状态机 ----------

    @property
    def link_state(self) -> LinkState:
        with self._lock:
            return self._link_state

    @property
    def link_up(self) -> bool:
        with self._lock:
            return self._link_state == "up"

    def note_link_up(self, *, now: float | None = None) -> bool:
        """返回 True 表示发生了一次 'down/unknown -> up' 翻转（即复链）。"""
        with self._lock:
            if self._link_state == "up":
                return False
            self._link_state = "up"
            self._link_since = now if now is not None else self._clock()
            self.stats.link_flips += 1
            self.stats.reconnects += 1
            return True

    def note_link_down(self, *, now: float | None = None) -> bool:
        with self._lock:
            if self._link_state == "down":
                return False
            self._link_state = "down"
            self._link_since = now if now is not None else self._clock()
            self.stats.link_flips += 1
            return True

    @property
    def link_since(self) -> float:
        with self._lock:
            return self._link_since

    # ---------- 写入 ----------

    def put(
        self,
        subject: str,
        payload: bytes,
        *,
        msg_id: str | None = None,
        ttl_seconds: float | None = None,
        now: float | None = None,
    ) -> PutResult:
        moment = now if now is not None else self._clock()
        ttl = self.default_ttl_seconds if ttl_seconds is None else ttl_seconds
        expires_at = None if ttl is None else moment + ttl
        with self._lock:
            if msg_id and msg_id in self._seen:
                self.stats.duplicates += 1
                return PutResult(status="duplicate", seq=self._seen[msg_id], reason="msg_id 已在去重窗口内")
            if expires_at is not None and expires_at <= moment:
                self.stats.dropped_expired += 1
                return PutResult(status="expired", reason="入队即过期 (ttl<=0)")

            size = len(payload)
            if size > self.max_bytes:
                self.stats.rejected += 1
                return PutResult(status="rejected", reason=f"单条 {size}B 超缓冲字节上限 {self.max_bytes}")

            evicted = 0
            if self.overflow_policy == "reject" and (len(self._pending) >= self.max_items or self._pending_bytes + size > self.max_bytes):
                self.stats.rejected += 1
                return PutResult(status="rejected", reason="缓冲已满且策略为 reject")

            while self._pending and (len(self._pending) + 1 > self.max_items or self._pending_bytes + size > self.max_bytes):
                oldest = self._pending.popleft()
                self._pending_bytes -= len(oldest.payload)
                evicted += 1
                self.stats.dropped_overflow += 1

            self._seq += 1
            sample = BufferedSample(
                seq=self._seq,
                subject=subject,
                payload=payload,
                enqueued_at=moment,
                expires_at=expires_at,
                msg_id=msg_id,
            )
            self._pending.append(sample)
            self._pending_bytes += size
            self.stats.accepted += 1
            self.stats.subjects[subject] = self.stats.subjects.get(subject, 0) + 1
            if msg_id:
                self._remember(msg_id, sample.seq)
            return PutResult(status="stored", seq=sample.seq, evicted=evicted)

    def _remember(self, msg_id: str, seq: int) -> None:
        """去重窗口有界：只保最近 max_seen_ids 个 msg_id，防止长期运行内存无上限。"""
        self._seen[msg_id] = seq
        while len(self._seen) > self.max_seen_ids:
            self._seen.popitem(last=False)

    # ---------- 重放 ----------

    def drain(self, limit: int | None = None, *, now: float | None = None, require_link_up: bool = True) -> list[BufferedSample]:
        """取出下一批待发消息；断链期不取出（消息留在本地），过期消息在此丢弃。"""
        moment = now if now is not None else self._clock()
        with self._lock:
            if require_link_up and self._link_state != "up":
                self.stats.blocked_drains += 1
                return []
            batch: list[BufferedSample] = []
            while self._pending and (limit is None or len(batch) < limit):
                sample = self._pending.popleft()
                self._pending_bytes -= len(sample.payload)
                if sample.is_expired(moment):
                    self.stats.dropped_expired += 1
                    continue
                batch.append(sample)
            # 过期消息只在队首被顺带清理：一次性扫全表会把 O(n) 成本压进热路径。
            self._in_flight.extend(batch)
            return batch

    def ack(self, samples: list[BufferedSample] | tuple[BufferedSample, ...]) -> int:
        with self._lock:
            acknowledged = 0
            for sample in samples:
                if sample in self._in_flight:
                    self._in_flight.remove(sample)
                    acknowledged += 1
            self.stats.replayed += acknowledged
            return acknowledged

    def requeue(self, *, now: float | None = None) -> int:
        """在途未确认样本回到队首，序列号顺序不变——复链重放不得改变消息次序。"""
        moment = now if now is not None else self._clock()
        with self._lock:
            restored = [sample for sample in self._in_flight if not sample.is_expired(moment)]
            expired = len(self._in_flight) - len(restored)
            self.stats.dropped_expired += expired
            self._in_flight.clear()
            for sample in reversed(restored):
                self._pending.appendleft(sample)
                self._pending_bytes += len(sample.payload)
            self.stats.requeued += len(restored)
            return len(restored)

    # ---------- 查询与观测 ----------

    def purge_expired(self, *, now: float | None = None) -> int:
        moment = now if now is not None else self._clock()
        with self._lock:
            kept = [sample for sample in self._pending if not sample.is_expired(moment)]
            removed = len(self._pending) - len(kept)
            if removed:
                self._pending = deque(kept)
                self._pending_bytes = sum(len(sample.payload) for sample in kept)
                self.stats.dropped_expired += removed
            return removed

    def query(
        self,
        pattern: str,
        *,
        limit: int | None = None,
        include_in_flight: bool = True,
        now: float | None = None,
    ) -> list[BufferedSample]:
        """按 NATS 模式在本地缓冲区做 store/query（不消费、不重放）。"""
        moment = now if now is not None else self._clock()
        with self._lock:
            pool: list[BufferedSample] = list(self._pending)
            if include_in_flight:
                pool.extend(self._in_flight)
            pool.sort(key=lambda sample: sample.seq)
            matched = [sample for sample in pool if not sample.is_expired(moment) and subject_matches(pattern, sample.subject)]
            return matched if limit is None else matched[:limit]

    @property
    def pending(self) -> int:
        with self._lock:
            return len(self._pending)

    @property
    def in_flight(self) -> int:
        with self._lock:
            return len(self._in_flight)

    @property
    def pending_bytes(self) -> int:
        with self._lock:
            return self._pending_bytes

    @property
    def last_seq(self) -> int:
        with self._lock:
            return self._seq

    @property
    def replay_due(self) -> bool:
        with self._lock:
            return self._link_state == "up" and bool(self._pending)

    def sequence_log(self) -> list[tuple[int, str]]:
        with self._lock:
            return [(sample.seq, sample.subject) for sample in self._pending]

    def snapshot(self, *, now: float | None = None) -> dict[str, object]:
        moment = now if now is not None else self._clock()
        with self._lock:
            return {
                "station_id": self.station_id,
                "link_state": self._link_state,
                "pending": len(self._pending),
                "pending_bytes": self._pending_bytes,
                "in_flight": len(self._in_flight),
                "max_items": self.max_items,
                "max_bytes": self.max_bytes,
                "default_ttl_seconds": self.default_ttl_seconds,
                "overflow_policy": self.overflow_policy,
                "last_seq": self._seq,
                "expired_now": sum(1 for s in self._pending if s.is_expired(moment)),
                "stats": {
                    "accepted": self.stats.accepted,
                    "duplicates": self.stats.duplicates,
                    "dropped_expired": self.stats.dropped_expired,
                    "dropped_overflow": self.stats.dropped_overflow,
                    "rejected": self.stats.rejected,
                    "replayed": self.stats.replayed,
                    "requeued": self.stats.requeued,
                    "link_flips": self.stats.link_flips,
                    "reconnects": self.stats.reconnects,
                    "blocked_drains": self.stats.blocked_drains,
                    "offered": self.stats.offered,
                },
            }


__all__ = [
    "DEFAULT_MAX_BYTES",
    "DEFAULT_MAX_ITEMS",
    "DEFAULT_MAX_SEEN_IDS",
    "DEFAULT_TTL_SECONDS",
    "BufferStats",
    "BufferedSample",
    "EdgeOfflineBuffer",
    "LinkState",
    "OverflowPolicy",
    "PutResult",
    "PutStatus",
]
