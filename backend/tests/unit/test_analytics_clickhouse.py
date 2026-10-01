"""ClickHouseSink 边界测试：write-behind 受理、列式批量落库、退避重试、关停与"绝不阻塞事件循环"。

`clickhouse-connect` 是同步 HTTP 客户端，本模块的全部价值就在于把它的阻塞包住。
因此这里最有分量的两条断言不是"写对了没"，而是：
1. `ingest()` 期间一次 insert 都不发生（否则弱网慢盘会直接拖垮摄取回路）；
2. insert 跑在工作线程上，事件循环期间能正常心跳（用 `time.sleep` 真实阻塞来证明）。
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from collections.abc import Sequence
from datetime import UTC, datetime

import pytest

from aegis.analytics.clickhouse_sink import ClickHouseSink, _backoff, connect_clickhouse, create_schema
from aegis.analytics.port import AnalyticsSink, AnalyticsSinkError, FactRow
from aegis.analytics.sql import DEFAULT_DATABASE, FACT_COLUMNS, FACT_TABLE

BASE = datetime(2026, 9, 30, 3, 0, tzinfo=UTC)


def fact(index: int, *, value: float = 1.0) -> FactRow:
    return FactRow(
        event_id=f"evt-{index}",
        kind="telemetry",
        observed_at=BASE,
        region_code="540121",
        station_id=f"ST-{index}",
        metric="rainfall_mm",
        value=value,
        lat=29.65 + index * 1e-4,
        lon=91.12,
    )


class FakeClient:
    """记录每次 insert 的入参与所在线程；可注入前 n 次失败与阻塞时长。"""

    def __init__(self, *, fail_times: int = 0, block: float = 0.0, close_error: Exception | None = None) -> None:
        self.inserts: list[dict[str, object]] = []
        self.columns: list[list[list[object]]] = []
        self.commands: list[str] = []
        self.threads: list[int] = []
        self.close_calls = 0
        self._fail_times = fail_times
        self._block = block
        self._close_error = close_error

    @property
    def insert_calls(self) -> int:
        return len(self.inserts)

    def insert(
        self,
        table: str,
        data: Sequence[Sequence[object]],
        *,
        column_names: Sequence[str] | None = None,
        database: str | None = None,
        column_oriented: bool = False,
    ) -> object:
        self.threads.append(threading.get_ident())
        self.inserts.append(
            {
                "table": table,
                "column_names": list(column_names or ()),
                "database": database,
                "column_oriented": column_oriented,
                "rows": max((len(c) for c in data), default=0),
            }
        )
        self.columns.append([list(column) for column in data])
        if self._block:
            time.sleep(self._block)  # 真阻塞：同步客户端不给 await 的机会
        if self.insert_calls <= self._fail_times:
            raise OSError("connection reset by peer")
        return None

    def command(self, query: str, *args: object, **kwargs: object) -> object:
        self.threads.append(threading.get_ident())
        self.commands.append(query)
        return None

    def close(self) -> None:
        self.threads.append(threading.get_ident())
        self.close_calls += 1
        if self._close_error is not None:
            raise self._close_error


def sink(client: FakeClient | None = None, **overrides: object) -> ClickHouseSink:
    options: dict[str, object] = {
        "max_batch": 3,
        "buffer_limit": 12,
        "flush_interval": 30.0,
        "backoff_base": 0.001,
        "backoff_cap": 0.002,
    }
    options.update(overrides)
    return ClickHouseSink(client or FakeClient(), **options)  # type: ignore[arg-type]


# --------------------------------------------------------------------- 构造校验


class TestConstruction:
    def test_max_attempts_must_be_at_least_one(self) -> None:
        with pytest.raises(ValueError, match="max_attempts"):
            ClickHouseSink(FakeClient(), max_attempts=0)

    @pytest.mark.parametrize(("base", "cap"), [(0.0, 1.0), (-1.0, 1.0), (0.5, 0.2)])
    def test_backoff_params_must_be_sane(self, base: float, cap: float) -> None:
        """cap < base 会让"封顶"变成放大，退避方向反了，属配置错误。"""
        with pytest.raises(ValueError, match="退避参数"):
            ClickHouseSink(FakeClient(), backoff_base=base, backoff_cap=cap)

    def test_buffer_limit_below_max_batch_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="buffer_limit"):
            ClickHouseSink(FakeClient(), max_batch=10, buffer_limit=4)

    def test_sink_satisfies_the_narrow_port(self) -> None:
        """端口是 `@runtime_checkable` 的：sink 换实现时这条断言就是"调用方不必改代码"的证据。"""
        assert isinstance(sink(), AnalyticsSink)

    def test_defaults_target_the_documented_database_and_table(self) -> None:
        instance = ClickHouseSink(FakeClient())
        assert (instance.database, instance.table) == (DEFAULT_DATABASE, FACT_TABLE)
        assert instance.columns == FACT_COLUMNS


class TestBackoffPureFunction:
    def test_sequence_is_exponential_then_capped(self) -> None:
        assert _backoff(1, 0.2, 5.0) == pytest.approx(0.2)
        assert _backoff(2, 0.2, 5.0) == pytest.approx(0.4)
        assert _backoff(3, 0.2, 5.0) == pytest.approx(0.8)
        assert _backoff(20, 0.2, 5.0) == 5.0

    def test_never_negative(self) -> None:
        assert _backoff(1, 0.01, 0.01) >= 0.0


# --------------------------------------------------------------------- 受理与落库


class TestIngestAndFlush:
    def test_ingest_performs_no_io(self) -> None:
        """write-behind 的定义：受理只入队。若这里出现了 insert，链路就被分析后端绑死了。"""
        client = FakeClient()
        instance = sink(client)

        async def drive() -> None:
            accepted = await instance.ingest([fact(i) for i in range(5)])
            assert accepted == 5
            assert client.insert_calls == 0
            assert instance.stats()["buffered"] == 5
            await instance.flush()

        asyncio.run(drive())
        assert client.insert_calls == 2

    def test_empty_ingest_is_free_and_starts_nothing(self) -> None:
        client = FakeClient()
        instance = sink(client)

        async def drive() -> None:
            assert await instance.ingest([]) == 0
            await instance.flush()

        asyncio.run(drive())
        assert client.insert_calls == 0

    def test_insert_payload_is_column_oriented_with_fact_columns(self) -> None:
        client = FakeClient()
        instance = sink(client, max_batch=8, buffer_limit=8)

        async def drive() -> None:
            await instance.ingest([fact(i) for i in range(4)])
            await instance.flush()

        asyncio.run(drive())
        assert len(client.inserts) == 1
        call = client.inserts[0]
        assert call["column_oriented"] is True
        assert call["column_names"] == list(FACT_COLUMNS)
        assert call["table"] == FACT_TABLE
        assert call["database"] == DEFAULT_DATABASE
        assert call["rows"] == 4

    def test_columns_are_transposed_from_row_tuples(self) -> None:
        """列式写入最容易静默错位的点：第 0 列必须是 event_id，第 8 列必须是 value。"""
        client = FakeClient()
        instance = sink(client)

        async def drive() -> None:
            await instance.ingest([fact(0, value=7.5), fact(1, value=8.5)])
            await instance.flush()

        asyncio.run(drive())
        columns = client.columns[0]
        assert len(columns) == len(FACT_COLUMNS)
        assert columns[FACT_COLUMNS.index("event_id")] == ["evt-0", "evt-1"]
        assert columns[FACT_COLUMNS.index("value")] == [7.5, 8.5]
        assert columns[FACT_COLUMNS.index("station_id")] == ["ST-0", "ST-1"]

    def test_flush_splits_by_max_batch(self) -> None:
        client = FakeClient()
        instance = sink(client, max_batch=3, buffer_limit=12)

        async def drive() -> None:
            await instance.ingest([fact(i) for i in range(7)])
            await instance.flush()

        asyncio.run(drive())
        assert [c["rows"] for c in client.inserts] == [3, 3, 1]

    def test_custom_database_and_table_are_passed_through(self) -> None:
        client = FakeClient()
        instance = sink(client, database="aegis_edge", table="fact_local")

        async def drive() -> None:
            await instance.ingest([fact(0)])
            await instance.flush()

        asyncio.run(drive())
        assert client.inserts[0]["database"] == "aegis_edge"
        assert client.inserts[0]["table"] == "fact_local"

    def test_background_flush_kicks_in_at_batch_threshold(self) -> None:
        client = FakeClient()
        instance = sink(client, max_batch=2, buffer_limit=4, flush_interval=0.01)

        async def drive() -> None:
            await instance.ingest([fact(0), fact(1)])
            for _ in range(100):
                if client.insert_calls:
                    break
                await asyncio.sleep(0.005)

        asyncio.run(drive())
        assert client.insert_calls >= 1
        assert instance.stats()["inserted"] == 2


# --------------------------------------------------------------------- 重试与降级


class TestRetry:
    def test_transient_failures_are_retried_within_the_budget(self) -> None:
        client = FakeClient(fail_times=2)
        instance = sink(client, max_attempts=3)

        async def drive() -> None:
            await instance.ingest([fact(0)])
            await instance.flush()

        asyncio.run(drive())
        assert client.insert_calls == 3
        assert instance.stats()["retries"] == 2
        assert instance.stats()["inserted"] == 1

    def test_exhausted_budget_raises_typed_error_and_keeps_rows(self) -> None:
        """重试预算用尽：抛类型化错误，但行必须回灌缓冲而不是消失——链路降级而非崩溃。"""
        client = FakeClient(fail_times=99)
        instance = sink(client, max_attempts=2)

        async def drive() -> None:
            await instance.ingest([fact(0), fact(1)])
            with pytest.raises(AnalyticsSinkError):
                await instance.flush()

        asyncio.run(drive())
        assert client.insert_calls == 2
        stats = instance.stats()
        assert stats["failures"] == 1
        assert stats["buffered"] == 2
        assert stats["last_error"] == "AnalyticsSinkError"
        assert stats["accepted"] == 2

    def test_max_attempts_one_means_no_retry(self) -> None:
        client = FakeClient(fail_times=99)
        instance = sink(client, max_attempts=1)

        async def drive() -> None:
            await instance.ingest([fact(0)])
            with pytest.raises(AnalyticsSinkError):
                await instance.flush()

        asyncio.run(drive())
        assert client.insert_calls == 1
        assert instance.stats()["retries"] == 0

    def test_error_detail_names_the_target_table(self) -> None:
        client = FakeClient(fail_times=99)
        instance = sink(client, max_attempts=1, database="db1", table="tb1")

        async def drive() -> AnalyticsSinkError:
            await instance.ingest([fact(0)])
            with pytest.raises(AnalyticsSinkError) as caught:
                await instance.flush()
            return caught.value

        error = asyncio.run(drive())
        detail = error.detail if isinstance(error.detail, dict) else {}
        assert detail.get("table") == "db1.tb1"

    def test_sink_error_is_retriable_by_default(self) -> None:
        assert AnalyticsSinkError("x").retryable is True


# --------------------------------------------------------------------- 事件循环不被阻塞


class TestNonBlocking:
    def test_insert_runs_off_the_event_loop(self) -> None:
        """同步客户端若直接调用会占住循环：只统计 flush 窗口内的心跳跳数来证明循环没被按住。

        Windows 默认定时器精度约 15.6ms，所以这里不比绝对次数，而比"落库期间是否仍在跳"：
        若 insert 真跑在事件循环上，0.25s 的 sleep 期间心跳必然是 0。
        """
        client = FakeClient(block=0.25)
        instance = sink(client, max_batch=8, buffer_limit=8)
        ticks = 0

        async def beat() -> None:
            nonlocal ticks
            while True:
                await asyncio.sleep(0.005)
                ticks += 1

        async def drive() -> tuple[int, int]:
            pacer = asyncio.create_task(beat())
            try:
                await instance.ingest([fact(i) for i in range(3)])
                before = ticks
                await instance.flush()
                return before, ticks
            finally:
                pacer.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await pacer

        before, after = asyncio.run(drive())
        assert after - before >= 3, f"落库期间事件循环只跳了 {after - before} 次，疑似被同步客户端按住"
        assert set(client.threads) != {threading.get_ident()}

    def test_apply_schema_runs_off_the_event_loop(self) -> None:
        client = FakeClient()
        instance = sink(client)
        main = threading.get_ident()

        async def drive() -> tuple[str, ...]:
            return await instance.apply_schema()

        statements = asyncio.run(drive())
        assert len(statements) == 4
        assert client.commands == list(statements)
        assert all(t != main for t in client.threads)


# --------------------------------------------------------------------- 建表与关停


class TestSchemaAndClose:
    def test_create_schema_executes_ddl_in_order(self) -> None:
        client = FakeClient()
        statements = create_schema(client, database="edg")
        assert client.commands == list(statements)
        assert client.commands[0].startswith("CREATE DATABASE")
        assert "edg" in client.commands[1]

    def test_close_flushes_then_closes_client(self) -> None:
        client = FakeClient()
        instance = sink(client)

        async def drive() -> None:
            await instance.ingest([fact(0), fact(1)])
            await instance.close(1_000)

        asyncio.run(drive())
        assert instance.stats()["inserted"] == 2
        assert client.close_calls == 1
        assert instance.stats()["closed"] is True

    def test_close_is_idempotent(self) -> None:
        client = FakeClient()
        instance = sink(client)

        async def drive() -> None:
            await instance.close(10)
            await instance.close(10)

        asyncio.run(drive())
        assert client.close_calls == 1

    def test_close_error_is_recorded_not_raised(self) -> None:
        """关停路径的异常不能冒泡：优雅退出时抛错会把整条链路的收尾带崩。"""
        client = FakeClient(close_error=RuntimeError("socket 已死"))
        instance = sink(client)

        async def drive() -> None:
            await instance.close(10)

        asyncio.run(drive())
        assert instance.stats()["close_error"] == "RuntimeError"

    def test_zero_grace_accounts_uninserted_rows(self) -> None:
        client = FakeClient()
        instance = sink(client)

        async def drive() -> None:
            await instance.ingest([fact(0), fact(1), fact(2)])
            await instance.close(0)

        asyncio.run(drive())
        stats = instance.stats()
        assert stats["inserted"] == 0
        assert stats["dropped_closed"] == 3
        assert stats["buffered"] == 0
        assert client.close_calls == 1

    def test_ingest_after_close_is_refused_and_counted(self) -> None:
        client = FakeClient()
        instance = sink(client)

        async def drive() -> int:
            await instance.close(10)
            return await instance.ingest([fact(0)])

        accepted = asyncio.run(drive())
        assert accepted == 0
        assert client.insert_calls == 0
        assert instance.stats()["dropped_closed"] == 1

    def test_overflow_drops_oldest_and_reports_loss_rate(self) -> None:
        client = FakeClient()
        instance = sink(client, max_batch=4, buffer_limit=4, drop_policy="oldest")

        async def drive() -> None:
            for i in range(10):
                await instance.ingest([fact(i)])

        asyncio.run(drive())
        stats = instance.stats()
        assert stats["accepted"] == 10
        assert stats["dropped_overflow"] == 6
        assert stats["loss_rate"] == pytest.approx(0.6)


class TestStatsPurity:
    def test_stats_never_touches_the_client(self) -> None:
        client = FakeClient()
        instance = sink(client)

        async def drive() -> None:
            await instance.ingest([fact(0)])
            for _ in range(50):
                instance.stats()

        asyncio.run(drive())
        assert client.insert_calls == 0
        assert client.commands == []

    def test_stats_carries_backend_identity_and_counters(self) -> None:
        instance = sink(FakeClient(), database="db9")

        async def drive() -> dict[str, object]:
            await instance.ingest([fact(0)])
            await instance.flush()
            return instance.stats()

        stats = asyncio.run(drive())
        assert stats["backend"] == "clickhouse"
        assert stats["database"] == "db9"
        assert stats["max_attempts"] == 3
        assert stats["inserted"] == 1
        assert stats["running"] is True


class TestDriverMissing:
    def test_connect_without_driver_is_a_typed_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """没装 clickhouse-connect 的部署必须拿到可诊断的类型化错误，而不是 ImportError 穿透装配层。

        延迟 import 是本模块的既有约定（OLAP 是可选子系统），所以这里替换的是 sink 模块里的
        `importlib` 名字本身，而不是全局 stdlib 模块对象——不污染其它测试。
        """
        import aegis.analytics.clickhouse_sink as module

        class MissingDriver:
            @staticmethod
            def import_module(name: str) -> object:
                raise ImportError(f"no module named {name}")

        monkeypatch.setattr(module, "importlib", MissingDriver())
        with pytest.raises(AnalyticsSinkError, match="未安装 clickhouse-connect") as caught:
            connect_clickhouse()
        assert caught.value.retryable is False
