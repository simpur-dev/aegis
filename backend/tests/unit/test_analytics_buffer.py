"""`BoundedBatcher` 边界测试：热路径同步不阻塞、溢出按策略丢弃、失败批次保序回灌、关停限时排空。

分析缓冲是"链路永不因分析后端抖动而崩溃"的落点，所以这里守的四条不变式：
1. `offer()` 同步返回，慢 `send` 不能把它拖住（弱网/慢盘时摄取回路必须继续跑）；
2. 行数守恒：`accepted == inserted + dropped_overflow + dropped_closed + buffered`；
3. 失败批次回灌**队首且保序**，重试成功后时序不乱；
4. 只有显式 `flush()` 抛错，后台循环吞错但保留数据与计数。
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Sequence
from typing import Any

import pytest

from aegis.analytics.buffer import (
    BatchingPort,
    BoundedBatcher,
    BufferCounters,
)


class SendRecorder:
    """可编排的 send 替身：记录每批内容，可按次序注入失败与耗时。"""

    def __init__(self, *, fail_times: int = 0, delay: float = 0.0, error: Exception | None = None, written: int | None = None) -> None:
        self.batches: list[list[Any]] = []
        self.calls = 0
        self._fail_times = fail_times
        self._delay = delay
        self._error = error or RuntimeError("sink 不可达")
        self._written = written

    async def __call__(self, batch: Sequence[Any]) -> int:
        self.calls += 1
        self.batches.append(list(batch))
        if self._delay:
            await asyncio.sleep(self._delay)
        if self.calls <= self._fail_times:
            raise self._error
        return len(batch) if self._written is None else self._written


def make(
    send: Any,
    *,
    max_batch: int = 4,
    buffer_limit: int = 16,
    flush_interval: float = 0.02,
    drop_policy: str = "oldest",
) -> BoundedBatcher[str]:
    return BoundedBatcher(
        send,
        name="t",
        max_batch=max_batch,
        buffer_limit=buffer_limit,
        flush_interval=flush_interval,
        drop_policy=drop_policy,  # type: ignore[arg-type]
    )


def conserve(batch: BoundedBatcher[str], offered: int) -> None:
    """行数守恒：任何时刻，落库 + 丢弃 + 仍在缓冲 = 受理总数。"""
    c = batch.counters
    assert offered == c.inserted + c.dropped_overflow + c.dropped_closed + batch.buffered


# --------------------------------------------------------------------- 构造校验


class TestConstruction:
    @pytest.mark.parametrize("bad", [0, -1])
    def test_max_batch_must_be_positive(self, bad: int) -> None:
        with pytest.raises(ValueError, match="max_batch"):
            BoundedBatcher(SendRecorder(), max_batch=bad)

    def test_buffer_limit_must_not_underflow_max_batch(self) -> None:
        """缓冲比批次还小会让"满批即刷"永远凑不齐，属配置错误而非运行时状态。"""
        with pytest.raises(ValueError, match="buffer_limit"):
            BoundedBatcher(SendRecorder(), max_batch=8, buffer_limit=4)

    @pytest.mark.parametrize("bad", [0.0, -1.0])
    def test_flush_interval_must_be_positive(self, bad: float) -> None:
        with pytest.raises(ValueError, match="flush_interval"):
            BoundedBatcher(SendRecorder(), flush_interval=bad)

    def test_unknown_drop_policy_rejected(self) -> None:
        with pytest.raises(ValueError, match="丢弃策略"):
            BoundedBatcher(SendRecorder(), drop_policy="random")  # type: ignore[arg-type]

    def test_defaults_match_module_documentation(self) -> None:
        batch = BoundedBatcher(SendRecorder())
        assert (batch.max_batch, batch.buffer_limit) == (1_000, 20_000)
        assert batch.flush_interval == 5.0

    def test_implements_batching_port(self) -> None:
        assert isinstance(BoundedBatcher(SendRecorder()), BatchingPort)


# --------------------------------------------------------------------- offer 热路径


class TestOffer:
    def test_offer_is_synchronous_and_returns_accepted_count(self) -> None:
        recorder = SendRecorder()
        batch = make(recorder)
        accepted = batch.offer(["a", "b", "c"])
        assert accepted == 3
        assert batch.buffered == 3
        assert recorder.calls == 0

    def test_empty_offer_is_free(self) -> None:
        recorder = SendRecorder()
        batch = make(recorder)
        assert batch.offer([]) == 0
        assert batch.counters.accepted == 0

    def test_offer_never_raises_even_when_sink_is_dead(self) -> None:
        """受理路径永不动异常：后端死了也只入队，错误留给 flush/统计。"""
        batch = make(SendRecorder(fail_times=99))
        assert batch.offer(["a"]) == 1

    def test_hot_path_stays_non_blocking_with_a_slow_sink(self) -> None:
        """慢落库（弱网）不得拖住 offer：这是"链路不阻塞"的唯一可验证形式。"""

        async def slow_send(batch: Sequence[str]) -> int:
            await asyncio.sleep(0.5)
            return len(batch)

        async def drive() -> None:
            batch = BoundedBatcher(slow_send, max_batch=2, buffer_limit=8, flush_interval=0.01)
            await batch.start()
            started = time.perf_counter()
            for _ in range(5):
                batch.offer(["r1", "r2"])
            elapsed = time.perf_counter() - started
            assert elapsed < 0.05, f"offer 被落库拖住了 {elapsed:.3f}s"
            assert batch.buffered == 8
            conserve(batch, 10)
            await batch.aclose(0)

        asyncio.run(drive())

    @pytest.mark.parametrize("policy", ["oldest", "newest"])
    def test_buffer_never_exceeds_limit(self, policy: str) -> None:
        batch = make(SendRecorder(), max_batch=2, buffer_limit=5, drop_policy=policy)
        for i in range(20):
            batch.offer([f"r{i}"])
        assert batch.buffered <= 5
        conserve(batch, 20)


class TestDropPolicy:
    """溢出策略边界。构造约束 `buffer_limit >= max_batch` 是模块自己的不变式（缓冲比一批还小就永远凑不齐批次），
    所以下面都用 max_batch == buffer_limit 来构造"恰好一批"的裁剪场景。"""

    def test_oldest_keeps_newest_rows(self) -> None:
        recorder = SendRecorder()
        batch = make(recorder, max_batch=3, buffer_limit=3)
        accepted = batch.offer(["r0", "r1", "r2", "r3", "r4"])
        assert accepted == 3
        assert recorder.batches == []
        asyncio.run(batch.flush())
        assert recorder.batches[0] == ["r2", "r3", "r4"]
        assert batch.counters.dropped_overflow == 2
        conserve(batch, 5)

    def test_oldest_evicts_existing_head_before_new_arrivals(self) -> None:
        recorder = SendRecorder()
        batch = make(recorder, max_batch=3, buffer_limit=3)
        batch.offer(["a", "b"])
        batch.offer(["c", "d"])
        asyncio.run(batch.flush())
        assert recorder.batches[0] == ["b", "c", "d"]
        assert batch.counters.dropped_overflow == 1

    def test_newest_drops_the_tail_of_the_incoming_batch(self) -> None:
        recorder = SendRecorder()
        batch = make(recorder, max_batch=3, buffer_limit=3, drop_policy="newest")
        accepted = batch.offer(["r0", "r1", "r2", "r3", "r4"])
        assert accepted == 3
        asyncio.run(batch.flush())
        assert recorder.batches[0] == ["r0", "r1", "r2"]
        assert batch.counters.dropped_overflow == 2

    def test_newest_keeps_history_when_full(self) -> None:
        recorder = SendRecorder()
        batch = make(recorder, max_batch=2, buffer_limit=2, drop_policy="newest")
        batch.offer(["a", "b"])
        assert batch.offer(["c"]) == 0
        asyncio.run(batch.flush())
        assert recorder.batches[0] == ["a", "b"]
        assert batch.counters.dropped_overflow == 1

    def test_loss_rate_is_reported(self) -> None:
        counters = BufferCounters(accepted=10, dropped_overflow=3, dropped_closed=2)
        assert counters.loss_rate() == pytest.approx(0.5)
        assert BufferCounters().loss_rate() == 0.0

    def test_counters_dict_is_flat_int_map(self) -> None:
        payload = BufferCounters().as_dict()
        assert set(payload) == {
            "accepted",
            "inserted",
            "batches",
            "failures",
            "flushes",
            "dropped_overflow",
            "dropped_closed",
            "requeued",
        }
        assert all(isinstance(v, int) for v in payload.values())


# --------------------------------------------------------------------- 批次与刷写


class TestFlush:
    def test_flush_splits_by_max_batch_in_order(self) -> None:
        recorder = SendRecorder()
        batch = make(recorder, max_batch=3, buffer_limit=16)
        batch.offer([str(i) for i in range(7)])
        asyncio.run(batch.flush())
        assert [len(b) for b in recorder.batches] == [3, 3, 1]
        assert [item for b in recorder.batches for item in b] == [str(i) for i in range(7)]
        assert batch.counters.batches == 3
        assert batch.counters.inserted == 7
        assert batch.counters.flushes == 1
        assert batch.buffered == 0

    def test_flush_on_empty_buffer_is_a_noop_for_the_sink(self) -> None:
        recorder = SendRecorder()
        batch = make(recorder)
        asyncio.run(batch.flush())
        assert recorder.calls == 0
        assert batch.counters.flushes == 1

    def test_inserted_follows_send_return_value(self) -> None:
        """账本按 send 的**实际返回值**记账：后端只落了 2 行就不能记 5 行。"""
        recorder = SendRecorder(written=2)
        batch = make(recorder, max_batch=5)
        batch.offer(["a", "b", "c", "d", "e"])
        asyncio.run(batch.flush())
        assert batch.counters.inserted == 2

    def test_explicit_flush_surfaces_sink_error(self) -> None:
        recorder = SendRecorder(fail_times=1)
        batch = make(recorder)
        batch.offer(["a", "b"])
        with pytest.raises(RuntimeError, match="sink 不可达"):
            asyncio.run(batch.flush())
        assert batch.counters.failures == 1
        assert batch.last_error is not None
        assert batch.buffered == 2
        conserve(batch, 2)

    def test_failed_batch_is_requeued_at_the_head_in_order(self) -> None:
        recorder = SendRecorder(fail_times=1)
        batch = make(recorder, max_batch=2, buffer_limit=8)
        batch.offer(["a", "b", "c"])
        with pytest.raises(RuntimeError):
            asyncio.run(batch.flush())
        asyncio.run(batch.flush())
        assert recorder.batches[0] == ["a", "b"]
        assert recorder.batches[1] == ["a", "b"]
        assert recorder.batches[2] == ["c"]
        assert batch.buffered == 0
        conserve(batch, 3)

    def test_requeue_overflow_is_counted_as_dropped_not_silently_lost(self) -> None:
        recorder = SendRecorder(fail_times=99)
        batch = make(recorder, max_batch=2, buffer_limit=2)
        batch.offer(["a", "b"])
        with pytest.raises(RuntimeError):
            asyncio.run(batch.flush())
        # 缓冲已被后台/前次腾空后再灌：失败批次要么回灌要么计入丢弃，二者必居其一。
        assert batch.buffered + batch.counters.dropped_overflow >= 2
        conserve(batch, 2)

    def test_flush_lock_serializes_concurrent_drains(self) -> None:
        """并发 flush 不得交叉取批：两条 drain 各自看到的批次不应重复计数同一行。"""
        recorder = SendRecorder(delay=0.01)
        batch = make(recorder, max_batch=2, buffer_limit=32)
        batch.offer([str(i) for i in range(8)])

        async def drive() -> None:
            await asyncio.gather(batch.flush(), batch.flush())

        asyncio.run(drive())
        flat = [item for b in recorder.batches for item in b]
        assert sorted(flat) == sorted(str(i) for i in range(8))
        assert len(flat) == 8
        assert batch.counters.inserted == 8


# --------------------------------------------------------------------- 后台循环


class TestBackgroundLoop:
    def test_kick_flushes_once_batch_is_full(self) -> None:
        recorder = SendRecorder(delay=0.005)
        batch = make(recorder, max_batch=2, buffer_limit=16, flush_interval=30.0)

        async def drive() -> None:
            await batch.start()
            batch.offer(["a", "b"])
            for _ in range(200):
                if batch.counters.inserted == 2:
                    break
                await asyncio.sleep(0.005)
            await batch.aclose(100)

        asyncio.run(drive())
        assert batch.counters.inserted == 2
        assert recorder.calls >= 1

    def test_interval_flushes_a_partial_batch(self) -> None:
        recorder = SendRecorder()
        batch = make(recorder, max_batch=16, buffer_limit=16, flush_interval=0.01)

        async def drive() -> None:
            await batch.start()
            batch.offer(["a"])
            for _ in range(200):
                if batch.counters.inserted == 1:
                    break
                await asyncio.sleep(0.005)
            await batch.aclose(100)

        asyncio.run(drive())
        assert batch.counters.inserted == 1
        assert batch.buffered == 0

    def test_background_failure_keeps_data_and_does_not_raise(self) -> None:
        """后台吞错但保留行：错误只能进日志与 stats，绝不能冒到摄取回路。"""
        recorder = SendRecorder(fail_times=99)
        batch = make(recorder, max_batch=1, buffer_limit=16, flush_interval=0.01)

        async def drive() -> None:
            await batch.start()
            batch.offer(["a", "b"])
            await asyncio.sleep(0.08)

        asyncio.run(drive())
        assert recorder.calls >= 1
        assert batch.buffered == 2
        assert batch.counters.failures >= 1
        conserve(batch, 2)

    def test_start_is_idempotent(self) -> None:
        batch = make(SendRecorder())

        async def drive() -> None:
            await batch.start()
            first = batch.running
            await batch.start()
            await batch.aclose(0)
            assert first is True

        asyncio.run(drive())

    def test_restart_after_close_is_rejected(self) -> None:
        """已关停的缓冲不能再启动：关停是终态，否则同一批行会被写两次。"""
        batch = make(SendRecorder())

        async def drive() -> None:
            await batch.start()
            await batch.aclose(0)
            with pytest.raises(RuntimeError, match="已关闭"):
                await batch.start()

        asyncio.run(drive())


# --------------------------------------------------------------------- 关停


class TestClose:
    def test_aclose_drains_within_grace(self) -> None:
        recorder = SendRecorder(delay=0.005)
        batch = make(recorder, max_batch=2, buffer_limit=16)
        batch.offer(["a", "b", "c"])

        async def drive() -> None:
            await batch.aclose(1_000)

        asyncio.run(drive())
        assert batch.counters.inserted == 3
        assert batch.buffered == 0
        assert batch.counters.dropped_closed == 0
        conserve(batch, 3)

    def test_zero_grace_accounts_unwritten_rows_as_dropped_closed(self) -> None:
        recorder = SendRecorder()
        batch = make(recorder)
        batch.offer(["a", "b"])

        async def drive() -> None:
            await batch.aclose(0)

        asyncio.run(drive())
        assert recorder.calls == 0
        assert batch.counters.dropped_closed == 2
        assert batch.buffered == 0
        conserve(batch, 2)

    def test_dead_sink_close_does_not_hang_and_accounts_loss(self) -> None:
        recorder = SendRecorder(fail_times=99)
        batch = make(recorder, max_batch=2, buffer_limit=16)
        batch.offer(["a", "b"])

        async def drive() -> None:
            await asyncio.wait_for(batch.aclose(50), timeout=2.0)

        asyncio.run(drive())
        assert batch.counters.dropped_closed == 2
        assert batch.buffered == 0
        conserve(batch, 2)

    def test_aclose_is_idempotent(self) -> None:
        recorder = SendRecorder()
        batch = make(recorder)
        batch.offer(["a"])

        async def drive() -> None:
            await batch.aclose(100)
            await batch.aclose(100)

        asyncio.run(drive())
        assert recorder.calls == 1
        assert batch.counters.dropped_closed == 0

    def test_offer_after_close_is_counted_and_refused(self) -> None:
        recorder = SendRecorder()
        batch = make(recorder)

        async def drive() -> None:
            await batch.aclose(0)
            return batch.offer(["a", "b"])

        accepted = asyncio.run(drive())
        assert accepted == 0
        assert batch.counters.accepted == 2
        assert batch.counters.dropped_closed == 2
        assert batch.buffered == 0
        conserve(batch, 2)
        assert batch.closing is True

    def test_close_marks_stats(self) -> None:
        batch = make(SendRecorder())

        async def drive() -> None:
            await batch.aclose(0)

        asyncio.run(drive())
        assert batch.stats()["running"] is False
        assert batch.stats()["drop_policy"] == "oldest"


# --------------------------------------------------------------------- 统计快照


class TestCancellability:
    """回归：后台 flusher 曾把 `CancelledError` 当业务异常吞掉，于是任务杀不掉、
    `asyncio.run()` 收尾与平台优雅退出都会在"有一批正在落库"时永久卡住。"""

    def test_flusher_task_dies_when_cancelled_mid_flight(self) -> None:
        async def slow_send(batch: Sequence[str]) -> int:
            await asyncio.sleep(0.5)
            return len(batch)

        async def drive() -> asyncio.Task[None]:
            batch = BoundedBatcher(slow_send, name="regression-flusher", max_batch=1, buffer_limit=4, flush_interval=30.0)
            await batch.start()
            batch.offer(["a", "b"])
            await asyncio.sleep(0.02)  # 让后台真正进入 in-flight 落库
            task = next(t for t in asyncio.all_tasks() if t.get_name() == "regression-flusher")
            task.cancel()
            _done, pending = await asyncio.wait({task}, timeout=1.0)
            assert not pending, "后台 flusher 吞掉了取消信号：该任务永远杀不掉"
            assert task.done()
            return task

        task = asyncio.run(drive())
        assert task.cancelled()


class TestStats:
    def test_stats_is_pure_memory_and_carries_counters(self) -> None:
        recorder = SendRecorder()
        batch = make(recorder, buffer_limit=6)
        batch.offer(["a", "b"])

        async def drive() -> dict[str, object]:
            await batch.flush()
            return batch.stats()

        snapshot = asyncio.run(drive())
        assert snapshot["buffered"] == 0
        assert snapshot["buffer_limit"] == 6
        assert snapshot["max_batch"] == 4
        assert snapshot["inserted"] == 2
        assert snapshot["accepted"] == 2
        assert snapshot["last_error"] is None
        assert snapshot["loss_rate"] == 0.0
        assert recorder.calls == 1

    def test_stats_reports_last_error_type_name(self) -> None:
        class SinkDown(Exception):
            pass

        recorder = SendRecorder(fail_times=1, error=SinkDown("x"))
        batch = make(recorder)
        batch.offer(["a"])

        async def drive() -> dict[str, object]:
            with pytest.raises(SinkDown):
                await batch.flush()
            return batch.stats()

        snapshot = asyncio.run(drive())
        assert snapshot["last_error"] == "SinkDown"

    def test_stats_never_touches_the_sink(self) -> None:
        recorder = SendRecorder()
        batch = make(recorder)
        batch.offer(["a"])
        for _ in range(50):
            batch.stats()
        assert recorder.calls == 0

    def test_row_conservation_under_mixed_overflow_and_failures(self) -> None:
        """把溢出与失败混在一起压一遍，守恒式仍必须成立——这是"没有静默丢数据"的证据。"""
        recorder = SendRecorder(fail_times=2)
        batch = make(recorder, max_batch=3, buffer_limit=6)
        for i in range(10):
            batch.offer([f"x{i}", f"y{i}"])
        offered = batch.counters.accepted
        with contextlib.suppress(RuntimeError):
            asyncio.run(batch.flush())
        assert offered == 20
        conserve(batch, offered)
        assert batch.buffered <= batch.buffer_limit
        assert batch.counters.failures >= 1
        # 失败行要么仍在缓冲等重试，要么已被计入丢弃，绝不允许凭空消失。
        assert batch.buffered + batch.counters.dropped_overflow + batch.counters.inserted == offered
