"""领域记录 <-> 关系行的纯映射：只有函数与列序常量，没有 I/O，也没有第二套行模型。

列序常量（*_COLUMNS）是 SQL 语句与参数元组的唯一事实源：两者必须同序同长，
`test_persistence_rows.py` 对每张表断言 len(COLUMNS) == len(params)，避免列漂移。

时间口径统一为毫秒级 UTC + Z 后缀（`domain.messages.now_iso`），域内所有时间戳均由它生成，
因此 落库 -> 读回 的字符串逐字节可复原；datetime 只作为 timestamptz 的绑定值存在。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Any

import orjson
from pydantic import ValidationError

from aegis.domain.enums import RiskLevel
from aegis.domain.messages import (
    DeliveryAttempt,
    StandardizedTaskUnit,
    TelemetryReading,
    WarningRecord,
    now_iso,
    parse_iso,
)
from aegis.persistence.errors import MappingError
from aegis.workflow.model import NodeRun, WorkflowDef, WorkflowInstance

if TYPE_CHECKING:  # 仅用于类型标注：运行期不导入 pipeline，本层因此不牵动 services 依赖
    from aegis.pipeline.chain import ChainResult

# 契约 ID 的实际上限：stu_/wrn_/trc_/evt_ 前缀 + 十六进制，64 已远够，越界即视为上游污染。
MAX_ID_LEN = 64
MAX_TEXT_LEN = 8_192
MAX_JSON_BYTES = 262_144
EMBEDDING_DIM = 1024
DELIVERED_STATUSES = frozenset({"delivered", "retried"})

STATION_COLUMNS = ("station_id", "name_zh", "region_code", "hazard_focus", "geom", "elevation_m")
TELEMETRY_COLUMNS = (
    "station_id",
    "metric",
    "value",
    "unit",
    "region_code",
    "observed_at",
    "ingested_at",
    "source",
    "quality_flag",
)
TASK_COLUMNS = (
    "task_unit_id",
    "schema_version",
    "event_id",
    "hazard_type",
    "region_code",
    "task_type",
    "objective",
    "priority",
    "sla_seconds",
    "owner_role",
    "required_capabilities",
    "trigger_refs",
    "outputs",
    "dependencies",
    "input_data_refs",
    "fallback_policy",
    "context_snapshot",
    "created_by",
    "created_at",
)
TASK_EMBEDDING_ORDER = ("embedding", "embedded_model", "task_unit_id")
WARNING_COLUMNS = (
    "warning_id",
    "event_id",
    "trace_id",
    "hazard_type",
    "region_code",
    "region_codes",
    "risk_level",
    "status",
    "title_zh",
    "body_zh",
    "body_bo",
    "audiences",
    "channels",
    "translation_pending",
    "generated_at",
    "released_at",
    "delivered_at",
    "reach_seconds",
)
RECEIPT_COLUMNS = ("warning_id", "channel", "audience_count", "status", "attempted_at", "receipt_at", "provider_msg_id")
CHAIN_COLUMNS = (
    "trace_id",
    "event_id",
    "ok",
    "acted",
    "stages",
    "risk",
    "hits",
    "task_unit_ids",
    "warning_id",
    "errors",
    "degradations",
    "finished_at",
)
WORKFLOW_DEF_COLUMNS = ("workflow_id", "name", "description", "version", "status", "created_by", "nodes", "edges", "signature")
WORKFLOW_INSTANCE_COLUMNS = (
    "instance_id",
    "workflow_id",
    "workflow_version",
    "trace_id",
    "status",
    "error",
    "payload",
    "results",
    "created_at",
    "finished_at",
)
NODE_STATE_COLUMNS = (
    "instance_id",
    "node_id",
    "type",
    "state",
    "attempts",
    "schedule_latency_ms",
    "duration_ms",
    "output",
    "error",
    "notes",
)


# ---------- 值级校验：越界即拒绝，不留给数据库报错 ----------


def check_id(value: str, *, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise MappingError(f"{field} 不得为空", detail={"field": field})
    if len(value) > MAX_ID_LEN:
        raise MappingError(f"{field} 超长：上限 {MAX_ID_LEN}", detail={"field": field, "length": len(value)})
    return value


def check_text(value: str, *, field: str, allow_empty: bool = True) -> str:
    if len(value) > MAX_TEXT_LEN:
        raise MappingError(f"{field} 超长：上限 {MAX_TEXT_LEN}", detail={"field": field, "length": len(value)})
    if not value and not allow_empty:
        raise MappingError(f"{field} 不得为空文本", detail={"field": field})
    return value


def check_json(value: object, *, field: str) -> object:
    """jsonb 载荷的尺寸闸门：载荷来自智能体，超大对象必须在进入缓冲前就挡下。"""
    try:
        size = len(orjson.dumps(value))
    except (TypeError, ValueError) as exc:
        raise MappingError(f"{field} 不是可序列化 JSON", detail={"field": field}) from exc
    if size > MAX_JSON_BYTES:
        raise MappingError(f"{field} JSON 载荷超限：上限 {MAX_JSON_BYTES} 字节", detail={"field": field, "bytes": size})
    return value


def _revalidate(model: type[Any], raw: object, *, field: str) -> Any:
    """行 -> 值对象：库内脏数据统一以 MappingError 表达，不让第三种异常族外泄。"""
    try:
        return model.model_validate(raw)
    except ValidationError as exc:
        raise MappingError(f"{field} 行数据不合契约", detail={"field": field, "invalid": len(exc.errors())}) from exc


def to_moment(value: str) -> datetime:
    """ISO 字符串 -> 带时区 datetime（timestamptz 绑定值）。"""
    try:
        moment = parse_iso(value)
    except (ValueError, TypeError) as exc:
        raise MappingError(f"时间戳无法解析：{value!r}") from exc
    if moment.tzinfo is None:
        raise MappingError(f"时间戳必须带时区：{value!r}")
    return moment


def to_iso(moment: datetime) -> str:
    return now_iso(moment)


def optional_moment(value: str | None) -> datetime | None:
    """None 与"缺字段"在行层同形（都是 SQL NULL），区别只在域层是否显式赋 None。"""
    return None if value is None else to_moment(value)


def point_wkt(lon: float, lat: float) -> str:
    """PostGIS geography 可解析的 WGS84 点文本（经度在前，OGC 口径）。"""
    if not -180.0 <= lon <= 180.0:
        raise MappingError("经度越界", detail={"lon": lon})
    if not -90.0 <= lat <= 90.0:
        raise MappingError("纬度越界", detail={"lat": lat})
    return f"SRID=4326;POINT({lon:.6f} {lat:.6f})"


def validate_embedding(values: Sequence[float], *, dim: int = EMBEDDING_DIM) -> list[float]:
    """pgvector 只在维度精确匹配时可用，故在缓冲入口就挡住长度与 NaN。"""
    vector = list(values)
    if len(vector) != dim:
        raise MappingError(f"向量维度不符：期望 {dim}", detail={"actual": len(vector)})
    floats = [float(v) for v in vector]
    if any(v != v or v in (float("inf"), float("-inf")) for v in floats):
        raise MappingError("向量含 NaN/Inf")
    return floats


# ---------- monitoring_stations ----------


def station_params(
    station_id: str,
    region_code: str,
    lon: float | None,
    lat: float | None,
    *,
    name_zh: str = "",
    hazard_focus: Sequence[str] = (),
    elevation_m: float | None = None,
) -> tuple[Any, ...]:
    if (lon is None) != (lat is None):
        raise MappingError("坐标必须成对给出", detail={"lon": lon, "lat": lat})
    geom = None if lon is None or lat is None else point_wkt(lon, lat)
    return (
        check_id(station_id, field="station_id"),
        check_text(name_zh, field="name_zh"),
        check_id(region_code, field="region_code"),
        list(hazard_focus),
        geom,
        elevation_m,
    )


# ---------- telemetry_readings ----------


def telemetry_params(reading: TelemetryReading) -> tuple[Any, ...]:
    return (
        check_id(reading.station_id, field="station_id"),
        check_id(reading.metric, field="metric"),
        float(reading.value),
        check_id(reading.unit, field="unit"),
        check_id(reading.region_code, field="region_code"),
        to_moment(reading.observed_at),
        to_moment(reading.ingested_at),
        check_text(reading.source, field="source", allow_empty=False),
        reading.quality_flag,
    )


def telemetry_from_row(row: Mapping[str, Any]) -> TelemetryReading:
    return TelemetryReading(
        station_id=row["station_id"],
        metric=row["metric"],
        value=row["value"],
        unit=row["unit"],
        region_code=row["region_code"],
        observed_at=to_iso(row["observed_at"]),
        ingested_at=to_iso(row["ingested_at"]),
        source=row["source"],
        quality_flag=row["quality_flag"],
    )


# ---------- standardized_task_units ----------


def task_params(unit: StandardizedTaskUnit) -> tuple[Any, ...]:
    check_json(unit.context_snapshot, field="context_snapshot")
    return (
        check_id(unit.task_unit_id, field="task_unit_id"),
        unit.schema_version,
        check_id(unit.event_id, field="event_id"),
        check_id(unit.hazard_type, field="hazard_type"),
        check_id(unit.region_code, field="region_code"),
        unit.task_type.value,
        check_text(unit.objective, field="objective", allow_empty=False),
        int(unit.priority),
        int(unit.sla_seconds),
        unit.owner_role.value,
        list(unit.required_capabilities),
        list(unit.trigger_refs),
        list(unit.outputs),
        list(unit.dependencies),
        [ref.model_dump(mode="json") for ref in unit.input_data_refs],
        unit.fallback_policy.model_dump(mode="json"),
        dict(unit.context_snapshot),
        check_text(unit.created_by, field="created_by", allow_empty=False),
        to_moment(unit.created_at),
    )


def task_from_row(row: Mapping[str, Any]) -> StandardizedTaskUnit:
    """行 -> STU：嵌入值对象显式重建，让契约校验在读路径上同样生效。"""
    return _revalidate(
        StandardizedTaskUnit,
        {
            "task_unit_id": row["task_unit_id"],
            "schema_version": row["schema_version"],
            "event_id": row["event_id"],
            "hazard_type": row["hazard_type"],
            "region_code": row["region_code"],
            "task_type": row["task_type"],
            "objective": row["objective"],
            "priority": int(row["priority"]),
            "sla_seconds": int(row["sla_seconds"]),
            "owner_role": row["owner_role"],
            "required_capabilities": list(row["required_capabilities"]),
            "trigger_refs": list(row["trigger_refs"]),
            "outputs": list(row["outputs"]),
            "dependencies": list(row["dependencies"]),
            "input_data_refs": list(row["input_data_refs"]),
            "fallback_policy": row["fallback_policy"],
            "context_snapshot": dict(row["context_snapshot"]),
            "created_by": row["created_by"],
            "created_at": to_iso(row["created_at"]),
        },
        field="standardized_task_unit",
    )


def task_embedding_params(task_unit_id: str, embedding: Sequence[float], *, model: str) -> tuple[Any, ...]:
    """回填向量的绑定值顺序（UPDATE 语句用，非 INSERT 列序，故无对应 *_COLUMNS）。"""
    return (
        validate_embedding(embedding),
        check_text(model, field="embedded_model", allow_empty=False),
        check_id(task_unit_id, field="task_unit_id"),
    )


# ---------- warnings / warning_receipts ----------


def warning_status(record: WarningRecord) -> str:
    """状态与 `WarningRecord.reach_seconds()` 同源，不从外部推断。"""
    if not record.released_at:
        return "draft"
    if not record.deliveries:
        return "released"
    delivered = sum(1 for d in record.deliveries if d.status in DELIVERED_STATUSES)
    if delivered == len(record.deliveries):
        return "delivered"
    return "partial" if delivered else "failed"


def delivered_at(record: WarningRecord) -> datetime | None:
    """最后回执时刻：reach_seconds 的终点，取 max 以覆盖乱序到达的回执。"""
    receipts = [to_moment(d.receipt_at) for d in record.deliveries if d.receipt_at]
    return max(receipts) if receipts else None


def reach_seconds(record: WarningRecord) -> float | None:
    """触达耗时；边缘设备时钟回拨会让回执早于发布，此处按 0 收敛而非落负值。"""
    raw = record.reach_seconds()
    return None if raw is None else max(0.0, raw)


def warning_params(record: WarningRecord) -> tuple[Any, ...]:
    if not record.region_codes:
        raise MappingError("region_codes 不得为空")
    return (
        check_id(record.warning_id, field="warning_id"),
        check_id(record.event_id, field="event_id"),
        check_id(record.trace_id, field="trace_id"),
        check_id(record.hazard_type, field="hazard_type"),
        check_id(record.region_codes[0], field="region_code"),
        [check_id(code, field="region_code") for code in record.region_codes],
        int(record.risk_level),
        warning_status(record),
        check_text(record.title_zh, field="title_zh", allow_empty=False),
        check_text(record.body_zh, field="body_zh", allow_empty=False),
        None if record.body_bo is None else check_text(record.body_bo, field="body_bo"),
        list(record.audiences),
        list(record.channels),
        bool(record.translation_pending),
        to_moment(record.generated_at),
        optional_moment(record.released_at),
        delivered_at(record),
        reach_seconds(record),
    )


def receipt_params(record: WarningRecord) -> list[tuple[Any, ...]]:
    return [
        (
            check_id(record.warning_id, field="warning_id"),
            check_id(attempt.channel, field="channel"),
            int(attempt.audience_count),
            attempt.status,
            to_moment(attempt.attempted_at),
            optional_moment(attempt.receipt_at),
            None if attempt.provider_msg_id is None else check_id(attempt.provider_msg_id, field="provider_msg_id"),
        )
        for attempt in record.deliveries
    ]


def warning_from_row(row: Mapping[str, Any], receipts: Sequence[Mapping[str, Any]] = ()) -> WarningRecord:
    return WarningRecord(
        warning_id=row["warning_id"],
        event_id=row["event_id"],
        trace_id=row["trace_id"],
        hazard_type=row["hazard_type"],
        region_codes=list(row["region_codes"]),
        risk_level=RiskLevel(int(row["risk_level"])),
        title_zh=row["title_zh"],
        body_zh=row["body_zh"],
        body_bo=row["body_bo"],
        audiences=list(row["audiences"]),
        channels=list(row["channels"]),
        translation_pending=bool(row["translation_pending"]),
        generated_at=to_iso(row["generated_at"]),
        released_at=None if row["released_at"] is None else to_iso(row["released_at"]),
        deliveries=[
            DeliveryAttempt(
                channel=r["channel"],
                audience_count=int(r["audience_count"]),
                status=r["status"],
                attempted_at=to_iso(r["attempted_at"]),
                receipt_at=None if r["receipt_at"] is None else to_iso(r["receipt_at"]),
                provider_msg_id=r["provider_msg_id"],
            )
            for r in receipts
        ],
    )


# ---------- chain_runs ----------


def chain_row(result: ChainResult, *, finished_at: datetime) -> tuple[Any, ...]:
    """链路台账行。

    风险载荷经 `ChainResult.as_dict()` 取，避免本层导入 aegis.services 的 RiskVerdict 类型；
    读侧不回建 ChainResult（其载荷含服务层类型，见 002_schema 的表注）。
    """
    payload = result.as_dict()
    check_json(payload["stages"], field="stages")
    check_json(payload["risk"], field="risk")
    return (
        check_id(result.trace_id, field="trace_id"),
        check_id(result.event_id, field="event_id"),
        bool(payload["ok"]),
        bool(payload["acted"]),
        list(payload["stages"]),
        payload["risk"],
        [hit.model_dump(mode="json") for hit in result.hits],
        [unit.task_unit_id for unit in result.task_units],
        None if result.warning is None else check_id(result.warning.warning_id, field="warning_id"),
        list(result.errors),
        list(result.degradations),
        finished_at,
    )


# ---------- workflow_definitions ----------


def workflow_def_params(definition: WorkflowDef) -> tuple[Any, ...]:
    nodes = [node.model_dump(mode="json") for node in definition.nodes]
    edges = [edge.model_dump(mode="json") for edge in definition.edges]
    check_json(nodes, field="nodes")
    check_json(edges, field="edges")
    return (
        check_id(definition.workflow_id, field="workflow_id"),
        check_text(definition.name, field="name", allow_empty=False),
        check_text(definition.description, field="description"),
        int(definition.version),
        definition.status,
        check_text(definition.created_by, field="created_by", allow_empty=False),
        nodes,
        edges,
        check_text(definition.signature(), field="signature"),
    )


def workflow_def_from_row(row: Mapping[str, Any]) -> WorkflowDef:
    return _revalidate(
        WorkflowDef,
        {
            "workflow_id": row["workflow_id"],
            "name": row["name"],
            "description": row["description"],
            "version": int(row["version"]),
            "status": row["status"],
            "created_by": row["created_by"],
            "nodes": list(row["nodes"]),
            "edges": list(row["edges"]),
        },
        field="workflow_definition",
    )


# ---------- workflow_instances / workflow_node_states ----------


def workflow_instance_params(instance: WorkflowInstance) -> tuple[Any, ...]:
    check_json(instance.payload, field="payload")
    check_json(instance.results, field="results")
    return (
        check_id(instance.instance_id, field="instance_id"),
        check_id(instance.workflow_id, field="workflow_id"),
        int(instance.workflow_version),
        check_id(instance.trace_id, field="trace_id"),
        instance.status.value,
        None if instance.error is None else check_text(instance.error, field="error"),
        dict(instance.payload),
        dict(instance.results),
        to_moment(instance.created_at),
        optional_moment(instance.finished_at),
    )


def workflow_instance_from_row(row: Mapping[str, Any], node_states: Sequence[Mapping[str, Any]] = ()) -> WorkflowInstance:
    return _revalidate(
        WorkflowInstance,
        {
            "instance_id": row["instance_id"],
            "workflow_id": row["workflow_id"],
            "workflow_version": int(row["workflow_version"]),
            "trace_id": row["trace_id"],
            "status": row["status"],
            "error": row["error"],
            "payload": dict(row["payload"]),
            "results": dict(row["results"]),
            "created_at": to_iso(row["created_at"]),
            "finished_at": None if row["finished_at"] is None else to_iso(row["finished_at"]),
            "nodes": {str(r["node_id"]): node_run_from_row(r) for r in node_states},
        },
        field="workflow_instance",
    )


def node_state_params(instance_id: str, run: NodeRun) -> tuple[Any, ...]:
    check_json(run.output, field="output")
    return (
        check_id(instance_id, field="instance_id"),
        check_id(run.node_id, field="node_id"),
        check_id(run.type, field="type"),
        run.state.value,
        int(run.attempts),
        run.schedule_latency_ms,
        run.duration_ms,
        dict(run.output),
        None if run.error is None else check_text(run.error, field="error"),
        list(run.notes),
    )


def node_run_from_row(row: Mapping[str, Any]) -> NodeRun:
    return _revalidate(
        NodeRun,
        {
            "node_id": str(row["node_id"]),
            "type": str(row["type"]),
            "state": row["state"],
            "attempts": int(row["attempts"]),
            "schedule_latency_ms": row["schedule_latency_ms"],
            "duration_ms": row["duration_ms"],
            "output": dict(row["output"]),
            "error": row["error"],
            "notes": list(row["notes"]),
        },
        field="node_run",
    )
