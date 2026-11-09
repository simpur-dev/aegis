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
import secrets
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import pytest

from aegis.domain.chain import ChainResult, StageResult
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
from aegis.domain.provenance import DataProvenance
from aegis.domain.site import SiteAttribute, SiteProfile
from aegis.persistence.errors import ConnectionFailedError, QueryFailedError, VectorEmbeddingError
from aegis.persistence.postgres import PostgresStore
from aegis.storage.store import AuditRecord

asyncpg = pytest.importorskip("asyncpg", reason="真库档需要 asyncpg 驱动")

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
        "monitoring_stations, site_profiles, chain_runs, workflow_definitions, workflow_instances, workflow_node_states, "
        "audit_ops, handling_tasks, handling_spaces, assistant_alerts, assistant_notifications, "
        "control_actions, operators RESTART IDENTITY CASCADE"
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
        # 真值标注表也在同一批 DDL 里：回放缺它就退化成"只有预测侧"，算不出准确率
        assert "warning_truth_labels" in tables
        # 共享记忆表也是：没有它，§4.3 的"跨事件、跨智能体"就只活到下一次重启
        assert "agent_memory" in tables
        # 助手控制面：席位/动作/告警/处置/审计六张表必须同批到位，缺一张都会让对应端点 503
        assert {"operators", "control_actions", "assistant_alerts", "handling_spaces", "handling_tasks", "audit_ops"} <= tables
        # 011：撤销列与站内通知表。撤销放宽 CHECK 但不改写发布事实，通知与告警分表（去重口径不同）。
        assert "assistant_notifications" in tables
        columns = {
            r["column_name"]
            for r in await pool.fetch(
                "SELECT column_name FROM information_schema.columns WHERE table_schema = 'public' AND table_name = 'warnings'"
            )
        }
        assert "revoked_at" in columns
        check = await pool.fetchrow("SELECT pg_get_constraintdef(oid) AS ddl FROM pg_constraint WHERE conname = 'warnings_status'")
        assert check is not None and "revoked" in check["ddl"]

    async def test_migrate_is_idempotent_and_checksummed(self, store: PostgresStore) -> None:
        # 夹具已应用过一轮：幂等的含义是"不重复应用"，故再跑两次都应返回空
        assert await store.migrate() == [] and await store.migrate() == []
        ledger = await store.require_pool().fetch("SELECT name FROM public.schema_migrations ORDER BY name")
        assert [r["name"] for r in ledger] == [
            "001_extensions.sql",
            "002_schema.sql",
            "003_indexes.sql",
            "004_accuracy_labels.sql",
            "005_trigger_rules.sql",
            "006_warning_audience_copies.sql",
            "007_agent_memory.sql",
            "008_threshold_suggestions.sql",
            "009_workflow_runtime.sql",
            "010_assistant_control.sql",
            "011_warning_revocation.sql",
            "012_audit_partitions.sql",
        ]


class TestReadModelBackfill:
    """重启后问答能不能答出"当前预警"，取决于这一步。"""

    async def test_backfill_loads_recent_warnings_and_tasks(self, store: PostgresStore) -> None:
        warning = _warning("evt_abcdef012345", "trc_abcdef012345")
        task = _task("abcdef012345")
        await store.warnings.put(warning)
        await store.tasks.put_many([task])
        await store.flush()

        fresh = PostgresStore(dsn=DSN)
        try:
            await fresh.connect()
            report = await fresh.backfill_read_model(limit=10)
            assert report["warnings"] >= 1
            assert report["tasks"] >= 1
            assert fresh.warnings.get(warning.warning_id) is not None
            assert fresh.tasks.get(task.task_unit_id) is not None
        finally:
            await fresh.close()

    async def test_backfill_zero_limit_is_noop(self, store: PostgresStore) -> None:
        assert await store.backfill_read_model(limit=0) == {"warnings": 0, "tasks": 0}


