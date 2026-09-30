"""边缘有界缓冲单测：溢出策略、重放保序、msg_id 去重、TTL 丢弃、内存上界、链路状态机。

全部纯逻辑（含 time 注入），不依赖 zenoh 与事件循环。
"""

from __future__ import annotations

import threading

import pytest

from aegis.edge.offline_buffer import (
    DEFAULT_MAX_BYTES,
    DEFAULT_MAX_ITEMS,
    BufferStats,
    EdgeOfflineBuffer,
)


class _Clock:
    """手动时钟：TTL 与链路时间戳必须可判定，不能靠 sleep。"""

    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> _Clock:
    return _Clock()


@pytest.fixture
def buffer(clock: _Clock) -> EdgeOfflineBuffer:
    return EdgeOfflineBuffer(station_id="station01", default_ttl_seconds=None, clock=clock)


class TestBasics:
    def test_defaults_are_bounded(self) -> None:
        buf = EdgeOfflineBuffer()
        assert buf.max_items == DEFAULT_MAX_ITEMS
        assert buf.max_bytes == DEFAULT_MAX_BYTES

    def test_invalid_bounds_rejected(self) -> None:
        with pytest.raises(ValueError):
            EdgeOfflineBuffer(max_items=0)
        with pytest.raises(ValueError):
            EdgeOfflineBuffer(max_bytes=-1)
        with pytest.raises(ValueError):
            EdgeOfflineBuffer(overflow_policy="drop_random")  # type: ignore[arg-type]

    def test_put_returns_monotonic_seq(self, buffer: EdgeOfflineBuffer) -> None:
        first = buffer.put("data.s.rain", b"1")
        second = buffer.put("data.s.rain", b"2")
        assert (first.status, second.status) == ("stored", "stored")
        assert second.seq == first.seq + 1
        assert buffer.pending == 2

    def test_fifo_order_on_replay(self, buffer: EdgeOfflineBuffer) -> None:
        subjects = [f"data.s.m{index}" for index in range(6)]
        for subject in subjects:
            buffer.put(subject, subject.encode())
        buffer.note_link_up()
        drained = buffer.drain()
        assert [sample.subject for sample in drained] == subjects
        assert [sample.seq for sample in drained] == sorted(sample.seq for sample in drained)

    def test_drain_limit_keeps_order(self, buffer: EdgeOfflineBuffer) -> None:
        for index in range(5):
            buffer.put("data.s.m", str(index).encode())
        buffer.note_link_up()
        batch = buffer.drain(2)
        assert [sample.payload for sample in batch] == [b"0", b"1"]
        assert buffer.pending == 3
        assert buffer.in_flight == 2


class TestLinkStateMachine:
    def test_unknown_link_blocks_drain(self, buffer: EdgeOfflineBuffer) -> None:
        buffer.put("data.s.m", b"x")
        assert buffer.link_state == "unknown"
        assert buffer.drain() == []
        assert buffer.stats.blocked_drains == 1
        assert buffer.pending == 1

    def test_down_state_blocks_replay_and_up_resumes(self, buffer: EdgeOfflineBuffer) -> None:
        buffer.put("data.s.m", b"x")
        assert buffer.note_link_down() is True
        assert buffer.note_link_down() is False
        assert buffer.drain() == []
        assert buffer.replay_due is False
        assert buffer.note_link_up() is True
        assert buffer.note_link_up() is False
        assert buffer.replay_due is True
        assert len(buffer.drain()) == 1
        assert buffer.stats.link_flips == 2
        assert buffer.stats.reconnects == 1

    def test_put_still_accepted_while_down(self, buffer: EdgeOfflineBuffer) -> None:
        buffer.note_link_down()
        assert buffer.put("data.s.m", b"x").status == "stored"
        assert buffer.pending == 1

    def test_link_since_uses_injected_clock(self, buffer: EdgeOfflineBuffer, clock: _Clock) -> None:
        clock.advance(5)
        buffer.note_link_down()
        assert buffer.link_since == 1_005.0


