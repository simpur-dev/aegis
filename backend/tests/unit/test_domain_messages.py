"""AgentMessage / STU / 领域记录的构造语义与时间计算测试。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from aegis.domain.enums import Action, AgentType, MessageKind, RiskLevel
from aegis.domain.messages import (
    AgentMessage,
    CapabilityRegistration,
    DeliveryAttempt,
    Ref,
    StandardizedTaskUnit,
    TelemetryReading,
    WarningRecord,
    make_error,
    make_event,
    make_request,
    make_response,
    new_event_id,
    new_msg_id,
    new_stu_id,
    now_iso,
    parse_iso,
    utc_now,
)
from aegis.errors import ErrorCode

TRACE = "trc_" + "d" * 16


def request(**overrides: object) -> AgentMessage:
    params: dict[str, object] = {
        "source": "platform.gateway",
        "target": "assess.test01",
        "action": Action.ASSESS_HAZARD.value,
        "payload": {"region_code": "540121"},
        "trace_id": TRACE,
        "reply_to": "reply.gw00000001.inbox",
        "deadline_ms": 2_000,
    }
    params.update(overrides)
    return make_request(**params)  # type: ignore[arg-type]


class TestFactories:
    def test_id_shapes(self) -> None:
        assert new_msg_id().startswith("msg_") and len(new_msg_id()) == 20
        assert new_event_id().startswith("evt_") and len(new_event_id()) == 16
        assert new_stu_id().startswith("stu_")

    def test_ids_are_unique(self) -> None:
        assert len({new_msg_id() for _ in range(2_000)}) == 2_000


class TestMessageSemantics:
    def test_roundtrip_encode_decode(self) -> None:
        message = request(refs=[Ref(type="telemetry", id="RG-01")])
        clone = AgentMessage.decode(message.encode())
        assert clone == message

    def test_request_requires_reply_to(self) -> None:
        with pytest.raises(ValidationError, match="reply_to"):
            request(reply_to=None)

    def test_request_requires_deadline(self) -> None:
        with pytest.raises(ValidationError, match="deadline_ms"):
            request(deadline_ms=None)

    def test_deadline_at_math(self) -> None:
        message = request(deadline_ms=2_500)
        delta = (message.deadline_at - parse_iso(message.ts)).total_seconds()
        assert abs(delta - 2.5) < 0.01

    def test_ttl_expires_at_math(self) -> None:
        message = request(ttl_ms=1_000)
        assert (message.ttl_expires_at - parse_iso(message.ts)).total_seconds() == pytest.approx(1.0)

    def test_non_request_has_no_deadline_property(self) -> None:
        assert make_event(source="perceive.t01", action=Action.PERCEIVE_ANOMALY.value, payload={}, trace_id=TRACE).deadline_at is None

    def test_broadcast_requires_event(self) -> None:
        with pytest.raises(ValidationError, match="广播目标"):
            AgentMessage(
                source="perceive.t01",
                target="*",
                kind=MessageKind.REQUEST,
                action=Action.ASSESS_HAZARD.value,
                payload={},
                reply_to="reply.gw1.inbox",
                deadline_ms=500,
            )

    def test_event_broadcast_allowed(self) -> None:
        message = make_event(source="perceive.t01", action=Action.PERCEIVE_ANOMALY.value, payload={}, trace_id=TRACE)
        assert message.target == "*"

    def test_response_inherits_trace_and_targets_sender(self) -> None:
        original = request()
        reply = make_response(original, source="assess.test01", payload={"risk_level": 2})
        assert reply.trace_id == original.trace_id
        assert reply.causation_id == original.msg_id
        assert reply.target == original.source
        assert reply.msg_id != original.msg_id
        assert reply.span_id != original.span_id

    def test_error_carries_code_payload(self) -> None:
        original = request()
        err = make_error(original, source="assess.test01", code=ErrorCode.INTERNAL.value, message="boom", retryable=True)
        assert err.payload["code"] == "E_INTERNAL"
        assert err.payload["retryable"] is True
        assert err.kind is MessageKind.ERROR

    def test_payload_size_guard_via_refs(self) -> None:
        message = request(refs=[Ref(type="object", id="s3://x/" + "y" * 200)])
        assert len(message.refs) == 1

    def test_extra_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AgentMessage.model_validate(
                {
                    "schema_version": "1.0",
                    "msg_id": new_msg_id(),
                    "trace_id": TRACE,
                    "ts": now_iso(),
                    "source": "platform.gateway",
                    "target": "assess.test01",
                    "kind": "event",
                    "action": "perceive.anomaly",
                    "payload": {},
                    "unexpected": 1,
                }
            )

    def test_invalid_source_pattern(self) -> None:
        with pytest.raises(ValidationError):
            AgentMessage(
                source="assess",
                target="platform.gateway",
                kind=MessageKind.EVENT,
                action=Action.PERCEIVE_ANOMALY.value,
                payload={},
            )

    def test_priority_bounds(self) -> None:
        for bad in (0, 6):
            with pytest.raises(ValidationError):
                AgentMessage(
                    source="assess.test01",
                    target="platform.gateway",
                    kind=MessageKind.EVENT,
                    action=Action.PERCEIVE_ANOMALY.value,
                    payload={},
                    priority=bad,
                )

    def test_action_pattern(self) -> None:
        with pytest.raises(ValidationError):
            AgentMessage(
                source="assess.test01",
                target="platform.gateway",
                kind=MessageKind.EVENT,
                action="UPPER.case",
                payload={},
            )


class TestDomainRecords:
    def test_telemetry_ingest_latency(self) -> None:
        reading = TelemetryReading(
            station_id="RG-01",
            metric="rain_10min",
            value=12.0,
            unit="mm",
            region_code="540121",
            observed_at=now_iso(utc_now()),
            ingested_at=now_iso(utc_now()),
        )
        assert reading.ingest_latency_ms >= 0

    def test_warning_reach_uses_latest_receipt(self) -> None:
        warning = WarningRecord(
            event_id=new_event_id(),
            trace_id=TRACE,
            hazard_type="debris_flow",
            region_codes=["540121"],
            risk_level=RiskLevel.RED,
            title_zh="红",
            body_zh="正文",
            released_at="2026-09-29T01:00:00.000Z",
            deliveries=[
                DeliveryAttempt(
                    channel="sms",
                    audience_count=3,
                    status="delivered",
                    attempted_at="2026-09-29T01:00:01.000Z",
                    receipt_at="2026-09-29T01:00:10.000Z",
                ),
                DeliveryAttempt(
                    channel="beidou",
                    audience_count=3,
                    status="delivered",
                    attempted_at="2026-09-29T01:00:02.000Z",
                    receipt_at="2026-09-29T01:00:30.000Z",
                ),
            ],
        )
        assert warning.reach_seconds() == 30.0

    def test_warning_reach_none_without_receipt(self) -> None:
        warning = WarningRecord(
            event_id=new_event_id(),
            trace_id=TRACE,
            hazard_type="debris_flow",
            region_codes=["540121"],
            risk_level=RiskLevel.BLUE,
            title_zh="蓝",
            body_zh="正文",
            released_at="2026-09-29T01:00:00.000Z",
        )
        assert warning.reach_seconds() is None

    def test_capability_registration_bounds(self) -> None:
        with pytest.raises(ValidationError):
            CapabilityRegistration(agent_id="assess.test01", agent_type="assess", capabilities=[])
        with pytest.raises(ValidationError):
            CapabilityRegistration(agent_id="assess.test01", agent_type="unknown_type", capabilities=["x"])

    def test_parse_iso_accepts_offset_and_z(self) -> None:
        assert parse_iso("2026-09-29T01:00:00Z") == parse_iso("2026-09-29T01:00:00+00:00")

    def test_now_iso_is_utc_z_suffix(self) -> None:
        assert now_iso().endswith("Z")

    def test_stu_id_pattern_enforced(self) -> None:
        with pytest.raises(ValidationError):
            StandardizedTaskUnit.model_validate({"task_unit_id": "stu_bad", "event_id": new_event_id()})

    def test_agent_type_enum_covers_five_roles(self) -> None:
        assert {t.value for t in AgentType} == {"perceive", "assess", "plan", "execute", "feedback"}
