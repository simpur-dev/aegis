"""WriteBuffer 边界测试：构造校验、溢出策略、退避序列、关停排空与计数守恒。

热路径纪律是本模块的存在理由：submit 必须同步、不 await、不抛错，
库不可达时只计数并把重试留给后台刷写。
"""

from __future__ import annotations

import asyncio
import random
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from aegis.persistence.errors import WriteFailedError
from aegis.persistence.write_buffer import (
    DEFAULT_BATCH_ROWS,
    DEFAULT_CAPACITY_ROWS,
    WriteBuffer,
    WriteRequest,
    backoff_delay_ms,
)


class RecordingSink:
    """记录每批收到的 WriteRequest；可注入前 n 次失败。"""

    def __init__(self, fail_times: int = 0, error: Exception | None = None, delay: float = 0.0) -> None:
        self.batches: list[list[WriteRequest]] = []
        self.calls = 0
        self._fail_times = fail_times
        self._error = error or RuntimeError("库不可达")
        self._delay = delay

    @property
    def rows(self) -> int:
        return sum(req.size for batch in self.batches for req in batch)

    async def __call__(self, batch: list[WriteRequest]) -> None:
        self.calls += 1
        if self._delay:
            await asyncio.sleep(self._delay)
        if self.calls <= self._fail_times:
            raise self._error
        self.batches.append(batch)


def make_sink(fail_times: int = 0, delay: float = 0.0) -> RecordingSink:
    return RecordingSink(fail_times=fail_times, delay=delay)


class TestConstruction:
    @pytest.mark.parametrize(
        "kwargs",
        [
            {"capacity_rows": 0},
            {"capacity_rows": -1},
            {"batch_rows": 0},
            {"batch_rows": -5},
            {"capacity_rows": 10, "batch_rows": 11},
            {"jitter_ratio": -0.01},
            {"jitter_ratio": 1.0},
        ],
    )
    def test_rejects_inconsistent_bounds_at_construction(self, kwargs: dict[str, Any]) -> None:
        with pytest.raises(ValueError):
            WriteBuffer(make_sink(), **kwargs)

    def test_batch_equal_to_capacity_is_allowed(self) -> None:
        buf = WriteBuffer(make_sink(), capacity_rows=10, batch_rows=10)
        assert buf.batch_rows == 10

    def test_defaults_are_usable_without_start(self) -> None:
        buf = WriteBuffer(make_sink())
        assert buf.capacity == DEFAULT_CAPACITY_ROWS
        assert buf.batch_rows == DEFAULT_BATCH_ROWS
        assert not buf.running
        assert not buf.closed
        assert buf.depth == 0


class TestHotPath:
    def test_submit_is_synchronous_and_returns_accepted_rows(self) -> None:
        buf = WriteBuffer(make_sink())
        assert buf.submit("telemetry", [1, 2, 3]) == 3
        assert buf.depth == 3
        assert buf.counters.buffered == 3

    def test_empty_submission_is_accepted_as_zero_without_queueing(self) -> None:
        buf = WriteBuffer(make_sink())
        assert buf.submit("telemetry", []) == 0
        assert buf.depth == 0
        assert buf.counters.buffered == 0

    def test_submit_never_raises_when_sink_is_broken(self) -> None:
        """sink 立刻失败也不能影响热路径：提交只入队。"""
        buf = WriteBuffer(_always_failing_sink())
        assert buf.submit("telemetry", [1, 2]) == 2

    async def test_kind_is_carried_through_to_the_sink(self) -> None:
        sink = make_sink()
        buf = WriteBuffer(sink, capacity_rows=100, batch_rows=10)
        buf.submit("warnings", ["w1"])
        buf.submit("telemetry", ["t1"])
        await buf.flush()
        assert [req.kind for batch in sink.batches for req in batch] == ["warnings", "telemetry"]


def _always_failing_sink() -> RecordingSink:
    return RecordingSink(fail_times=10**9)


class TestOverflowPolicy:
    def test_full_queue_drops_oldest_and_counts_dropped_rows(self) -> None:
        buf = WriteBuffer(make_sink(), capacity_rows=5, batch_rows=2)
        assert buf.submit("a", [1, 2, 3]) == 3
        assert buf.submit("b", [4, 5, 6]) == 3
        # 容量 5：先丢最旧的整批（3 行），再收新批
        assert buf.depth <= 5
        assert buf.counters.dropped == 3
        assert buf.counters.dropped + buf.depth == buf.counters.buffered

    def test_single_batch_larger_than_capacity_drops_new_not_history(self) -> None:
        """单批即超容量时丢新批：清空历史会让上游看到"持续正常"，掩盖真实溢出。"""
        buf = WriteBuffer(make_sink(), capacity_rows=4, batch_rows=2)
        assert buf.submit("keep", [1, 2]) == 2
        assert buf.submit("huge", list(range(10))) == 0
        assert buf.depth == 2
        assert buf.counters.dropped == 10
        assert "单批" in str(buf.counters.last_error)

    def test_submit_after_close_counts_as_dropped(self) -> None:
        async def scenario() -> None:
            buf = WriteBuffer(make_sink(), capacity_rows=10, batch_rows=5)
            buf.submit("a", [1, 2])
            await buf.close(grace_ms=100)
            assert buf.closed
            assert buf.submit("a", [3, 4]) == 0
            assert buf.counters.dropped == 2

        asyncio.run(scenario())