class TestAlertPersistence:
    """告警台账只有真库才能证明的部分：UNIQUE 去重、UPDATE...RETURNING 的原子升级。"""

    async def _seed_operator(self, store: PostgresStore, operator_id: str) -> None:
        await store.require_pool().execute(
            "INSERT INTO operators (operator_id, display_name, role) VALUES ($1, $2, 'duty') ON CONFLICT DO NOTHING",
            operator_id,
            "值班员",
        )

    async def _raise(self, store: PostgresStore, warning_id: str, *, drill: bool) -> tuple[dict[str, Any], bool]:
        return await store.raise_alert(
            alert_id=f"alt_{secrets.token_hex(8)}",
            warning_id=warning_id,
            level=1,
            region_code="540121",
            drill=drill,
        )

    async def test_dedup_by_warning_id_and_ack_needs_open_status(self, store: PostgresStore) -> None:
        warning = _warning("evt_alert0000001", "trc_alert0000001")
        await store.warnings.put(warning)
        await store.flush()
        first, created = await self._raise(store, warning.warning_id, drill=False)
        again, recreated = await self._raise(store, warning.warning_id, drill=False)
        assert created is True and recreated is False
        assert first["alert_id"] == again["alert_id"]

        await self._seed_operator(store, "op-duty")
        acked = await store.ack_alert(first["alert_id"], "op-duty")
        assert acked is not None and acked["status"] == "acked" and acked["ack_by"] == "op-duty"
        # 已 acked 不在 _ACTIONABLE 里：第二次更新命中 0 行（service 层据此返回 already=true）
        assert await store.ack_alert(first["alert_id"], "op-duty") is None

        rows = await store.list_alerts(open_only=True, limit=10)
        target = next(row for row in rows if row["alert_id"] == first["alert_id"])
        # LEFT JOIN warnings 拿到的展示字段：告警卡上"灾种/标题"与预警同源
        assert target["title"] == warning.title_zh
        assert target["hazard_type"] == warning.hazard_type

    async def test_escalate_due_is_atomic_and_skips_drills(self, store: PostgresStore) -> None:
        real, _ = await self._raise(store, "wrn_" + "1" * 20, drill=False)
        drill_row, _ = await self._raise(store, "wrn_" + "2" * 20, drill=True)
        assert real is not None and drill_row is not None
        # 只把真实告警拨过升级窗口：演练永不在升级队列里
        await store.require_pool().execute("UPDATE assistant_alerts SET created_at = now() - interval '10 minutes' WHERE drill = false")
        escalated = await store.escalate_due(after_minutes=5)
        assert [row["alert_id"] for row in escalated] == [real["alert_id"]]
        # 再扫一遍：状态已 escalated，不在 _ESCALATABLE 里，0 行命中
        assert await store.escalate_due(after_minutes=5) == []

    async def test_transfer_only_moves_assignee(self, store: PostgresStore) -> None:
        record, _ = await self._raise(store, "wrn_" + "3" * 20, drill=False)
        assert record is not None
        await self._seed_operator(store, "op-a")
        await self._seed_operator(store, "op-b")
        acked = await store.ack_alert(record["alert_id"], "op-a")
        assert acked is not None
        moved = await store.transfer_alert(record["alert_id"], "op-b")
        assert moved is not None
        assert moved["assigned_to"] == "op-b"
        assert moved["ack_by"] == "op-a"


