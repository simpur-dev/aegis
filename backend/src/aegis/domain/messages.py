"""契约的 Python 镜像与领域记录。AgentMessage / STU 与 `contracts/*.schema.json` 严格同构。"""

from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from aegis.domain.enums import (
    FallbackMode,
    MessageKind,
    OwnerRole,
    RefType,
    RiskLevel,
    TaskType,
)

_SOURCE_PATTERN = r"^(platform|[a-z][a-z0-9_]{2,31})\.[a-z][a-z0-9_-]{0,31}$"
_TARGET_PATTERN = r"^(\*|platform\.[a-z][a-z0-9_-]{0,31}|[a-z][a-z0-9_]{2,31}\.[a-z][a-z0-9_-]{0,31})$"
_MSG_ID_PATTERN = r"^msg_[0-9a-f]{16}$"


def now_iso(moment: datetime | None = None) -> str:
    dt = moment or datetime.now(UTC)
    return dt.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def utc_now() -> datetime:
    return datetime.now(UTC)


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def new_msg_id() -> str:
    return f"msg_{secrets.token_hex(8)}"


def new_trace_id() -> str:
    return f"trc_{secrets.token_hex(8)}"


def new_span_id() -> str:
    return f"spn_{secrets.token_hex(4)}"


def new_event_id() -> str:
    return f"evt_{secrets.token_hex(6)}"


def new_stu_id() -> str:
    return f"stu_{secrets.token_hex(8)}"


def new_warning_id() -> str:
    return f"wrn_{secrets.token_hex(10)}"


def new_workflow_instance_id() -> str:
    return f"wfi_{secrets.token_hex(8)}"