class TestAckAndRequeue:
    def test_ack_clears_in_flight_and_counts(self, buffer: EdgeOfflineBuffer) -> None:
        buffer.put("data.s.m", b"x")
        buffer.note_link_up()
        batch = buffer.drain()
        assert buffer.in_flight == 1
        assert buffer.ack(batch) == 1
        assert buffer.in_flight == 0
        assert buffer.stats.replayed == 1

    def test_double_ack_is_idempotent(self, buffer: EdgeOfflineBuffer) -> None:
        buffer.put("data.s.m", b"x")
        buffer.note_link_up()
        batch = buffer.drain()
        buffer.ack(batch)
        assert buffer.ack(batch) == 0
        assert buffer.stats.replayed == 1

    def test_requeue_preserves_order_and_no_duplicates(self, buffer: EdgeOfflineBuffer) -> None:
        for index in range(5):
            buffer.put("data.s.m", str(index).encode())
        buffer.note_link_up()
        first = buffer.drain(2)
        buffer.ack(first)
        buffer.drain(2)
        assert buffer.requeue() == 2
        third = buffer.drain(10)
        seqs = [sample.seq for sample in first] + [sample.seq for sample in third]
        assert seqs == sorted(seqs)
        assert len(set(seqs)) == len(seqs)
        assert [sample.payload for sample in third] == [b"2", b"3", b"4"]

    def test_requeue_drops_expired_in_flight(self, buffer: EdgeOfflineBuffer, clock: _Clock) -> None:
        buffer.put("data.s.m", b"x", ttl_seconds=10)
        buffer.note_link_up()
        assert len(buffer.drain()) == 1
        clock.advance(20)
        assert buffer.requeue() == 0
        assert buffer.stats.dropped_expired == 1

    def test_replay_never_returns_message_twice_after_ack(self, buffer: EdgeOfflineBuffer) -> None:
        for index in range(10):
            buffer.put("data.s.m", str(index).encode())
        buffer.note_link_up()
        delivered: list[bytes] = []
        for _ in range(20):
            batch = buffer.drain(3)
            if not batch:
                break
            delivered.extend(sample.payload for sample in batch)
            buffer.ack(batch)
        assert delivered == [str(index).encode() for index in range(10)]
        assert buffer.pending == 0


class TestOverflowPolicy:
    def test_drop_oldest_evicts_and_counts(self, clock: _Clock) -> None:
        buf = EdgeOfflineBuffer(max_items=3, max_bytes=10_000, default_ttl_seconds=None, clock=clock)
        for index in range(5):
            result = buf.put("data.s.m", f"v{index}".encode())
            assert result.status == "stored"
        assert buf.pending == 3
        assert buf.stats.dropped_overflow == 2
        result = buf.put("data.s.m", b"v5")
        assert (result.status, result.evicted) == ("stored", 1)
        buf.note_link_up()
        assert [sample.payload for sample in buf.drain()] == [b"v3", b"v4", b"v5"]

    def test_reject_policy_refuses_new(self, clock: _Clock) -> None:
        buf = EdgeOfflineBuffer(
            max_items=2,
            max_bytes=10_000,
            default_ttl_seconds=None,
            overflow_policy="reject",
            clock=clock,
        )
        assert buf.put("data.s.m", b"a").status == "stored"
        assert buf.put("data.s.m", b"b").status == "stored"
        assert buf.put("data.s.m", b"c").status == "rejected"
        assert buf.stats.rejected == 1
        assert buf.pending == 2
        buf.note_link_up()
        assert [sample.payload for sample in buf.drain()] == [b"a", b"b"]

    def test_byte_bound_is_never_exceeded(self, clock: _Clock) -> None:
        buf = EdgeOfflineBuffer(max_items=10_000, max_bytes=250, default_ttl_seconds=None, clock=clock)
        for _ in range(50):
            buf.put("data.s.m", bytes(100))
            assert buf.pending_bytes <= 250
            assert buf.pending <= 10_000
        assert buf.pending_bytes == 200
        assert buf.pending == 2
        assert buf.stats.dropped_overflow == 48

    def test_single_oversized_message_rejected(self, clock: _Clock) -> None:
        buf = EdgeOfflineBuffer(max_items=10, max_bytes=64, default_ttl_seconds=None, clock=clock)
        result = buf.put("data.s.m", bytes(65))
        assert result.status == "rejected"
        assert buf.pending == 0

    def test_eviction_rolls_back_byte_accounting(self, clock: _Clock) -> None:
        buf = EdgeOfflineBuffer(max_items=2, max_bytes=1_000, default_ttl_seconds=None, clock=clock)
        buf.put("data.s.m", bytes(10))
        buf.put("data.s.m", bytes(20))
        buf.put("data.s.m", bytes(30))
        assert buf.pending_bytes == 50
        buf.note_link_up()
        buf.drain()
        assert buf.pending_bytes == 0