class TestAuditPartitioning:
    """012：audit_ops 是按月 RANGE 分区表；写入路由按 ts 落月分区，维护函数幂等。"""

    async def test_audit_ops_is_partitioned_with_default_catchall(self, store: PostgresStore) -> None:
        pool = store.require_pool()
        kind = await pool.fetchrow(
            "SELECT relkind FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relname = 'audit_ops'"
        )
        assert kind is not None and kind["relkind"] == b"p", "audit_ops 必须是分区父表（relkind='p'）"
        names = {
            row["relname"]
            for row in await pool.fetch(
                "SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid "
                "JOIN pg_class p ON p.oid = i.inhparent WHERE p.relname = 'audit_ops'"
            )
        }
        # 24 个月回溯 + 当前 + 2 个月前瞻 + DEFAULT 兜底；起服维护还会继续补未来的月分区
        assert "audit_ops_default" in names
        assert len([name for name in names if name.startswith("audit_ops_2")]) >= 25

    async def test_audit_row_routes_by_ts_and_maintenance_is_idempotent(self, store: PostgresStore) -> None:
        pool = store.require_pool()
        await pool.execute(
            "INSERT INTO audit_ops (action_id, phase, operator_id, role, action) "
            "VALUES ('act_probe000001', 'intent', 'unknown', 'duty', 'warning.publish')"
        )
        routed = await pool.fetchrow("SELECT tableoid::regclass::text AS part FROM audit_ops WHERE action_id = 'act_probe000001'")
        assert routed is not None and routed["part"].startswith("audit_ops_2"), "写入必须落到具体月分区"
        # 两次维护调用都幂等：起服重复执行是常态，不得重复建分区/重复摘除
        assert await pool.fetchval("SELECT aegis_audit_ensure_partitions(0, 2)") == []
        assert await pool.fetchval("SELECT aegis_audit_detach_expired($1)", store._audit_retention_months) == []
        with pytest.raises(asyncpg.RaiseError):
            await pool.fetchval("SELECT aegis_audit_detach_expired(0)")  # 保留月数 0 由函数自身拒绝