class Ref(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: RefType
    id: str = Field(min_length=1, max_length=256)
    note: str | None = Field(default=None, max_length=256)


class AgentMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    msg_id: str = Field(pattern=_MSG_ID_PATTERN, default_factory=new_msg_id)
    trace_id: str = Field(pattern=r"^trc_[0-9a-f]{16}$", default_factory=new_trace_id)
    span_id: str | None = Field(default=None, pattern=r"^spn_[0-9a-f]{8}$")
    causation_id: str | None = Field(default=None, pattern=_MSG_ID_PATTERN)
    ts: str = Field(default_factory=now_iso)
    source: str = Field(pattern=_SOURCE_PATTERN)
    target: str = Field(pattern=_TARGET_PATTERN)
    kind: MessageKind
    action: str = Field(pattern=r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*){1,2}$")
    priority: int = Field(default=3, ge=1, le=5)
    ttl_ms: int = Field(default=30_000, ge=0)
    payload: dict[str, Any] = Field(default_factory=dict)
    refs: list[Ref] = Field(default_factory=list)
    reply_to: str | None = None
    deadline_ms: int | None = Field(default=None, ge=100, le=600_000)

    @model_validator(mode="after")
    def _kind_semantics(self) -> AgentMessage:
        if self.kind is MessageKind.REQUEST:
            if not self.reply_to:
                raise ValueError("request 必须携带 reply_to")
            if not self.deadline_ms:
                raise ValueError("request 必须携带 deadline_ms")
        elif self.kind in (MessageKind.RESPONSE, MessageKind.ERROR) and not self.causation_id:
            raise ValueError(f"{self.kind.value} 必须携带 causation_id")
        if self.target == "*" and self.kind is not MessageKind.EVENT:
            raise ValueError("广播目标 '*' 仅允许 event 类消息")
        return self

    @property
    def deadline_at(self) -> datetime | None:
        if self.kind is not MessageKind.REQUEST:
            return None
        return parse_iso(self.ts) + timedelta(milliseconds=self.deadline_ms or 0)

    @property
    def ttl_expires_at(self) -> datetime:
        return parse_iso(self.ts) + timedelta(milliseconds=self.ttl_ms)

    def encode(self) -> bytes:
        return self.model_dump_json(exclude_none=True).encode()

    @classmethod
    def decode(cls, raw: bytes | str) -> AgentMessage:
        return cls.model_validate(json.loads(raw if isinstance(raw, str) else raw.decode()))


class CapabilityRegistration(BaseModel):
    """智能体启动时向能力注册中心声明的能力集（规范 §6）。"""

    agent_id: str = Field(pattern=r"^[a-z][a-z0-9_]{2,31}\.[a-z][a-z0-9_-]{0,31}$")
    agent_type: str = Field(pattern=r"^(perceive|assess|plan|execute|feedback)$")
    capabilities: list[str] = Field(min_length=1, max_length=16)
    hazard_types: list[str] = Field(default_factory=list)
    max_concurrency: int = Field(default=4, ge=1, le=64)
    version: str = "0.0.0"
    registered_at: str = Field(default_factory=now_iso)


class FallbackPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: FallbackMode
    max_retries: int = Field(default=2, ge=0, le=5)
    retry_backoff_ms: int = Field(default=1_000, ge=100)
    transfer_to: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{2,31}$")
    escalate_to_role: OwnerRole | None = None


class DataRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: RefType
    id: str = Field(min_length=1, max_length=256)


class StandardizedTaskUnit(BaseModel):
    """STU v1：决策智能体产出、工作流引擎消费的标准化任务单元。"""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    task_unit_id: str = Field(pattern=r"^stu_[0-9a-f]{16}$", default_factory=new_stu_id)
    event_id: str = Field(pattern=r"^evt_[0-9a-f]{12}$")
    hazard_type: str
    region_code: str = Field(pattern=r"^[0-9A-Z]{6,24}$")
    task_type: TaskType
    objective: str = Field(min_length=4, max_length=512)
    priority: int = Field(ge=1, le=5)
    sla_seconds: int = Field(ge=1, le=86_400)
    required_capabilities: list[str] = Field(min_length=1, max_length=8)
    owner_role: OwnerRole
    trigger_refs: list[str] = Field(default_factory=list)
    input_data_refs: list[DataRef] = Field(default_factory=list)
    outputs: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    fallback_policy: FallbackPolicy
    context_snapshot: dict[str, Any] = Field(default_factory=dict)
    created_by: str = Field(pattern=_SOURCE_PATTERN)
    created_at: str = Field(default_factory=now_iso)

    @property
    def deadline_at(self) -> datetime:
        return parse_iso(self.created_at) + timedelta(seconds=self.sla_seconds)


class TelemetryReading(BaseModel):
    station_id: str
    metric: str
    value: float
    unit: str
    region_code: str = Field(pattern=r"^[0-9A-Z]{6,24}$")
    observed_at: str = Field(default_factory=now_iso)
    ingested_at: str = Field(default_factory=now_iso)
    source: str = "simulator"
    quality_flag: str = Field(default="ok", pattern=r"^(ok|suspect|missing|drift)$")

    @property
    def ingest_latency_ms(self) -> float:
        return (parse_iso(self.ingested_at) - parse_iso(self.observed_at)).total_seconds() * 1000


class TriggerHit(BaseModel):
    rule_id: str
    hazard_type: str
    region_code: str
    evidence_refs: list[str] = Field(default_factory=list)
    score: float = Field(ge=0.0, le=1.0)
    observed_at: str = Field(default_factory=now_iso)


class RiskAssessment(BaseModel):
    hazard_type: str
    region_code: str
    risk_level: RiskLevel
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str
    evidence_refs: list[str] = Field(default_factory=list)
    assessed_at: str = Field(default_factory=now_iso)
    assessed_by: str = "platform.assess_rule"


class DeliveryAttempt(BaseModel):
    channel: str
    audience_count: int = Field(ge=0)
    status: str = Field(pattern=r"^(pending|delivered|failed|retried)$")
    attempted_at: str = Field(default_factory=now_iso)
    receipt_at: str | None = None
    provider_msg_id: str | None = None


class WarningRecord(BaseModel):
    """预警产物：生成时间（≤3min 指标）与触达时间（≤20min 指标）在此记录。"""

    warning_id: str = Field(default_factory=new_warning_id)
    event_id: str
    trace_id: str
    hazard_type: str
    region_codes: list[str] = Field(min_length=1)
    risk_level: RiskLevel
    title_zh: str
    body_zh: str
    body_bo: str | None = None
    audiences: list[str] = Field(default_factory=list)
    channels: list[str] = Field(default_factory=list)
    translation_pending: bool = False
    generated_at: str = Field(default_factory=now_iso)
    released_at: str | None = None
    deliveries: list[DeliveryAttempt] = Field(default_factory=list)

    def reach_seconds(self) -> float | None:
        if not self.released_at:
            return None
        receipts = [d.receipt_at for d in self.deliveries if d.receipt_at]
        if not receipts:
            return None
        return (max(parse_iso(r) for r in receipts) - parse_iso(self.released_at)).total_seconds()


class FeedbackReport(BaseModel):
    warning_id: str
    reach_stats: dict[str, Any] = Field(default_factory=dict)
    response_state: str = Field(default="unknown")
    effect_score: float | None = Field(default=None, ge=0.0, le=1.0)
    observed_at: str = Field(default_factory=now_iso)


def make_event(
    *,
    source: str,
    action: str,
    payload: dict[str, Any],
    trace_id: str,
    target: str = "*",
    refs: list[Ref] | None = None,
    priority: int = 3,
    ttl_ms: int = 30_000,
    span_id: str | None = None,
) -> AgentMessage:
    return AgentMessage(
        source=source,
        target=target,
        kind=MessageKind.EVENT,
        action=action,
        payload=payload,
        trace_id=trace_id,
        refs=refs or [],
        priority=priority,
        ttl_ms=ttl_ms,
        span_id=span_id or new_span_id(),
    )


def make_request(
    *,
    source: str,
    target: str,
    action: str,
    payload: dict[str, Any],
    trace_id: str,
    reply_to: str,
    deadline_ms: int = 10_000,
    refs: list[Ref] | None = None,
    priority: int = 3,
    ttl_ms: int = 30_000,
    span_id: str | None = None,
) -> AgentMessage:
    return AgentMessage(
        source=source,
        target=target,
        kind=MessageKind.REQUEST,
        action=action,
        payload=payload,
        trace_id=trace_id,
        refs=refs or [],
        priority=priority,
        ttl_ms=ttl_ms,
        reply_to=reply_to,
        deadline_ms=deadline_ms,
        span_id=span_id or new_span_id(),
    )


def make_response(request: AgentMessage, *, source: str, payload: dict[str, Any], refs: list[Ref] | None = None) -> AgentMessage:
    return AgentMessage(
        msg_id=new_msg_id(),
        trace_id=request.trace_id,
        span_id=new_span_id(),
        causation_id=request.msg_id,
        source=source,
        target=request.source,
        kind=MessageKind.RESPONSE,
        action=request.action,
        priority=request.priority,
        payload=payload,
        refs=refs or [],
    )


def make_error(
    request: AgentMessage,
    *,
    source: str,
    code: str,
    message: str,
    detail: dict[str, Any] | None = None,
    retryable: bool = False,
) -> AgentMessage:
    return AgentMessage(
        trace_id=request.trace_id,
        span_id=new_span_id(),
        causation_id=request.msg_id,
        source=source,
        target=request.source,
        kind=MessageKind.ERROR,
        action=request.action,
        priority=request.priority,
        payload={"code": code, "message": message, "retryable": retryable, "detail": detail or {}},
    )