class TestDedupe:
    def test_duplicate_msg_id_dropped(self, buffer: EdgeOfflineBuffer) -> None:
        first = buffer.put("data.s.m", b"x", msg_id="msg_0000000000000001")
        dup = buffer.put("data.s.m", b"x", msg_id="msg_0000000000000001")
        assert first.status == "stored"
        assert dup.status == "duplicate"
        assert dup.seq == first.seq
        assert buffer.pending == 1
        assert buffer.stats.duplicates == 1

    def test_different_msg_ids_both_stored(self, buffer: EdgeOfflineBuffer) -> None:
        assert buffer.put("data.s.m", b"x", msg_id="msg_1").status == "stored"
        assert buffer.put("data.s.m", b"x", msg_id="msg_2").status == "stored"
        assert buffer.pending == 2

    def test_missing_msg_id_never_dedupes(self, buffer: EdgeOfflineBuffer) -> None:
        buffer.put("data.s.m", b"x")
        buffer.put("data.s.m", b"x")
        assert buffer.pending == 2
        assert buffer.stats.duplicates == 0

    def test_seen_window_is_bounded(self, clock: _Clock) -> None:
        buf = EdgeOfflineBuffer(max_items=100, max_bytes=100_000, max_seen_ids=2, default_ttl_seconds=None, clock=clock)
        for msg_id in ("m1", "m2", "m3"):
            buf.put("data.s.m", b"x", msg_id=msg_id)
        assert len(buf._seen) == 2
        assert buf.put("data.s.m", b"x", msg_id="m1").status == "stored"

    def test_replay_does_not_requeue_acked_duplicates(self, buffer: EdgeOfflineBuffer) -> None:
        buffer.put("data.s.m", b"x", msg_id="msg_a")
        buffer.note_link_up()
        buffer.ack(buffer.drain())
        assert buffer.put("data.s.m", b"x", msg_id="msg_a").status == "duplicate"
        assert buffer.pending == 0


class TestTTL:
    def test_zero_ttl_dropped_at_entry(self, buffer: EdgeOfflineBuffer) -> None:
        result = buffer.put("data.s.m", b"x", ttl_seconds=0)
        assert result.status == "expired"
        assert buffer.pending == 0
        assert buffer.stats.dropped_expired == 1

    def test_expired_dropped_on_drain(self, buffer: EdgeOfflineBuffer, clock: _Clock) -> None:
        buffer.put("data.s.m", b"fresh", ttl_seconds=100)
        buffer.put("data.s.m", b"stale", ttl_seconds=5)
        clock.advance(10)
        buffer.note_link_up()
        drained = buffer.drain()
        assert [sample.payload for sample in drained] == [b"fresh"]
        assert buffer.stats.dropped_expired == 1

    def test_purge_expired_counts(self, buffer: EdgeOfflineBuffer, clock: _Clock) -> None:
        buffer.put("data.s.m", b"a", ttl_seconds=5)
        buffer.put("data.s.m", b"b", ttl_seconds=50)
        clock.advance(10)
        assert buffer.purge_expired() == 1
        assert buffer.pending == 1
        assert buffer.snapshot()["expired_now"] == 0

    def test_default_ttl_applies_when_not_given(self, clock: _Clock) -> None:
        buf = EdgeOfflineBuffer(default_ttl_seconds=3, clock=clock)
        buf.put("data.s.m", b"x")
        clock.advance(2)
        buf.note_link_up()
        assert len(buf.drain()) == 1
        clock.advance(2)
        buf.put("data.s.m", b"y")
        clock.advance(5)
        buf.note_link_up()
        assert buf.drain() == []

    def test_negative_ttl_rejected_as_expired(self, buffer: EdgeOfflineBuffer) -> None:
        assert buffer.put("data.s.m", b"x", ttl_seconds=-1).status == "expired"


