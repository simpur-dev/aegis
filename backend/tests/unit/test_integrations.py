"""装配层测试：可选子系统"接得上、退得掉、看得见"。

这里最有价值的不是 happy path，而是三条降级腿各自都真的成立：
1. 后端连不上 → 平台照常启动，读视图照常工作，故障进状态而不是进异常；
2. 分析旁路坏掉 → 链路语义与耗时不受影响（旁路只入队、异常自己吞）；
3. 状态接口必须能区分"没启用"和"启用了但降级"，否则运维只能靠猜。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from aegis.analytics.port import AnalyticsSinkError, FactRow
from aegis.config import Settings
from aegis.container import create_container
from aegis.domain.messages import TelemetryReading
from aegis.integrations import (
    AnalyticsRecorder,
    IntegrationState,
    build_analytics,
    build_store,
    chain_facts,
    reading_facts,
    start_store,
    stop_store,
)
from aegis.pipeline.chain import ChainResult, StageResult
from aegis.storage.store import PlatformStore

PG_DSN = "postgresql://aegis:sup3rs3cr3t@127.0.0.1:1/aegis"


def pg_settings(**overrides: Any) -> Settings:
    return base_settings(store_backend="postgres", pg_dsn=PG_DSN, **overrides)


def base_settings(**overrides: Any) -> Settings:
    payload: dict[str, Any] = {
        "env": "test",
        "bus_backend": "memory",
        "simulator_enabled": False,
        "delivery_mode": "mock",
        "store_backend": "memory",
        "analytics_backend": "off",
    }
    payload.update(overrides)
    return Settings(**payload)


class FakeSink:
    """替身 sink：只记录入队的行，永不触碰 I/O。"""

    def __init__(self, *, fail: bool = False) -> None:
        self.batches: list[list[FactRow]] = []
        self.flushed = 0
        self.closed_with: int | None = None
        self.schema_applied = False
        self.fail = fail

    @property
    def calls(self) -> int:
        return len(self.batches)

    async def ingest(self, rows: Any) -> int:
        if self.fail:
            raise RuntimeError("sink 炸了")
        self.batches.append(list(rows))
        return len(rows)

    async def flush(self) -> None:
        self.flushed += 1

    async def close(self, grace_ms: int) -> None:
        self.closed_with = grace_ms

    async def apply_schema(self) -> tuple[str, ...]:
        self.schema_applied = True
        return ()

    def stats(self) -> dict[str, object]:
        return {"buffered": 0, "inserted": sum(len(b) for b in self.batches), "dropped_overflow": 0, "dropped_closed": 0, "loss_rate": 0.0}


def warning_chain() -> ChainResult:
    from aegis.domain.enums import RiskLevel
    from aegis.domain.messages import WarningRecord

    record = WarningRecord(
        event_id="evt_1",
        trace_id="trc_0000000000000001",
        hazard_type="debris_flow",
        region_codes=["540121", "540122"],
        risk_level=RiskLevel.RED,
        title_zh="泥石流预警",
        body_zh="正文",
        generated_at="2026-09-30T03:00:00+00:00",
    )
    return ChainResult(
        trace_id="trc_0000000000000001",
        event_id="evt_1",
        stages=[StageResult(name="assess", mode="local", ok=True, latency_ms=12.5)],
        warning=record,
    )


# --------------------------------------------------------------------- 存储装配


class TestBuildStore:
    def test_memory_backend_keeps_the_read_model_only(self) -> None:
        bundle = build_store(base_settings())
        assert isinstance(bundle.store, PlatformStore)
        assert bundle.durability is None
        assert bundle.state.driver == "memory"
        assert bundle.state.enabled is True

    def test_postgres_backend_wraps_the_same_read_model(self) -> None:
        """关键不变式：Postgres 不替换读视图，而是在读视图前挂落库。

        门面（`_TelemetryFacade`）不是读视图本身，所以这里断言的是**共享同一份数据**：
        写进 `bundle.store.telemetry` 的读数必须能从 `read.telemetry` 直接读到。
        """
        read = PlatformStore()
        bundle = build_store(pg_settings(), read_model=read)
        assert bundle.durability is bundle.store

        reading = TelemetryReading(
            station_id="ST-1",
            metric="rainfall_mm",
            value=7.0,
            unit="mm/h",
            region_code="540121",
            observed_at="2026-09-30T03:00:00+00:00",
            ingested_at="2026-09-30T03:00:00+00:00",
        )

        async def drive() -> int:
            await bundle.store.telemetry.add([reading])
            return len(read.telemetry.query(region_code="540121"))

        assert asyncio.run(drive()) == 1

    def test_state_never_leaks_the_dsn_password(self) -> None:
        bundle = build_store(pg_settings())
        dumped = str(bundle.state.as_dict())
        assert "sup3rs3cr3t" not in dumped
        assert "postgresql://" not in dumped

    def test_construction_does_not_connect(self) -> None:
        """建 store 不能碰网络：装配发生在事件循环启动前，一次阻塞就拖住整个进程。"""
        bundle = build_store(pg_settings())
        assert bundle.durability.connected is False


class TestStartStore:
    def test_memory_start_is_a_noop_and_reports_the_same_state(self) -> None:
        bundle = build_store(base_settings())
        assert start_store_sync(bundle, base_settings()) == bundle.state

    def test_unreachable_database_degrades_instead_of_raising(self) -> None:
        """连不上库必须照常起平台：高原现场常常是先起平台、后修数据库。"""
        bundle = build_store(pg_settings())
        state = start_store_sync(bundle, pg_settings())
        assert state.enabled is True
        assert state.detail["degraded"] == "read_model_only"
        assert "ConnectionFailed" in str(state.detail["error"])

    def test_reads_still_work_after_degradation(self) -> None:
        bundle = build_store(pg_settings())
        start_store_sync(bundle, pg_settings())
        reading = TelemetryReading(
            station_id="ST-1",
            metric="rainfall_mm",
            value=9.0,
            unit="mm/h",
            region_code="540121",
            observed_at="2026-09-30T03:00:00+00:00",
            ingested_at="2026-09-30T03:00:10+00:00",
        )

        async def drive() -> int:
            await bundle.store.telemetry.add([reading])
            return len(bundle.store.telemetry.query(region_code="540121"))

        assert asyncio.run(drive()) == 1

    def test_stop_store_tolerates_a_never_connected_pool(self) -> None:
        bundle = build_store(pg_settings())
        asyncio.run(stop_store(bundle, grace_ms=50))

    def test_stop_store_on_memory_backend_is_free(self) -> None:
        bundle = build_store(base_settings())
        asyncio.run(stop_store(bundle))


def start_store_sync(bundle: Any, settings: Settings) -> IntegrationState:
    """同步跑一次启动：这些用例只覆盖"连不上库"的分支，不需要真实事件循环编排。"""
    return asyncio.run(start_store(bundle, settings))


# --------------------------------------------------------------------- 事实扇出


class TestFactMapping:
    def test_chain_facts_cover_warning_regions_and_stage_latency(self) -> None:
        rows = chain_facts(warning_chain())
        kinds = [row.kind for row in rows]
        assert kinds.count("hazard") == 2  # 两个受影响区域各一条
        assert kinds.count("latency") == 1
        hazard = next(row for row in rows if row.kind == "hazard")
        assert hazard.hazard_type == "debris_flow"
        assert hazard.risk_level == 1
        latency = next(row for row in rows if row.kind == "latency")
        assert latency.metric == "stage_assess_ms"
        assert latency.latency_ms == pytest.approx(12.5)

    def test_chain_without_warning_emits_only_latency(self) -> None:
        bare = ChainResult(
            trace_id="trc_0000000000000002",
            event_id="evt_2",
            stages=[StageResult(name="plan", mode="local", ok=True, latency_ms=3.0)],
        )
        assert [row.kind for row in chain_facts(bare)] == ["latency"]

    def test_reading_facts_keep_the_idempotency_key(self) -> None:
        reading = TelemetryReading(
            station_id="ST-1",
            metric="mud_level_cm",
            value=42.0,
            unit="cm",
            region_code="540121",
            observed_at="2026-09-30T03:00:00+00:00",
            ingested_at="2026-09-30T03:00:05+00:00",
        )
        (row,) = reading_facts([reading])
        assert row.event_id == f"ST-1:mud_level_cm:{datetime(2026, 9, 30, 3, 0, tzinfo=UTC).isoformat(timespec='milliseconds')}"
        assert row.value == pytest.approx(42.0)


class TestAnalyticsRecorder:
    def test_records_chain_into_the_sink_without_awaiting_io(self) -> None:
        sink = FakeSink()
        recorder = AnalyticsRecorder(sink, driver="fake")

        async def drive() -> None:
            await recorder.record_chain(warning_chain())

        asyncio.run(drive())
        assert sink.calls == 1
        assert recorder.accepted == 3
        assert recorder.errors == 0

    def test_empty_result_does_not_disturb_the_sink(self) -> None:
        sink = FakeSink()
        recorder = AnalyticsRecorder(sink, driver="fake")
        asyncio.run(recorder.record_chain(ChainResult(trace_id="trc_3", event_id="e3")))
        assert sink.calls == 0

    def test_sink_failure_is_counted_and_never_propagates(self) -> None:
        """旁路坏了不能把链路带崩：这是"智能体优先·平台降级"在分析侧的同一纪律。"""
        recorder = AnalyticsRecorder(FakeSink(fail=True), driver="fake")

        async def drive() -> None:
            await recorder.record_chain(warning_chain())
            await recorder.record_readings([])

        asyncio.run(drive())
        assert recorder.errors == 1
        assert "RuntimeError" in str(recorder.last_error)

    def test_state_exposes_counters_and_sink_ledger(self) -> None:
        sink = FakeSink()
        recorder = AnalyticsRecorder(sink, driver="duckdb")
        asyncio.run(recorder.record_chain(warning_chain()))
        state = recorder.state()
        assert state.name == "analytics"
        assert state.enabled is True
        assert state.detail["accepted"] == 3
        assert state.detail["inserted"] == 3

    def test_state_survives_a_broken_stats_method(self) -> None:
        class Broken(FakeSink):
            def stats(self) -> dict[str, object]:
                raise RuntimeError("stats 不可读")

        recorder = AnalyticsRecorder(Broken(), driver="fake")
        assert recorder.state().detail["stats_error"] == "RuntimeError"

    def test_apply_schema_only_when_configured(self) -> None:
        sink = FakeSink()
        off = AnalyticsRecorder(sink, driver="fake", settings=base_settings(analytics_apply_schema=False))
        on = AnalyticsRecorder(sink, driver="fake", settings=base_settings(analytics_apply_schema=True))
        asyncio.run(off.open())
        assert sink.schema_applied is False
        asyncio.run(on.open())
        assert sink.schema_applied is True

    def test_close_flushes_then_closes_with_grace(self) -> None:
        sink = FakeSink()
        recorder = AnalyticsRecorder(sink, driver="fake", settings=base_settings(analytics_close_grace_ms=777))
        asyncio.run(recorder.close())
        assert sink.flushed == 1
        assert sink.closed_with == 777


# --------------------------------------------------------------------- 分析装配


class TestBuildAnalytics:
    def test_off_backend_mounts_nothing(self) -> None:
        sink, state = build_analytics(base_settings(analytics_backend="off"))
        assert sink is None
        assert state.enabled is False
        assert state.driver == "off"

    def test_duckdb_backend_opens_a_single_file_warehouse(self, tmp_path: Path) -> None:
        sink, state = build_analytics(base_settings(analytics_backend="duckdb", duckdb_path=str(tmp_path / "edge.duckdb")))
        assert state.driver == "duckdb"
        assert state.detail["path"] == str(tmp_path / "edge.duckdb")

        async def drive() -> None:
            await sink.ingest(reading_facts([]))  # 空批：只验证对象可用，不落盘
            await sink.close(0)

        asyncio.run(drive())

    def test_clickhouse_unavailable_is_reported_not_raised(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import aegis.analytics.clickhouse_sink as module

        def explode(**_kwargs: Any) -> Any:
            raise AnalyticsSinkError("ClickHouse 不可达")

        monkeypatch.setattr(module, "connect_clickhouse", explode)
        sink, state = build_analytics(base_settings(analytics_backend="clickhouse"))
        assert sink is None
        assert state.enabled is False
        assert "不可达" in str(state.detail["error"])


# --------------------------------------------------------------------- 容器接线


class TestContainerWiring:
    def test_default_container_has_no_optional_subsystems(self) -> None:
        ctn = create_container(base_settings(), with_simulator=False)
        assert ctn.analytics is None
        assert ctn.bundle.durability is None
        assert ctn.mqtt is None
        assert ctn.weather is None
        # 按名字断言而不是按位置：状态列表是对外契约，加一条腿不该把既有断言整体挪位
        status = {state.name: state for state in ctn.integration_status()}
        assert set(status) == {"store", "analytics", "knowledge", "retrieval", "mqtt", "weather", "tracing"}
        assert (status["store"].enabled, status["store"].driver) == (True, "memory")
        assert status["analytics"].enabled is False
        assert status["knowledge"].enabled is True
        assert (status["retrieval"].enabled, status["retrieval"].driver) == (False, "off")
        assert (status["mqtt"].enabled, status["mqtt"].driver) == (False, "off")
        assert (status["weather"].enabled, status["weather"].driver) == (False, "off")
        # 没配 OTLP 端点时链路追踪只留本地：这一行必须说真话，否则"接了 Jaeger"是假的
        assert (status["tracing"].enabled, status["tracing"].driver) == (False, "local")

    def test_analytics_sink_receives_chain_and_reading_facts(self, tmp_path: Path) -> None:
        """容器里的扇出真的接上了：链路落库与分析入队共用同一个 on_result 出口。"""
        ctn = create_container(
            base_settings(analytics_backend="duckdb", duckdb_path=str(tmp_path / "wired.duckdb")),
            with_simulator=False,
        )
        assert ctn.analytics is not None
        assert ctn.analytics.state().driver == "duckdb"
        assert {state.name: state for state in ctn.integration_status()}["analytics"].driver == "duckdb"

    def test_store_degradation_is_visible_through_the_container(self) -> None:
        ctn = create_container(pg_settings(), with_simulator=False)
        assert ctn.bundle.durability is not None
        state = start_store_sync(ctn.bundle, ctn.settings)
        assert ctn.settings.store_backend == "postgres"
        assert state.detail["degraded"] == "read_model_only"

    def test_container_store_satisfies_the_read_faces_the_api_uses(self) -> None:
        ctn = create_container(base_settings(), with_simulator=False)
        for name in ("telemetry", "warnings", "tasks", "chains"):
            assert getattr(ctn.store, name) is not None
        assert set(ctn.store.snapshot()) == {"telemetry_count", "warning_count", "task_count", "chain_count"}
