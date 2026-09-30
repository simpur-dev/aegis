"""rows.py 的纯映射测试：不连库，逐实体做双向回合，并压边界。

列序常量与映射函数的 arity 一并断言 —— 列漂移是这类持久层最典型的静默故障。
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from aegis.domain.enums import OwnerRole, RefType, RiskLevel, TaskType
from aegis.domain.messages import (
    DataRef,
    DeliveryAttempt,
    FallbackPolicy,
    StandardizedTaskUnit,
    TelemetryReading,
    WarningRecord,
    utc_now,
)
from aegis.persistence import rows
from aegis.persistence.errors import MappingError
from aegis.pipeline.chain import ChainResult, StageResult
from aegis.workflow.model import EdgeDef, InstanceStatus, NodeDef, NodeRun, NodeState, RetryPolicy, WorkflowDef, WorkflowInstance

# 与 002_schema.sql 的建表列一致（不含生成列/默认列），用于 arity 对照断言
TABLE_COLUMNS: dict[str, tuple[str, ...]] = {
    "monitoring_stations": rows.STATION_COLUMNS,
    "telemetry_readings": rows.TELEMETRY_COLUMNS,
    "standardized_task_units": rows.TASK_COLUMNS,
    "warnings": rows.WARNING_COLUMNS,
    "warning_receipts": rows.RECEIPT_COLUMNS,
    "chain_runs": rows.CHAIN_COLUMNS,
    "workflow_definitions": rows.WORKFLOW_DEF_COLUMNS,
    "workflow_instances": rows.WORKFLOW_INSTANCE_COLUMNS,
    "workflow_node_states": rows.NODE_STATE_COLUMNS,
}


def as_row(columns: tuple[str, ...], params: tuple[Any, ...]) -> dict[str, Any]:
    """按列序把映射结果还原成"库侧行"字典：即 asyncpg Record 的读形态。"""
    assert len(columns) == len(params), f"{columns} 与参数长度 {len(params)} 不符"
    return dict(zip(columns, params, strict=True))


def reading(**overrides: Any) -> TelemetryReading:
    base: dict[str, Any] = {
        "station_id": "RGSE01",
        "metric": "rain_10min",
        "value": 42.0,
        "unit": "mm",
        "region_code": "540421",
        "observed_at": "2026-06-14T08:00:00.000Z",
        "ingested_at": "2026-06-14T08:00:12.500Z",
        "source": "mqtt_edge",
    }
    base.update(overrides)
    return TelemetryReading(**base)


def unit(**overrides: Any) -> StandardizedTaskUnit:
    base: dict[str, Any] = {
        "event_id": "evt_" + "a1b2c3d4e5f6",
        "hazard_type": "debris_flow",
        "region_code": "540421",
        "task_type": TaskType.DISPATCH,
        "objective": "对易贡藏布泥石流形成区实施巡查与交通管制",
        "priority": 1,
        "sla_seconds": 900,
        "required_capabilities": ["field_survey"],
        "owner_role": OwnerRole.HUMAN_COMMANDER,
        "fallback_policy": FallbackPolicy(mode="escalate", max_retries=2, retry_backoff_ms=1_000),
        "created_by": "platform.task_parser",
        "created_at": "2026-06-14T08:01:00.000Z",
    }
    base.update(overrides)
    return StandardizedTaskUnit(**base)


def warning(**overrides: Any) -> WarningRecord:
    base: dict[str, Any] = {
        "event_id": "evt_" + "a1b2c3d4e5f6",
        "trace_id": "trc_" + "0123456789abcdef",
        "hazard_type": "debris_flow",
        "region_codes": ["540421", "540121"],
        "risk_level": RiskLevel.RED,
        "title_zh": "泥石流橙色预警",
        "body_zh": "6 小时内易贡藏布沿岸可能发生泥石流，请立即撤离河道两侧低洼地带。",
        "audiences": ["林芝市应急管理局", "易贡乡"],
        "channels": ["sms", "beidou"],
        "generated_at": "2026-06-14T08:02:00.000Z",
        "released_at": "2026-06-14T08:03:00.000Z",
        "deliveries": [
            DeliveryAttempt(
                channel="sms",
                audience_count=1_820,
                status="delivered",
                attempted_at="2026-06-14T08:03:01.000Z",
                receipt_at="2026-06-14T08:05:30.000Z",
                provider_msg_id="pmsg-1",
            ),
            DeliveryAttempt(
                channel="beidou",
                audience_count=40,
                status="pending",
                attempted_at="2026-06-14T08:03:02.000Z",
            ),
        ],
    }
    base.update(overrides)
    return WarningRecord(**base)


def workflow_def(**overrides: Any) -> WorkflowDef:
    base: dict[str, Any] = {
        "workflow_id": "wf_" + "0a1b2c3d4e5f",
        "name": "泥石流应急处置剧本",
        "description": "感知→研判→决策→执行→反馈 五段式剧本（中文与 Tibetan 混排）",
        "version": 3,
        "nodes": [
            NodeDef(node_id="perceive", type="rule_trigger", name="触发研判", config={"region": "540421"}),
            NodeDef(
                node_id="assess",
                type="risk_assess",
                retry=RetryPolicy(max_attempts=2, backoff_ms=500),
                config={"level": "orange"},
            ),
        ],
        "edges": [EdgeDef(source="perceive", target="assess", condition="HIT")],
        "created_by": "platform.workflow",
    }
    base.update(overrides)
    return WorkflowDef(**base)


def workflow_instance(**overrides: Any) -> WorkflowInstance:
    base: dict[str, Any] = {
        "instance_id": "wfi_" + "0a1b2c3d4e5f",
        "workflow_id": "wf_" + "0a1b2c3d4e5f",
        "workflow_version": 3,
        "trace_id": "trc_" + "0123456789abcdef",
        "status": InstanceStatus.SUCCEEDED,
        "nodes": {
            "perceive": NodeRun(
                node_id="perceive",
                type="rule_trigger",
                state=NodeState.SUCCEEDED,
                attempts=1,
                schedule_latency_ms=12.5,
                duration_ms=88.0,
                output={"hits": ["R-DEBRIS-RAIN-1"], "说明": "雨强超阈值"},
                notes=["首轮即就绪"],
            ),
            "assess": NodeRun(node_id="assess", type="risk_assess", state=NodeState.AWAITING_HUMAN, attempts=0),
        },
        "payload": {"region_code": "540421"},
        "results": {"perceive": {"hits": ["R-DEBRIS-RAIN-1"]}},
        "created_at": "2026-06-14T08:00:00.000Z",
        "finished_at": None,
    }
    base.update(overrides)
    return WorkflowInstance(**base)


class TestColumnArity:
    """列序常量与映射函数产物的 arity 必须逐表对齐。"""

    @pytest.mark.parametrize("table", sorted(TABLE_COLUMNS))
    def test_column_tuples_are_unique(self, table: str) -> None:
        columns = TABLE_COLUMNS[table]
        assert len(set(columns)) == len(columns), table
        assert all(name == name.strip().lower() for name in columns), table

    def test_telemetry_arity(self) -> None:
        assert len(rows.TELEMETRY_COLUMNS) == len(rows.telemetry_params(reading()))

    def test_task_arity(self) -> None:
        assert len(rows.TASK_COLUMNS) == len(rows.task_params(unit()))

    def test_warning_and_receipt_arity(self) -> None:
        record = warning()
        assert len(rows.WARNING_COLUMNS) == len(rows.warning_params(record))
        for params in rows.receipt_params(record):
            assert len(rows.RECEIPT_COLUMNS) == len(params)

    def test_station_arity(self) -> None:
        params = rows.station_params("RGSE01", "540421", 95.321, 30.215, name_zh="易贡", hazard_focus=["debris_flow"])
        assert len(rows.STATION_COLUMNS) == len(params)

    def test_chain_arity(self) -> None:
        result = ChainResult(trace_id="trc_" + "0123456789abcdef", event_id="evt_" + "a1b2c3d4e5f6")
        assert len(rows.CHAIN_COLUMNS) == len(rows.chain_row(result, finished_at=utc_now()))

    def test_workflow_arity(self) -> None:
        definition = workflow_def()
        instance = workflow_instance()
        assert len(rows.WORKFLOW_DEF_COLUMNS) == len(rows.workflow_def_params(definition))
        assert len(rows.WORKFLOW_INSTANCE_COLUMNS) == len(rows.workflow_instance_params(instance))
        for run in instance.nodes.values():
            assert len(rows.NODE_STATE_COLUMNS) == len(rows.node_state_params(instance.instance_id, run))


class TestTelemetryRoundTrip:
    def test_round_trip(self) -> None:
        original = reading()
        restored = rows.telemetry_from_row(as_row(rows.TELEMETRY_COLUMNS, rows.telemetry_params(original)))
        assert restored == original

    def test_boundary_values_survive(self) -> None:
        for value in (0.0, -0.0, 1e18, -1e18, 0.000_001):
            assert rows.telemetry_from_row(as_row(rows.TELEMETRY_COLUMNS, rows.telemetry_params(reading(value=value)))).value == value

    def test_cjk_and_tibetan_source_survive(self) -> None:
        original = reading(source="ལུང་པ།-藏东南边端网关")
        assert rows.telemetry_from_row(as_row(rows.TELEMETRY_COLUMNS, rows.telemetry_params(original))) == original

    def test_quality_flag_default_is_explicit(self) -> None:
        params = rows.telemetry_params(reading())
        assert params[rows.TELEMETRY_COLUMNS.index("quality_flag")] == "ok"

    def test_identical_observed_at_is_dedupable_by_natural_key(self) -> None:
        first = rows.telemetry_params(reading())
        again = rows.telemetry_params(reading())
        key = (
            rows.TELEMETRY_COLUMNS.index("station_id"),
            rows.TELEMETRY_COLUMNS.index("metric"),
            rows.TELEMETRY_COLUMNS.index("observed_at"),
        )
        assert [first[i] for i in key] == [again[i] for i in key]

    def test_clock_skew_ordering_is_preserved_verbatim(self) -> None:
        """边缘时钟回拨（ingested 早于 observed）不修正：负时延是证据，不是要抹掉的数据。"""
        skewed = reading(observed_at="2026-06-14T08:00:30.000Z", ingested_at="2026-06-14T08:00:00.000Z")
        assert skewed.ingest_latency_ms == pytest.approx(-30_000.0)
        restored = rows.telemetry_from_row(as_row(rows.TELEMETRY_COLUMNS, rows.telemetry_params(skewed)))
        assert restored == skewed

    def test_naive_timestamp_rejected(self) -> None:
        with pytest.raises(MappingError, match="带时区"):
            rows.to_moment("2026-06-14T08:00:00")

    def test_unparseable_timestamp_rejected(self) -> None:
        with pytest.raises(MappingError, match="无法解析"):
            rows.to_moment("昨天下午")


class TestTaskRoundTrip:
    def test_round_trip(self) -> None:
        original = unit()
        restored = rows.task_from_row(as_row(rows.TASK_COLUMNS, rows.task_params(original)))
        assert restored == original

    def test_empty_collections_and_payload(self) -> None:
        original = unit(trigger_refs=[], outputs=[], dependencies=[], input_data_refs=[], context_snapshot={})
        params = rows.task_params(original)
        assert params[rows.TASK_COLUMNS.index("context_snapshot")] == {}
        assert rows.task_from_row(as_row(rows.TASK_COLUMNS, params)) == original

    def test_cjk_and_tibetan_context_snapshot(self) -> None:
        snapshot = {
            "标题": "易贡藏布 6 号沟",
            "bo": "ལུང་པ།",
            "nested": {"读数": [{"metric": "rain_10min", "value": 42.0}]},
            "emoji": "⛰️🌧",
        }
        original = unit(context_snapshot=snapshot, objective="ལུང་པ། 巡查并管制河道")
        restored = rows.task_from_row(as_row(rows.TASK_COLUMNS, rows.task_params(original)))
        assert restored.context_snapshot == snapshot
        assert restored.objective == "ལུང་པ། 巡查并管制河道"

    def test_input_data_refs_are_plain_json(self) -> None:
        original = unit(input_data_refs=[DataRef(type=RefType.TELEMETRY, id="RGSE01")])
        refs = rows.task_params(original)[rows.TASK_COLUMNS.index("input_data_refs")]
        assert refs == [{"type": "telemetry", "id": "RGSE01"}]
        assert rows.task_from_row(as_row(rows.TASK_COLUMNS, rows.task_params(original))) == original

    def test_fallback_policy_round_trips_as_value_object(self) -> None:
        policy = FallbackPolicy(mode="transfer", max_retries=5, retry_backoff_ms=100, transfer_to="assess")
        params = rows.task_params(unit(fallback_policy=policy))
        restored = rows.task_from_row(as_row(rows.TASK_COLUMNS, params))
        assert restored.fallback_policy == policy

    def test_none_versus_missing_optional(self) -> None:
        with_none = unit(trigger_refs=[], outputs=[], dependencies=[])
        dumped = with_none.model_dump(include={"trigger_refs", "outputs", "dependencies"})
        assert dumped == {"trigger_refs": [], "outputs": [], "dependencies": []}

    def test_oversized_id_rejected(self) -> None:
        with pytest.raises(MappingError, match="超长"):
            rows.check_id("stu_" + "a" * 80, field="task_unit_id")

    def test_empty_id_rejected(self) -> None:
        with pytest.raises(MappingError, match="不得为空"):
            rows.check_id("", field="event_id")

    def test_oversized_json_payload_rejected(self) -> None:
        with pytest.raises(MappingError, match="载荷超限"):
            rows.task_params(unit(context_snapshot={"blob": "x" * (rows.MAX_JSON_BYTES + 10)}))

    def test_non_serialisable_payload_rejected(self) -> None:
        with pytest.raises(MappingError, match="可序列化"):
            rows.check_json({"bad": {1, 2}.copy}, field="context_snapshot")

    def test_corrupt_row_surfaces_as_mapping_error(self) -> None:
        row = as_row(rows.TASK_COLUMNS, rows.task_params(unit()))
        row["fallback_policy"] = {"mode": "not_a_mode"}
        with pytest.raises(MappingError, match="不合契约"):
            rows.task_from_row(row)

    def test_deadline_is_derived_from_two_persisted_columns(self) -> None:
        original = unit(sla_seconds=900)
        row = as_row(rows.TELEMETRY_COLUMNS if False else rows.TASK_COLUMNS, rows.task_params(original))
        created = row["created_at"]
        assert created + timedelta(seconds=row["sla_seconds"]) == original.deadline_at


class TestWarningRoundTrip:
    def test_round_trip_with_receipts(self) -> None:
        original = warning()
        row = as_row(rows.WARNING_COLUMNS, rows.warning_params(original))
        receipts = [as_row(rows.RECEIPT_COLUMNS, params) for params in rows.receipt_params(original)]
        assert rows.warning_from_row(row, receipts) == original

    def test_derived_columns(self) -> None:
        row = as_row(rows.WARNING_COLUMNS, rows.warning_params(warning()))
        assert row["status"] == "partial"
        assert row["region_code"] == "540421"
        assert row["reach_seconds"] == pytest.approx(150.0)
        assert row["delivered_at"] == datetime(2026, 6, 14, 8, 5, 30, tzinfo=UTC)

    def test_negative_reach_seconds_is_impossible(self) -> None:
        """边缘时钟回拨让回执早于发布：落库值按 0 收敛，CHECK 约束也禁负。"""
        skewed = warning(
            released_at="2026-06-14T08:05:00.000Z",
            deliveries=[
                DeliveryAttempt(
                    channel="sms",
                    audience_count=10,
                    status="delivered",
                    attempted_at="2026-06-14T08:05:01.000Z",
                    receipt_at="2026-06-14T08:04:00.000Z",
                )
            ],
        )
        assert skewed.reach_seconds() == pytest.approx(-60.0)
        row = as_row(rows.WARNING_COLUMNS, rows.warning_params(skewed))
        assert row["reach_seconds"] == 0.0
        assert row["reach_seconds"] >= 0

    def test_draft_without_release_has_no_reach(self) -> None:
        row = as_row(rows.WARNING_COLUMNS, rows.warning_params(warning(released_at=None)))
        assert row["status"] == "draft"
        assert row["released_at"] is None
        assert row["reach_seconds"] is None

    def test_draft_carries_receipt_time_but_no_reach(self) -> None:
        """未发布却已有回执（乱序/补录）：delivered_at 只由回执决定，reach 因缺发布时刻而按 None 收口。"""
        row = as_row(rows.WARNING_COLUMNS, rows.warning_params(warning(released_at=None)))
        assert row["delivered_at"] == datetime(2026, 6, 14, 8, 5, 30, tzinfo=UTC)
        assert row["reach_seconds"] is None

    def test_draft_without_receipts_has_neither(self) -> None:
        row = as_row(rows.WARNING_COLUMNS, rows.warning_params(warning(released_at=None, deliveries=[])))
        assert row["status"] == "draft"
        assert row["delivered_at"] is None
        assert row["reach_seconds"] is None

    def test_released_without_receipts(self) -> None:
        record = warning(deliveries=[])
        row = as_row(rows.WARNING_COLUMNS, rows.warning_params(record))
        assert row["status"] == "released"
        assert row["reach_seconds"] is None
        assert rows.receipt_params(record) == []

    def test_all_channels_failed(self) -> None:
        record = warning(
            deliveries=[DeliveryAttempt(channel="sms", audience_count=10, status="failed", attempted_at="2026-06-14T08:03:01.000Z")]
        )
        assert rows.warning_status(record) == "failed"

    def test_out_of_order_receipts_use_latest(self) -> None:
        record = warning(
            deliveries=[
                DeliveryAttempt(
                    channel="beidou",
                    audience_count=4,
                    status="delivered",
                    attempted_at="2026-06-14T08:09:00.000Z",
                    receipt_at="2026-06-14T08:09:00.000Z",
                ),
                DeliveryAttempt(
                    channel="sms",
                    audience_count=10,
                    status="delivered",
                    attempted_at="2026-06-14T08:03:01.000Z",
                    receipt_at="2026-06-14T08:40:00.000Z",
                ),
            ]
        )
        row = as_row(rows.WARNING_COLUMNS, rows.warning_params(record))
        assert row["delivered_at"] == datetime(2026, 6, 14, 8, 40, tzinfo=UTC)
        assert row["reach_seconds"] == pytest.approx(2_220.0)

    def test_none_versus_missing_body_bo(self) -> None:
        explicit_none = warning(body_bo=None)
        default = WarningRecord(
            **{
                **warning().model_dump(),
                "warning_id": "wrn_" + "0" * 20,
            }
        )
        assert explicit_none.body_bo is None
        assert rows.warning_params(explicit_none)[rows.WARNING_COLUMNS.index("body_bo")] is None
        assert rows.warning_params(default)[rows.WARNING_COLUMNS.index("body_bo")] is None

    def test_tibetan_body_round_trip(self) -> None:
        tibetan = "ཉེན་བརྡ། གླང་ཆུའི་འགྲམ་ནས་ཕྱིར་འཐེན་བྱོས།"
        record = warning(body_bo=tibetan)
        row = as_row(rows.WARNING_COLUMNS, rows.warning_params(record))
        assert row["body_bo"] == tibetan
        restored = rows.warning_from_row(row, [as_row(rows.RECEIPT_COLUMNS, p) for p in rows.receipt_params(record)])
        assert restored.body_bo == tibetan

    def test_provider_msg_id_none_versus_missing(self) -> None:
        record = warning(
            deliveries=[
                DeliveryAttempt(
                    channel="sms", audience_count=1, status="delivered", attempted_at="2026-06-14T08:00:00.000Z", provider_msg_id=None
                )
            ]
        )
        params = rows.receipt_params(record)[0]
        assert params[rows.RECEIPT_COLUMNS.index("provider_msg_id")] is None

    def test_empty_region_codes_rejected(self) -> None:
        record = warning()
        object.__setattr__(record, "region_codes", [])
        with pytest.raises(MappingError, match="region_codes"):
            rows.warning_params(record)

    def test_oversized_warning_id_rejected(self) -> None:
        record = warning(warning_id="wrn_" + "a" * 70)
        with pytest.raises(MappingError, match="超长"):
            rows.warning_params(record)


class TestStationMapping:
    def test_point_is_held_with_longitude_first(self) -> None:
        params = rows.station_params("RGSE01", "540421", 95.321_456, 30.215_678)
        geom = params[rows.STATION_COLUMNS.index("geom")]
        assert geom == "SRID=4326;POINT(95.321456 30.215678)"

    def test_missing_coordinate_is_null(self) -> None:
        params = rows.station_params("RGSE01", "540421", None, None)
        assert params[rows.STATION_COLUMNS.index("geom")] is None

    def test_half_coordinate_rejected(self) -> None:
        with pytest.raises(MappingError, match="成对"):
            rows.station_params("RGSE01", "540421", 95.3, None)

    @pytest.mark.parametrize(("lon", "lat"), [(181.0, 30.0), (-181.0, 30.0), (95.0, 91.0), (95.0, -91.0)])
    def test_out_of_earth_coordinates_rejected(self, lon: float, lat: float) -> None:
        with pytest.raises(MappingError):
            rows.point_wkt(lon, lat)


class TestChainLedger:
    def test_chain_row_shape(self) -> None:
        record = warning()
        created = unit()
        result = ChainResult(
            trace_id="trc_" + "0123456789abcdef",
            event_id="evt_" + "a1b2c3d4e5f6",
            stages=[StageResult("perceive", "local", True, 12.3456, "命中 2 条")],
            task_units=[created],
            warning=record,
            errors=["全部通道失败: 超时"],
            degradations=["研判降级"],
        )
        row = as_row(rows.CHAIN_COLUMNS, rows.chain_row(result, finished_at=datetime(2026, 6, 14, 8, 9, tzinfo=UTC)))
        assert row["trace_id"] == result.trace_id
        assert row["ok"] is False
        assert row["acted"] is False
        assert row["task_unit_ids"] == [created.task_unit_id]
        assert row["warning_id"] == record.warning_id
        assert row["stages"][0]["latency_ms"] == 12.346
        assert row["risk"] is None
        assert row["errors"] == ["全部通道失败: 超时"]

    def test_chain_row_without_warning(self) -> None:
        result = ChainResult(trace_id="trc_" + "f" * 16, event_id="evt_" + "a" * 12)
        row = as_row(rows.CHAIN_COLUMNS, rows.chain_row(result, finished_at=utc_now()))
        assert row["warning_id"] is None
        assert row["stages"] == []
        assert row["task_unit_ids"] == []

    def test_chain_row_never_imports_service_types(self) -> None:
        """risk 列只走 as_dict()：映射层不得为取载荷而导入 aegis.services/api（文档串里的提及不算）。"""
        tree = ast.parse(Path(rows.__file__).read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        assert not [name for name in imported if name.startswith(("aegis.services", "aegis.api"))]
        # ChainResult 只在 TYPE_CHECKING 下标注：运行期本层因此不牵动 pipeline/services 依赖
        assert not hasattr(rows, "ChainResult")


class TestWorkflowMapping:
    def test_definition_round_trip(self) -> None:
        original = workflow_def()
        restored = rows.workflow_def_from_row(as_row(rows.WORKFLOW_DEF_COLUMNS, rows.workflow_def_params(original)))
        assert restored.signature() == original.signature()
        assert restored.nodes[0].config == {"region": "540421"}
        assert restored.edges[0].condition == "hit"

    def test_definition_rejects_corrupt_graph(self) -> None:
        row = as_row(rows.WORKFLOW_DEF_COLUMNS, rows.workflow_def_params(workflow_def()))
        row["edges"] = [{"source": "ghost", "target": "assess", "condition": ""}]
        with pytest.raises(MappingError, match="不合契约"):
            rows.workflow_def_from_row(row)

    def test_instance_and_node_states_round_trip(self) -> None:
        original = workflow_instance()
        instance_row = as_row(rows.WORKFLOW_INSTANCE_COLUMNS, rows.workflow_instance_params(original))
        node_rows = [as_row(rows.NODE_STATE_COLUMNS, rows.node_state_params(original.instance_id, run)) for run in original.nodes.values()]
        restored = rows.workflow_instance_from_row(instance_row, node_rows)
        assert restored.instance_id == original.instance_id
        assert restored.status is InstanceStatus.SUCCEEDED
        assert restored.results == original.results
        assert restored.nodes["perceive"].output == {"hits": ["R-DEBRIS-RAIN-1"], "说明": "雨强超阈值"}
        assert restored.nodes["assess"].state is NodeState.AWAITING_HUMAN
        assert restored.nodes["perceive"].notes == ["首轮即就绪"]

    def test_node_run_optional_monotonic_fields_are_not_persisted(self) -> None:
        """mono 时基是进程内的，落库无意义：状态里只留时长与延迟。"""
        run = NodeRun(node_id="perceive", type="rule_trigger", state=NodeState.RUNNING, started_mono=1_234.5, ready_mono=1_230.0)
        params = rows.node_state_params("wfi_" + "0a1b2c3d4e5f", run)
        assert len(params) == len(rows.NODE_STATE_COLUMNS)
        restored = rows.node_run_from_row(as_row(rows.NODE_STATE_COLUMNS, params))
        assert restored.started_mono is None
        assert restored.state is NodeState.RUNNING

    def test_finished_at_none_versus_missing(self) -> None:
        running = workflow_instance(status=InstanceStatus.RUNNING, finished_at=None, error=None)
        row = as_row(rows.WORKFLOW_INSTANCE_COLUMNS, rows.workflow_instance_params(running))
        assert row["finished_at"] is None and row["error"] is None
        assert row["status"] == "running"

    def test_failure_error_text_round_trips(self) -> None:
        failed = workflow_instance(status=InstanceStatus.FAILED, error="节点 assess 连续失败：上游无响应")
        row = as_row(rows.WORKFLOW_INSTANCE_COLUMNS, rows.workflow_instance_params(failed))
        assert rows.workflow_instance_from_row(row).error == failed.error


class TestTimestampHelpers:
    def test_now_iso_is_millisecond_utc(self) -> None:
        assert rows.to_iso(datetime(2026, 6, 14, 8, 0, 0, 123_456, tzinfo=UTC)) == "2026-06-14T08:00:00.123Z"

    def test_to_moment_accepts_offset_timezone(self) -> None:
        moment = rows.to_moment("2026-06-14T16:00:00.000+08:00")
        assert moment == datetime(2026, 6, 14, 8, 0, tzinfo=UTC)

    def test_optional_moment_passes_none_through(self) -> None:
        assert rows.optional_moment(None) is None

    def test_embedding_dimension_guard(self) -> None:
        with pytest.raises(MappingError, match="维度不符"):
            rows.validate_embedding([0.1] * 3)

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
    def test_embedding_rejects_non_finite(self, bad: float) -> None:
        with pytest.raises(MappingError, match="NaN/Inf"):
            rows.validate_embedding([bad] * rows.EMBEDDING_DIM)

    def test_task_embedding_param_order(self) -> None:
        """UPDATE 的绑定序（向量、模型、ID）与 SQL_TASK_EMBEDDING 的 $1/$2/$3 必须同序。"""
        params = rows.task_embedding_params("stu_" + "0" * 20, [0.5] * rows.EMBEDDING_DIM, model="bge-m3")
        assert len(params) == 3
        vector, model, task_unit_id = params
        assert vector == [0.5] * rows.EMBEDDING_DIM
        assert all(isinstance(v, float) for v in vector)
        assert model == "bge-m3"
        assert task_unit_id == "stu_" + "0" * 20

    def test_task_embedding_rejects_wrong_dim_and_blank_model(self) -> None:
        with pytest.raises(MappingError, match="维度不符"):
            rows.task_embedding_params("stu_" + "0" * 20, [0.5] * 3, model="bge-m3")
        with pytest.raises(MappingError, match="不得为空"):
            rows.task_embedding_params("stu_" + "0" * 20, [0.5] * rows.EMBEDDING_DIM, model="")