class TestAssistantControlPersistence:
    """真库才能证明的：version 乐观锁（UPDATE...RETURNING 命中 0 行）、通知已读隔离、撤销落库。"""

    @staticmethod
    async def _seed_operator(store: PostgresStore, operator_id: str) -> None:
        await store.require_pool().execute(
            "INSERT INTO operators (operator_id, display_name, role) VALUES ($1, $2, 'lead') ON CONFLICT DO NOTHING",
            operator_id,
            "值班长",
        )

    async def test_handling_task_version_is_optimistic_lock(self, store: PostgresStore) -> None:
        await self._seed_operator(store, "op-lead")
        space_id = "wrn_" + "a" * 20
        stu_id = "stu_" + "a" * 16
        await store.ensure_space(space_id=space_id, warning_id=space_id)
        first, created = await store.ensure_task(space_id=space_id, stu_id=stu_id)
        again, recreated = await store.ensure_task(space_id=space_id, stu_id=stu_id)
        assert created is True and recreated is False
        assert first["version"] == 1 and again["version"] == 1

        claimed = await store.claim_task(stu_id, "op-lead", expected_version=1)
        assert claimed is not None and claimed["status"] == "claimed" and claimed["version"] == 2
        assert await store.claim_task(stu_id, "op-lead", expected_version=1) is None, "过期版本命中 0 行"
        assert await store.claim_task(stu_id, "op-lead", expected_version=2) is None, "claimed 不能再认领"
        assert await store.close_task(stu_id, expected_version=2) is not None
        assert await store.transfer_task(stu_id, "op-lead", expected_version=3) is None, "结单后一切迁移拒绝"

    async def test_notifications_are_scoped_to_recipient_and_read_once(self, store: PostgresStore) -> None:
        row = await store.insert_notification(recipient="lead-01", message="复核预警", kind="lead", sender="duty-01")
        assert row["read_at"] is None
        assert len(await store.list_notifications(recipient="lead-01", unread_only=True)) == 1
        assert await store.list_notifications(recipient="lead-02") == []
        assert await store.mark_read(row["id"], "lead-02") is None, "不能替别人标已读"
        marked = await store.mark_read(row["id"], "lead-01")
        assert marked is not None and marked["read_at"] is not None
        assert await store.mark_read(row["id"], "lead-01") is None, "重复标记不改时间"
        assert await store.list_notifications(recipient="lead-01", unread_only=True) == []

    async def test_revocation_persists_without_rewriting_release_facts(self, store: PostgresStore) -> None:
        moment = now_iso()
        record = _warning("evt_revoke00001", "trc_revoke00001").model_copy(update={"released_at": moment, "revoked_at": moment})
        await store.warnings.put(record)
        await store.flush()
        row = await store.require_pool().fetchrow(
            "SELECT status, released_at, revoked_at FROM warnings WHERE warning_id = $1", record.warning_id
        )
        assert row is not None
        assert row["status"] == "revoked"
        assert row["released_at"] is not None and row["revoked_at"] is not None, "撤销不抹掉发布事实"

    async def test_dual_review_votes_are_atomic_and_gate_confirmation(self, store: PostgresStore) -> None:
        """012：两票状态机在 SQL 侧的原子性——同一席位不能投两次，第二票才迁 executing。"""
        await self._seed_operator(store, "op-lead")
        await self._seed_operator(store, "op-a")
        await self._seed_operator(store, "op-b")
        await store.create_action(
            action_id="act_deadbeefdeadbeef",
            operator_id="op-lead",
            action="warning.publish",
            space_id=None,
            utterance="",
            resolved_args={},
            impact_scope={},
            expires_in_seconds=60,
            idempotency_key=None,
            review_required=2,
        )
        first = await store.review_action("act_deadbeefdeadbeef", {"operator_id": "op-a", "role": "lead", "at": now_iso()})
        assert first is not None and first[1] is False and len(first[0]["reviewers"]) == 1
        assert await store.review_action("act_deadbeefdeadbeef", {"operator_id": "op-a", "role": "lead", "at": now_iso()}) is None, (
            "同一席位不能投第二票"
        )
        assert await store.confirm_action("act_deadbeefdeadbeef") is None, "单票不能绕过双人复核"
        second = await store.review_action("act_deadbeefdeadbeef", {"operator_id": "op-b", "role": "lead", "at": now_iso()})
        assert second is not None and second[1] is True and second[0]["status"] == "executing"

    async def test_audit_rows_pair_with_transitions_and_survive_jsonb_roundtrip(self, store: PostgresStore) -> None:
        """第 8 步接线：intent 随 proposed 同事务、result 随 executed 同事务；越权独立行记 unknown。"""
        await self._seed_operator(store, "op-lead")
        pool = store.require_pool()
        action_id = "act_abcdef0123456789"
        intent = AuditRecord(
            phase="intent",
            action_id=action_id,
            operator_id="op-lead",
            role="lead",
            action="warning.publish",
            target="wrn_probe000001",
            utterance="把这条发出去",
            resolved_args={"target": "wrn_probe000001"},
            idempotency_key="idem-audit-0001",
        )
        record, replayed = await store.create_action(
            action_id=action_id,
            operator_id="op-lead",
            action="warning.publish",
            space_id=None,
            utterance="把这条发出去",
            resolved_args={"target": "wrn_probe000001"},
            impact_scope={},
            expires_in_seconds=60,
            idempotency_key="idem-audit-0001",
            audit=intent,
        )
        assert replayed is False and record["status"] == "proposed"
        # 幂等重放：同一 idempotency_key 再提交，审计不追加（重复提交不是新意图）
        await store.create_action(
            action_id=action_id,
            operator_id="op-lead",
            action="warning.publish",
            space_id=None,
            utterance="把这条发出去",
            resolved_args={"target": "wrn_probe000001"},
            impact_scope={},
            expires_in_seconds=60,
            idempotency_key="idem-audit-0001",
            audit=intent,
        )
        assert await pool.fetchval("SELECT count(*) FROM audit_ops WHERE action_id = $1", action_id) == 1

        assert await store.confirm_action(action_id) is not None
        await store.mark_action_executed(
            action_id,
            {"summary": "已发布"},
            audit=AuditRecord(
                phase="result",
                action_id=action_id,
                operator_id="op-lead",
                role="lead",
                action="warning.publish",
                target="wrn_probe000001",
                resolved_args={"target": "wrn_probe000001"},
                after={"summary": "已发布", "deliveries": 2},
            ),
        )
        result_row = await pool.fetchrow(
            "SELECT after->>'summary' AS summary, after->>'deliveries' AS deliveries "
            "FROM audit_ops WHERE action_id = $1 AND phase = 'result'",
            action_id,
        )
        assert result_row is not None
        assert result_row["summary"] == "已发布" and result_row["deliveries"] == "2", "jsonb 往返不失真"

        await store.record_audit(
            AuditRecord(
                phase="result",
                action_id="act_0badc0de0001",
                operator_id="unknown",
                role="unknown",
                action="warning.publish",
                after={"rejected": "E_ACTION_NOT_ALLOWED", "message": "需要 lead 及以上角色"},
            )
        )
        rejected = await pool.fetchrow(
            "SELECT operator_id, identity_kind, after->>'rejected' AS code FROM audit_ops WHERE action_id = $1",
            "act_0badc0de0001",
        )
        assert rejected is not None
        assert rejected["operator_id"] == "unknown" and rejected["code"] == "E_ACTION_NOT_ALLOWED"
        assert rejected["identity_kind"] == "declared", "席位是声明身份，审计如实标注"


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