class TestFlushAndBatching:
    async def test_flush_returns_flushed_row_count(self) -> None:
        sink = make_sink()
        buf = WriteBuffer(sink, capacity_rows=100, batch_rows=10)
        buf.submit("t", list(range(6)))
        assert await buf.flush() == 6
        assert buf.depth == 0
        assert buf.counters.flushed == 6

    async def test_flush_takes_whole_requests_up_to_the_row_budget(self) -> None:
        """batch_rows 是"行预算"不是"行切片"：批次只整批取，绝不把一次提交拆开。"""
        sink = make_sink()
        buf = WriteBuffer(sink, capacity_rows=100, batch_rows=4)
        for i in range(3):
            buf.submit("t", [i] * 3)
        sent = await buf.flush()
        assert sent == 3  # 首批 3 行；再加一批就达 6 > 4，故留在队列
        assert buf.depth == 6
        assert [sum(req.size for req in batch) for batch in sink.batches] == [3]

    async def test_flush_on_empty_buffer_returns_zero(self) -> None:
        assert await WriteBuffer(make_sink()).flush() == 0

    async def test_drain_loops_until_queue_is_empty(self) -> None:
        sink = make_sink()
        buf = WriteBuffer(sink, capacity_rows=100, batch_rows=2)
        buf.submit("t", list(range(9)))
        assert await buf.drain() == 9
        assert buf.depth == 0

    async def test_background_pump_flushes_without_manual_call(self) -> None:
        sink = make_sink()
        buf = WriteBuffer(sink, capacity_rows=100, batch_rows=5, flush_interval_ms=10)
        buf.start()
        try:
            buf.submit("t", [1, 2, 3])
            for _ in range(200):
                if sink.rows == 3:
                    break
                await asyncio.sleep(0.005)
            assert sink.rows == 3
        finally:
            await buf.close(grace_ms=100)

    async def test_start_is_idempotent(self) -> None:
        buf = WriteBuffer(make_sink(), flush_interval_ms=10)
        buf.start()
        first = buf._task
        buf.start()
        assert buf._task is first
        await buf.close(grace_ms=50)

    async def test_pump_stays_idle_on_empty_queue(self) -> None:
        sink = make_sink()
        buf = WriteBuffer(sink, flush_interval_ms=5)
        buf.start()
        try:
            await asyncio.sleep(0.06)
            assert sink.calls == 0
        finally:
            await buf.close(grace_ms=50)


class TestRetryAndOutage:
    async def test_transient_failure_is_retried_then_succeeds(self) -> None:
        sink = RecordingSink(fail_times=1)
        buf = WriteBuffer(sink, capacity_rows=100, batch_rows=10, backoff_base_ms=1)
        buf.submit("t", [1, 2])
        with pytest.raises(WriteFailedError):
            await buf.flush()
        assert buf.depth == 2  # 失败批次回到队列，不丢数据
        assert buf.counters.retries >= 1
        assert await buf.flush() == 2
        assert sink.rows == 2

    async def test_flush_timeout_does_not_lose_queued_rows(self) -> None:
        sink = make_sink(delay=0.2)
        buf = WriteBuffer(sink, capacity_rows=100, batch_rows=10)
        buf.submit("t", [1, 2, 3])
        with pytest.raises((WriteFailedError, TimeoutError)):
            await buf.flush(timeout_ms=10)
        await asyncio.sleep(0.25)
        assert buf.depth + sink.rows == 3

    def test_backoff_is_exponential_capped_and_jittered(self) -> None:
        seq = [backoff_delay_ms(i, base_ms=100, factor=2, cap_ms=800, jitter_ratio=0.0) for i in range(1, 6)]
        assert seq == [100, 200, 400, 800, 800]

    def test_backoff_jitter_stays_within_ratio_band(self) -> None:
        for attempt in range(1, 8):
            values = [backoff_delay_ms(attempt, base_ms=100, cap_ms=8000, jitter_ratio=0.25) for _ in range(200)]
            nominal = min(100 * 2.0 ** (attempt - 1), 8000)
            assert all(nominal * 0.75 - 1e-9 <= v <= nominal * 1.25 + 1e-9 for v in values)

    def test_backoff_rejects_attempt_below_one(self) -> None:
        with pytest.raises(ValueError, match="attempt"):
            backoff_delay_ms(0)

    def test_backoff_is_deterministic_with_injected_rng(self) -> None:
        a = backoff_delay_ms(3, jitter_ratio=0.25, rand=random.Random(7).random)
        b = backoff_delay_ms(3, jitter_ratio=0.25, rand=random.Random(7).random)
        assert a == b

    async def test_outage_does_not_block_the_hot_path(self) -> None:
        """库持续不可达时，热路径提交仍必须是常数时间且不抛错。"""
        sink = _always_failing_sink()
        buf = WriteBuffer(sink, capacity_rows=1000, batch_rows=10, flush_interval_ms=5, backoff_base_ms=1)
        buf.start()
        try:
            loop = asyncio.get_running_loop()
            started = loop.time()
            for i in range(200):
                buf.submit("t", [i])
            elapsed = loop.time() - started
            assert elapsed < 0.5
            assert buf.counters.buffered == 200
        finally:
            await buf.close(grace_ms=50)


