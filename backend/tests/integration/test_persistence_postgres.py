"""真实 PostgreSQL 17 + PostGIS + pgvector 的集成验证（默认跳过）。

起库（镜像会把 postgis 与 pgvector 装在一起，包名已在 deploy/postgres/Dockerfile 注明实测来源）：
    docker build -t aegis-pg deploy/postgres
    docker run -d --name aegis-pg -p 5543:5432 \\
        -e POSTGRES_USER=aegis -e POSTGRES_PASSWORD=ci_only_pw -e POSTGRES_DB=aegis aegis-pg

然后：
    AEGIS_TEST_PG_DSN=postgresql://aegis:ci_only_pw@127.0.0.1:5543/aegis \\
        pytest tests/integration/test_persistence_postgres.py

这里验的是"只有真库才能证明的东西"：扩展 DDL 能否执行、ST_DWithin/ST_Covers 是否按米算、
pgvector 余弦检索是否真的排序、写缓冲在库不可达时是否保住热路径。
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import pytest

from aegis.domain.enums import HazardType, OwnerRole, RiskLevel, TaskType
from aegis.domain.messages import (
    FallbackMode,
    FallbackPolicy,
    StandardizedTaskUnit,
    TelemetryReading,
    WarningRecord,
    now_iso,
    utc_now,
)
from aegis.persistence.errors import ConnectionFailedError, VectorEmbeddingError
from aegis.persistence.postgres import PostgresStore

DSN = os.getenv("AEGIS_TEST_PG_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="未设置 AEGIS_TEST_PG_DSN，跳过真实 Postgres 测试")

# 拉萨附近三个点：约 111km 纬度差 = 1 度
STATIONS = (
    ("PG-S1", "540102", 91.2800, 29.8960),
    ("PG-S2", "540102", 91.3000, 29.9000),
    ("PG-S3", "540121", 90.6000, 29.6000),
)
EVT_A = "a1b2c3d4e5f6"
EVT_V1 = "b1b2c3d4e5f6"
EVT_V2 = "c1b2c3d4e5f6"
EVT_V3 = "d1b2c3d4e5f6"
ZONE = "POLYGON((91.20 29.80, 91.40 29.80, 91.40 30.00, 91.20 30.00, 91.20 29.80))"


def _reading(
    station: str,
    value: float,
    *,
    region: str = "540102",
    metric: str = "hourly_rain",
    seconds_ago: int = 0,
) -> TelemetryReading:
    moment = utc_now() - timedelta(seconds=seconds_ago)
    return TelemetryReading(
        station_id=station,
        metric=metric,
        value=value,
        unit="mm",
        region_code=region,
        observed_at=moment.isoformat(timespec="milliseconds"),
        ingested_at=now_iso(),
        source="integration",
        quality_flag="ok",
    )


def _task(event_id: str, *, region: str = "540102", hazard: HazardType = HazardType.DEBRIS_FLOW) -> StandardizedTaskUnit:
    return StandardizedTaskUnit(
        event_id=f"evt_{event_id}",
        hazard_type=hazard.value if isinstance(hazard, HazardType) else hazard,
        region_code=region,
        task_type=TaskType.WARN,
        objective="发布橙色预警并组织转移",  # 契约要求 objective 至少 4 字
        priority=1,
        sla_seconds=180,
        required_capabilities=["warning.publish"],
        owner_role=OwnerRole.PLATFORM,
        fallback_policy=FallbackPolicy(mode=FallbackMode.DEGRADE_TO_RULE),
        created_by="platform.integration",
    )


def _warning(event_id: str, trace_id: str) -> WarningRecord:
    moment = now_iso()
    return WarningRecord(
        event_id=event_id,
        trace_id=trace_id,
        hazard_type=HazardType.DEBRIS_FLOW,
        region_codes=["540102"],
        risk_level=RiskLevel.ORANGE,
        title_zh="泥石流风险橙色预警",
        body_zh="过去 1 小时累计降雨超阈值，建议立即转移沟口居民。",
        generated_at=moment,
        channels=[],
    )


@pytest.fixture
async def store() -> AsyncIterator[PostgresStore]:
    pg = PostgresStore(dsn=DSN, batch_rows=50)
    await pg.connect(start_buffer=False)
    await pg.migrate()
    pool = pg.require_pool()
    await pool.execute(
        "TRUNCATE telemetry_readings, warnings, warning_receipts, standardized_task_units, "
        "monitoring_stations, chain_runs, workflow_definitions, workflow_instances, workflow_node_states RESTART IDENTITY CASCADE"
    )
    try:
        yield pg
    finally:
        await pg.close(grace_ms=500)


async def _count(pg: PostgresStore, table: str) -> int:
    row = await pg.require_pool().fetchrow(f"SELECT count(*) AS n FROM {table}")
    return int(row["n"])


class TestMigrations:
    async def test_extensions_and_tables_exist_after_migrate(self, store: PostgresStore) -> None:
        pool = store.require_pool()
        installed = await pool.fetch("SELECT extname, extversion FROM pg_extension WHERE extname IN ('postgis','vector') ORDER BY extname")
        assert [r["extname"] for r in installed] == ["postgis", "vector"]
        tables = {r["tablename"] for r in await pool.fetch("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")}
        assert {"monitoring_stations", "telemetry_readings", "warnings", "standardized_task_units"} <= tables

    async def test_migrate_is_idempotent_and_checksummed(self, store: PostgresStore) -> None:
        # 夹具已应用过一轮：幂等的含义是"不重复应用"，故再跑两次都应返回空
        assert await store.migrate() == [] and await store.migrate() == []
        ledger = await store.require_pool().fetch("SELECT name FROM public.schema_migrations ORDER BY name")
        assert [r["name"] for r in ledger] == ["001_extensions.sql", "002_schema.sql", "003_indexes.sql"]


class TestSpatialQueries:
    async def test_radius_query_returns_nearest_first_in_metres(self, store: PostgresStore) -> None:
        for station_id, region, lon, lat in STATIONS:
            await store.upsert_station(station_id, region, lon, lat, name_zh=f"测试站{station_id[-1]}")
        await store.flush()

        near = await store.stations_within(lon=91.28, lat=29.896, radius_m=5000, limit=10)
        assert [r["station_id"] for r in near] == ["PG-S1", "PG-S2"]
        assert near[0]["distance_m"] < near[1]["distance_m"]
        # 约 2.2km 量级：证明按"米"算而不是按平面度数算
        assert 500 < near[1]["distance_m"] < 5000

    async def test_radius_excludes_distant_station(self, store: PostgresStore) -> None:
        for station_id, region, lon, lat in STATIONS:
            await store.upsert_station(station_id, region, lon, lat)
        await store.flush()
        wide = await store.stations_within(lon=91.28, lat=29.896, radius_m=500_000, limit=10)
        assert {r["station_id"] for r in wide} == {"PG-S1", "PG-S2", "PG-S3"}

    async def test_upsert_station_is_idempotent(self, store: PostgresStore) -> None:
        await store.upsert_station("PG-S1", "540102", 91.28, 29.896)
        await store.upsert_station("PG-S1", "540102", 91.28, 29.896, name_zh="改名")
        await store.flush()
        assert await _count(store, "monitoring_stations") == 1
        row = await store.require_pool().fetchrow("SELECT name_zh FROM monitoring_stations WHERE station_id='PG-S1'")
        assert row["name_zh"] == "改名"

    async def test_hazard_trace_summary_counts_within_polygon(self, store: PostgresStore) -> None:
        for station_id, region, lon, lat in STATIONS:
            await store.upsert_station(station_id, region, lon, lat)
        await store.telemetry.add([_reading("PG-S1", 42.0), _reading("PG-S3", 99.0, region="540121")])
        await store.flush()
        since = utc_now() - timedelta(hours=1)
        summary = await store.hazard_trace_summary(polygon_wkt=ZONE, since=since)
        assert summary["station_count"] == 2  # S3 在面外
        assert summary["readings"] == 1  # 只有面内站点的读数


class TestTelemetryPersistence:
    async def test_readings_reach_the_database(self, store: PostgresStore) -> None:
        await store.telemetry.add([_reading("PG-S1", v, seconds_ago=i * 60) for i, v in enumerate((10.5, 22.0, 31.25))])
        assert await _count(store, "telemetry_readings") == 0  # 未刷写前库里没有
        flushed = await store.flush()
        assert flushed == 3
        assert await _count(store, "telemetry_readings") == 3

    async def test_repeated_flush_does_not_duplicate_rows(self, store: PostgresStore) -> None:
        await store.telemetry.add([_reading("PG-S1", 5.0)])
        await store.flush()
        await store.flush()
        assert await _count(store, "telemetry_readings") == 1

    async def test_values_with_sql_payload_are_stored_as_data(self, store: PostgresStore) -> None:
        """注入防护的真实一关：恶意文本走参数绑定，表结构与行数不受影响。"""
        evil = "PG-'); DROP TABLE telemetry_readings; --"
        await store.telemetry.add([_reading(evil, 1.0)])
        await store.flush()
        assert await _count(store, "telemetry_readings") == 1
        row = await store.require_pool().fetchrow("SELECT station_id FROM telemetry_readings")
        assert row["station_id"] == evil


class TestWarningAndTaskPersistence:
    async def test_warning_and_receipts_round_trip(self, store: PostgresStore) -> None:
        record = _warning("evt_w1", "trc_" + "ab" * 8)
        await store.warnings.put(record)
        await store.flush()
        loaded = await store.load_warning(record.warning_id)
        assert loaded is not None
        assert loaded.title_zh == record.title_zh
        assert loaded.risk_level is record.risk_level
        assert loaded.region_codes == record.region_codes

    async def test_task_units_round_trip_by_event(self, store: PostgresStore) -> None:
        units = [_task(EVT_A, region="540102"), _task(EVT_A, region="540121")]
        await store.tasks.put_many(units)
        await store.flush()
        loaded = await store.load_tasks_by_event(f"evt_{EVT_A}")
        assert {u.task_unit_id for u in loaded} == {u.task_unit_id for u in units}

    async def test_pgvector_cosine_search_ranks_by_similarity(self, store: PostgresStore) -> None:
        unit = _task(EVT_V1)
        await store.tasks.put_many([unit])
        await store.flush()
        # 余弦只看方向：全正数向量彼此近乎共线，随便写会让"远"样本也得满分
        base = [1.0] + [0.0] * 1023
        near = [0.99] + [0.01] * 3 + [0.0] * 1020
        far = [0.0] * 512 + [1.0] + [0.0] * 511
        await store.update_task_embedding(unit.task_unit_id, base, model="bge-m3")
        await store.flush()
        other = _task(EVT_V2)
        await store.tasks.put_many([other])
        await store.flush()
        await store.update_task_embedding(other.task_unit_id, far, model="bge-m3")
        await store.flush()

        hits = await store.search_similar_tasks(near, k=2)
        assert hits and hits[0].id == unit.task_unit_id
        assert hits[0].score > hits[-1].score
        assert 0.0 <= hits[0].score <= 1.0

    async def test_embedding_dimension_mismatch_is_rejected_before_sql(self, store: PostgresStore) -> None:
        unit = _task(EVT_V3)
        await store.tasks.put_many([unit])
        await store.flush()
        with pytest.raises(VectorEmbeddingError):
            await store.update_task_embedding(unit.task_unit_id, [0.1] * 8, model="bge-m3")


class TestWorkflowPersistence:
    async def test_definition_and_instance_with_node_states_round_trip(self, store: PostgresStore) -> None:
        from aegis.workflow.model import EdgeDef, InstanceStatus, NodeDef, NodeRun, RetryPolicy, WorkflowDef, WorkflowInstance

        definition = WorkflowDef(
            workflow_id="wf_aaaaaaaaaaaa",
            name="集成验证流程",
            nodes=(
                NodeDef(node_id="a", type="threshold", name="阈值判定", config={"metric": "hourly_rain"}),
                NodeDef(node_id="b", type="warning_generate", name="生成预警", retry=RetryPolicy(max_attempts=1)),
            ),
            edges=(EdgeDef(source="a", target="b"),),
        )
        await store.save_workflow_definition(definition)
        await store.flush()
        loaded = await store.load_workflow_definitions(name=definition.name)
        assert [d.nodes for d in loaded] == [definition.nodes]

        instance = WorkflowInstance(
            instance_id="wfi_a1b2c3d4e5f6",
            workflow_id=definition.workflow_id,
            workflow_version=definition.version,
            trace_id="trc_" + "cd" * 8,
            status=InstanceStatus.RUNNING,
            nodes={node.node_id: NodeRun(node_id=node.node_id, type=node.type) for node in definition.nodes},
            created_at=now_iso(),
        )
        await store.save_workflow_instance(instance)
        await store.flush()
        reloaded = await store.load_workflow_instance(instance.instance_id)
        assert reloaded is not None
        assert reloaded.status is instance.status
        assert set(reloaded.nodes) == set(instance.nodes)


class TestWeakNetworkBehaviour:
    async def test_unreachable_database_raises_typed_error_at_connect(self) -> None:
        """连不上库必须在建连阶段就抛类型化错误，而不是把异常留到第一次写。"""
        with pytest.raises(ConnectionFailedError):
            await PostgresStore(dsn="postgresql://aegis:pw@127.0.0.1:59999/aegis").connect()

    async def test_hot_path_survives_database_dying_after_connect(self, store: PostgresStore) -> None:
        """连上之后库失效：提交仍不得阻塞或抛错给业务，损失量由计数暴露。"""
        await store.connect(start_buffer=True)
        await store._pool.close()  # 直接掐掉池，模拟闪断后连接全部失效
        await store.telemetry.add([_reading("PG-S1", 7.0, seconds_ago=1)])
        metrics = await store.close(grace_ms=200)
        assert metrics["buffered"] >= 1
        # 关停后队列必空：余量要么写出、要么按丢弃计数，二者之和等于入队量
        assert metrics["dropped"] + metrics["flushed"] == metrics["buffered"]

    async def test_bad_dsn_is_rejected_at_connect(self) -> None:
        with pytest.raises(ConnectionFailedError):
            await PostgresStore(dsn="postgresql://x@127.0.0.1:59999/none?timeout=1").connect()


class TestReadModelStillServes:
    async def test_memory_read_model_answers_queries_after_flush(self, store: PostgresStore) -> None:
        await store.telemetry.add([_reading("PG-S1", 12.0), _reading("PG-S1", 30.0)])
        await store.flush()
        rows = store.telemetry.query(station_id="PG-S1", limit=10)
        assert [r.value for r in rows] == [12.0, 30.0]

    async def test_snapshot_reports_buffer_and_store_state(self, store: PostgresStore) -> None:
        snapshot: dict[str, Any] = store.snapshot()
        assert {"telemetry_count", "warning_count", "task_count", "chain_count"} <= set(snapshot)