class TestSiteProfiles:
    @staticmethod
    def _profile(*, slope: float = 38.0) -> SiteProfile:
        return SiteProfile(
            region_code="540121",
            name="林芝演示区",
            hazard_types=[HazardType.DEBRIS_FLOW],
            attributes=[
                SiteAttribute(key="slope_angle_deg", value=slope, unit="°", source="demo_survey"),
                SiteAttribute(key="water_source_type", value="rainstorm"),
            ],
            provenance=DataProvenance(data_kind="demo", sources=["site_profile:demo"], is_mock=True),
        )

    async def test_upsert_then_read_back_from_the_database(self, store: PostgresStore) -> None:
        profile = self._profile()
        await store.upsert_site_profile(profile)

        fetched = await store.get_site_profile("540121")
        assert fetched == profile
        listed = await store.list_site_profiles()
        assert [row.region_code for row in listed] == ["540121"]

        # 直接从库读原始行：证明画像真的落库，而不是只活在这条连接的读视图里
        raw = await store.require_pool().fetchrow("SELECT attributes FROM site_profiles WHERE region_code = '540121'")
        assert any(attr["key"] == "slope_angle_deg" for attr in raw["attributes"])

    async def test_upsert_replaces_instead_of_appending(self, store: PostgresStore) -> None:
        await store.upsert_site_profile(self._profile(slope=38.0))
        await store.upsert_site_profile(self._profile(slope=41.0))
        assert await _count(store, "site_profiles") == 1
        fetched = await store.get_site_profile("540121")
        assert fetched is not None and fetched.value_of("slope_angle_deg") == 41.0

    async def test_region_filter_and_missing_row(self, store: PostgresStore) -> None:
        await store.upsert_site_profile(self._profile())
        assert [row.region_code for row in await store.list_site_profiles(region_code="540121")] == ["540121"]
        assert await store.list_site_profiles(region_code="540221") == []
        assert await store.get_site_profile("540221") is None