class TestQuery:
    def test_query_by_nats_pattern(self, buffer: EdgeOfflineBuffer) -> None:
        buffer.put("data.station01.rain", b"1")
        buffer.put("data.station01.level", b"2")
        buffer.put("agent.perceive.in", b"3")
        assert [sample.payload for sample in buffer.query("data.*.rain")] == [b"1"]
        assert [sample.payload for sample in buffer.query("data.>")] == [b"1", b"2"]
        assert [sample.payload for sample in buffer.query("agent.*.in")] == [b"3"]
        assert buffer.query("workflow.>") == []

    def test_query_respects_limit_and_order(self, buffer: EdgeOfflineBuffer) -> None:
        for index in range(5):
            buffer.put("data.s.m", str(index).encode())
        rows = buffer.query("data.>", limit=2)
        assert [row.seq for row in rows] == [1, 2]

    def test_query_can_exclude_in_flight(self, buffer: EdgeOfflineBuffer) -> None:
        buffer.put("data.s.m", b"1")
        buffer.note_link_up()
        buffer.drain()
        assert len(buffer.query("data.>")) == 1
        assert buffer.query("data.>", include_in_flight=False) == []

    def test_query_skips_expired(self, buffer: EdgeOfflineBuffer, clock: _Clock) -> None:
        buffer.put("data.s.m", b"x", ttl_seconds=5)
        clock.advance(10)
        assert buffer.query("data.>") == []

    def test_query_does_not_consume(self, buffer: EdgeOfflineBuffer) -> None:
        buffer.put("data.s.m", b"x")
        buffer.query("data.>")
        assert buffer.pending == 1


class TestObservability:
    def test_snapshot_shape(self, buffer: EdgeOfflineBuffer) -> None:
        buffer.put("data.s.m", b"x", msg_id="m1")
        buffer.put("data.s.m", b"x", msg_id="m1")
        snapshot = buffer.snapshot()
        assert snapshot["station_id"] == "station01"
        assert snapshot["link_state"] == "unknown"
        assert snapshot["pending"] == 1
        assert snapshot["last_seq"] == 1
        stats = snapshot["stats"]
        assert isinstance(stats, dict)
        assert stats["accepted"] == 1
        assert stats["duplicates"] == 1
        assert stats["offered"] == 2

    def test_stats_offered_counts_every_outcome(self, clock: _Clock) -> None:
        buf = EdgeOfflineBuffer(max_items=1, max_bytes=10, default_ttl_seconds=None, clock=clock)
        buf.put("data.s.m", b"x")
        buf.put("data.s.m", b"y")
        buf.put("data.s.m", b"z", ttl_seconds=0)
        buf.put("data.s.m", bytes(50))
        assert buf.stats.offered == 4
        assert isinstance(buf.stats, BufferStats)

    def test_sequence_log_exposes_order(self, buffer: EdgeOfflineBuffer) -> None:
        buffer.put("data.a", b"1")
        buffer.put("data.b", b"2")
        assert buffer.sequence_log() == [(1, "data.a"), (2, "data.b")]

    def test_pending_and_bytes_properties(self, buffer: EdgeOfflineBuffer) -> None:
        buffer.put("data.s.m", b"12345")
        assert buffer.pending_bytes == 5
        assert buffer.last_seq == 1


class TestConcurrency:
    def test_parallel_producers_respect_bounds(self, clock: _Clock) -> None:
        """zenoh queryable 回调在别的线程读缓冲：写入侧并发必须不破坏上界与总数。"""
        buf = EdgeOfflineBuffer(max_items=500, max_bytes=100_000, default_ttl_seconds=None, clock=clock)

        def produce(worker: int) -> None:
            for index in range(200):
                buf.put(f"data.s{worker}.m", f"{worker}-{index}".encode(), msg_id=f"m{worker}-{index}")

        threads = [threading.Thread(target=produce, args=(worker,)) for worker in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert buf.stats.accepted == 1_600
        assert buf.stats.offered == 1_600
        assert buf.stats.dropped_overflow == 1_100
        assert buf.pending == 500
        assert buf.pending_bytes <= 100_000
        assert buf.stats.duplicates == 0

    def test_concurrent_query_during_put_does_not_raise(self, clock: _Clock) -> None:
        buf = EdgeOfflineBuffer(max_items=100, max_bytes=10_000, default_ttl_seconds=None, clock=clock)
        stop = threading.Event()

        def read() -> None:
            while not stop.is_set():
                buf.query("data.>")
                buf.snapshot()

        reader = threading.Thread(target=read)
        reader.start()
        for index in range(500):
            buf.put("data.s.m", str(index).encode())
        stop.set()
        reader.join(timeout=2)
        assert not reader.is_alive()