class TestShutdown:
    async def test_close_drains_backlog_within_grace(self) -> None:
        sink = make_sink()
        buf = WriteBuffer(sink, capacity_rows=100, batch_rows=2)
        buf.submit("t", list(range(5)))
        metrics = await buf.close(grace_ms=500)
        assert sink.rows == 5
        assert buf.depth == 0
        assert metrics["dropped"] == 0

    async def test_close_counts_undrainable_backlog_as_dropped(self) -> None:
        sink = _always_failing_sink()
        buf = WriteBuffer(sink, capacity_rows=100, batch_rows=2)
        buf.submit("t", [1, 2, 3])
        metrics = await buf.close(grace_ms=50)
        assert metrics["dropped"] == 3
        assert buf.depth == 0

    async def test_close_before_start_is_safe_and_idempotent(self) -> None:
        buf = WriteBuffer(make_sink(), capacity_rows=10, batch_rows=5)
        first = await buf.close(grace_ms=10)
        second = await buf.close(grace_ms=10)
        assert first == second
        assert buf.closed

    async def test_close_cancels_pump_without_hanging(self) -> None:
        sink = make_sink(delay=10)
        buf = WriteBuffer(sink, flush_interval_ms=5)
        buf.start()
        buf.submit("t", [1])
        await asyncio.sleep(0.02)
        metrics = await buf.close(grace_ms=30)
        assert isinstance(metrics, dict)


class TestMetricsSurface:
    async def test_counters_and_metrics_shape(self) -> None:
        sink = make_sink()
        buf = WriteBuffer(sink, capacity_rows=10, batch_rows=4)
        buf.submit("t", [1, 2, 3, 4, 5])
        await buf.flush()
        metrics = buf.metrics
        for key in ("buffered", "flushed", "dropped", "retries", "last_error"):
            assert key in metrics, key
        assert metrics["buffered"] == 5
        assert metrics["flushed"] == 5
        assert metrics["dropped"] == 0

    async def test_idle_buffer_reports_no_loss_and_no_error(self) -> None:
        buf = WriteBuffer(make_sink())
        metrics = buf.metrics
        assert metrics["buffered"] == 0 and metrics["dropped"] == 0
        assert metrics["last_error"] is None

    def test_counters_are_reported_in_rows_not_batches(self) -> None:
        buf = WriteBuffer(make_sink(), capacity_rows=100, batch_rows=50)
        buf.submit("t", [1, 2, 3])
        assert buf.counters.buffered == 3


@settings(max_examples=40, deadline=None)
@given(
    sizes=st.lists(st.integers(min_value=1, max_value=20), min_size=1, max_size=25),
    capacity=st.integers(min_value=5, max_value=60),
)
def test_row_conservation_holds_for_any_submission_sequence(sizes: list[int], capacity: int) -> None:
    """任何提交序列下：接收 = 已刷 + 已丢 + 仍在队。丢数据必须可被算出来。"""
    sink = make_sink()

    async def scenario() -> None:
        buf = WriteBuffer(sink, capacity_rows=capacity, batch_rows=max(1, capacity // 3))
        submitted = 0
        accepted = 0
        for i, n in enumerate(sizes):
            submitted += n
            accepted += buf.submit(f"k{i % 3}", list(range(n)))
        await buf.drain()
        # 守恒律按"提交总量"计：入队后被驱逐的行与提交时被拒收的行都进 dropped，
        # 任何一行都不许凭空消失。注意 buffered 记的是"入队过"的行，
        # 因此它不等于 flushed + depth —— 入队后被驱逐的那部分已计入 dropped。
        assert submitted == buf.counters.flushed + buf.counters.dropped + buf.depth
        assert accepted == buf.counters.buffered
        assert buf.counters.flushed == sink.rows
        assert buf.depth <= capacity

    asyncio.run(scenario())