class TestStationInventory:
    """`GET /api/v1/stations` 背后的合并语义：维表给身份与坐标，遥测证明存在。"""

    async def test_ledger_rows_carry_names_and_coordinates(self, store: PostgresStore) -> None:
        await store.upsert_station("PG-S1", "540102", 91.28, 29.896, name_zh="拉萨河站", hazard_focus=("泥石流",), elevation_m=3650.0)
        await store.upsert_station("PG-S2", "540102", 91.30, 29.90)

        rows = await store.list_stations()

        assert [row["station_id"] for row in rows] == ["PG-S1", "PG-S2"]
        assert rows[0]["name_zh"] == "拉萨河站" and rows[0]["hazard_focus"] == ["泥石流"]
        assert rows[0]["elevation_m"] == 3650.0
        assert round(rows[0]["lon"], 3) == 91.28 and round(rows[0]["lat"], 3) == 29.896
        assert str(rows[0]["geom"]).startswith("SRID=4326;POINT(91.28")
        assert rows[1]["name_zh"] == "" and rows[1]["elevation_m"] is None, "维表没写的字段必须是空值，不能补默认"

    async def test_stations_seen_only_in_telemetry_are_merged_without_coordinates(self, store: PostgresStore) -> None:
        await store.upsert_station("PG-S1", "540102", 91.28, 29.896, name_zh="拉萨河站")
        await store.telemetry.add([_reading("PG-S9", 12.0)])

        rows = await store.list_stations()
        by_id = {row["station_id"]: row for row in rows}

        assert set(by_id) == {"PG-S1", "PG-S9"}, "报过数但未登记的站必须进清单，否则地图比真实站数少"
        assert by_id["PG-S9"]["lon"] is None and by_id["PG-S9"]["name_zh"] == ""
        assert list(by_id) == sorted(by_id), "两个来源合并后仍要按 station_id 升序，否则两轮清单无法逐行对账"

    async def test_region_filter_and_limit_apply_to_the_merged_list(self, store: PostgresStore) -> None:
        await store.upsert_station("PG-S1", "540102", 91.28, 29.896)
        await store.upsert_station("PG-S2", "540121", 91.5, 29.6)
        await store.telemetry.add([_reading("PG-S3", 5.0, region="540121")])

        assert [row["station_id"] for row in await store.list_stations(region_code="540121")] == ["PG-S2", "PG-S3"]
        assert len(await store.list_stations(limit=1)) == 1
        with pytest.raises(QueryFailedError):
            await store.list_stations(limit=0)


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

    async def test_009_adds_runtime_snapshot_and_definition_lineage(self, store: PostgresStore) -> None:
        pool = store.require_pool()
        def_columns = {
            r["column_name"]
            for r in await pool.fetch("SELECT column_name FROM information_schema.columns WHERE table_name = 'workflow_definitions'")
        }
        instance_columns = {
            r["column_name"]
            for r in await pool.fetch("SELECT column_name FROM information_schema.columns WHERE table_name = 'workflow_instances'")
        }
        assert {"lineage_id", "content_hash"} <= def_columns
        assert "runtime" in instance_columns

    async def test_checkpoint_write_through_and_open_instance_recovery(self, store: PostgresStore) -> None:
        """经端口走完整链路：定义写穿 → 实例检查点入缓冲 → 重启读面取回同一条现场。"""
        from aegis.persistence.workflow_store import PostgresWorkflowPersistence
        from aegis.workflow.model import InstanceStatus, NodeDef, NodeRun, NodeState, WorkflowDef, WorkflowInstance
        from aegis.workflow.persistence import InstanceCheckpoint, WorkflowRuntime

        definition = WorkflowDef(
            workflow_id="wf_bbbbbbbbbbbb",
            lineage_id="wf_bbbbbbbbbbbb",
            name="检查点集成流程",
            nodes=(NodeDef(node_id="sign", type="human_review", config={"prompt": "签"}),),
        )
        persistence = PostgresWorkflowPersistence(store)
        persistence.save_definition(definition)
        instance = WorkflowInstance(
            instance_id="wfi_b1b2c3d4e5f6",
            workflow_id=definition.workflow_id,
            workflow_version=definition.version,
            trace_id="trc_" + "ef" * 8,
            status=InstanceStatus.WAITING,
            nodes={"sign": NodeRun(node_id="sign", type="human_review", state=NodeState.AWAITING_HUMAN)},
            created_at=now_iso(),
        )
        runtime = WorkflowRuntime(
            definition=definition,
            lineage_id=definition.lineage_id,
            pending_waits={"sign": "是否发布"},
            pending_options={"sign": ("approve", "reject")},
            positions={"sign": {"x": 1.0, "y": 2.0}},
        )
        persistence.save_instance(InstanceCheckpoint(instance=instance, runtime=runtime))
        await store.flush()

        definitions = await persistence.load_definitions(limit=10)
        assert [(d.workflow_id, d.lineage_id) for d in definitions] == [(definition.workflow_id, definition.lineage_id)]

        checkpoints = await persistence.load_open_instances(limit=10)
        assert [c.instance.instance_id for c in checkpoints] == [instance.instance_id]
        restored = checkpoints[0]
        assert restored.instance.status is InstanceStatus.WAITING
        assert restored.runtime.pending_waits == {"sign": "是否发布"}
        assert restored.runtime.positions == {"sign": {"x": 1.0, "y": 2.0}}
        assert restored.runtime.definition.nodes[0].type == "human_review"

    async def test_unparsable_runtime_snapshot_is_marked_failed_on_recovery(self, store: PostgresStore) -> None:
        """坏快照若继续以 waiting 留在库里，每次重启都会重演一遍；读面必须就地收成终态。"""
        from aegis.persistence.workflow_store import PostgresWorkflowPersistence
        from aegis.workflow.model import InstanceStatus, NodeDef, NodeRun, WorkflowDef, WorkflowInstance

        definition = WorkflowDef(
            workflow_id="wf_cccccccccccc",
            name="坏快照流程",
            nodes=(NodeDef(node_id="a", type="delay", config={"seconds": 0}),),
        )
        await store.save_workflow_definition(definition)
        instance = WorkflowInstance(
            instance_id="wfi_c1b2c3d4e5f6",
            workflow_id=definition.workflow_id,
            workflow_version=definition.version,
            trace_id="trc_" + "12" * 8,
            status=InstanceStatus.WAITING,
            nodes={"a": NodeRun(node_id="a", type="delay")},
            created_at=now_iso(),
        )
        await store.save_workflow_instance(instance)
        await store.flush()
        assert (
            await store.require_pool().fetchval("SELECT runtime FROM workflow_instances WHERE instance_id = $1", instance.instance_id) == {}
        )

        checkpoints = await PostgresWorkflowPersistence(store).load_open_instances(limit=10)
        assert checkpoints == []
        row = await store.require_pool().fetchrow(
            "SELECT status, error FROM workflow_instances WHERE instance_id = $1", instance.instance_id
        )
        assert row["status"] == "failed"
        assert "无法解析" in row["error"]
        assert await store.require_pool().fetchval("SELECT count(*) FROM workflow_instances WHERE status = 'waiting'") == 0


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


class TestChainAuditReadBack:
    """链路台账的落库与直读回合（README『模型与 Prompt 版本随结论入库』这句话的硬证据）。

    为什么必须在这一档验：`/api/v1/events` 读的是进程内视图，重启即空；
    凭证若只活在那里，"事后还能查这条预警是哪个模型产的"就没有依据。
    """

    async def test_模型凭证随stages落库并能按trace直读回(self, store: PostgresStore) -> None:
        trace = "trc_" + "ab" * 8
        result = ChainResult(trace_id=trace, event_id="evt_" + "cd" * 6)
        result.stages.append(
            StageResult(
                "assess",
                "agent",
                True,
                9026.2,
                "等级 2",
                model_info={"agent": "assess.real01", "model": "deepseek-chat", "prompt_version": "1.1.0"},
            )
        )
        result.stages.append(StageResult("plan", "local", True, 12.0, "生成 5 个任务单元"))
        await store.record_chain(result)
        assert await _count(store, "chain_runs") == 0, "落库走后台缓冲：flush 之前不该已经到库（这条同时钉住『别把缓冲当已写』）"
        await store.flush()
        assert await _count(store, "chain_runs") == 1

        rows_read = await store.load_chain_audit(trace)
        assert len(rows_read) == 1
        stages = rows_read[0]["stages"]
        assess = next(s for s in stages if s["name"] == "assess")
        assert assess["model_info"] == {"agent": "assess.real01", "model": "deepseek-chat", "prompt_version": "1.1.0"}
        plan = next(s for s in stages if s["name"] == "plan")
        assert "model_info" not in plan, "本地段在库里也必须缺席这个键，事后才分得清『没走模型』与『走了没记名』"

    async def test_未知trace回空表而不是报错(self, store: PostgresStore) -> None:
        assert await store.load_chain_audit("trc_" + "0" * 16) == []
