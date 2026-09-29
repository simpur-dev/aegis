"""契约完整性测试：schema 自身合法、schema 与 Pydantic 模型口径一致、边界值拒收。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from aegis.bus.gateway import ContractRegistry
from aegis.domain.enums import Action, MessageKind
from aegis.domain.messages import AgentMessage, StandardizedTaskUnit, make_request
from aegis.errors import ErrorCode, SchemaInvalidError

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts"


def test_contract_files_exist_and_parse() -> None:
    for name in ("agent_message.v1.schema.json", "stu.v1.schema.json"):
        path = CONTRACTS / name
        assert path.is_file(), f"契约文件缺失: {path}"
        schema = json.loads(path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)


def test_schema_required_matches_pydantic_required() -> None:
    schema = json.loads((CONTRACTS / "agent_message.v1.schema.json").read_text(encoding="utf-8"))
    schema_required = set(schema["required"])
    model_required = {name for name, field in AgentMessage.model_fields.items() if field.is_required()}
    # msg_id/trace_id/ts 等在模型侧有默认工厂，schema 侧强制存在 → 校验并集口径
    assert {"schema_version", "source", "target", "kind", "action", "payload"} <= schema_required
    assert model_required == {"source", "target", "kind", "action"}
    assert schema_required <= model_required | {"msg_id", "trace_id", "ts", "schema_version", "payload"}


def test_stu_schema_required_matches_model() -> None:
    schema = json.loads((CONTRACTS / "stu.v1.schema.json").read_text(encoding="utf-8"))
    schema_required = set(schema["required"])
    model_required = {name for name, field in StandardizedTaskUnit.model_fields.items() if field.is_required()}
    # 模型侧对 id/版本有默认工厂，schema 侧要求显式出现：允许的差异仅此两项，其余必须一致
    assert model_required <= schema_required, "契约遗漏了模型必填字段"
    assert schema_required - model_required == {"task_unit_id", "schema_version"}, "STU 必填口径漂移"


@pytest.mark.parametrize("action", sorted(a.value for a in Action))
def test_every_registered_action_passes_schema(action: str, contracts: ContractRegistry, valid_message_payload: dict) -> None:
    """防漂移：枚举里新增/改名的动作必须同时被契约 schema 接受。"""
    payload = dict(valid_message_payload)
    payload["action"] = action
    contracts.validate_message(payload)


@pytest.mark.parametrize("code", sorted(c.value for c in ErrorCode))
def test_every_error_code_passes_schema(code: str, contracts: ContractRegistry, valid_message_payload: dict) -> None:
    """防漂移：规范 §5 登记的错误码必须能通过契约（曾因 E_ 下划线被正则拒绝）。"""
    payload = dict(valid_message_payload)
    payload.pop("reply_to", None)
    payload.pop("deadline_ms", None)
    payload.update({"kind": "error", "causation_id": payload["msg_id"]})
    payload["payload"] = {"code": code, "message": "失败", "retryable": False}
    contracts.validate_message(payload)


def test_valid_request_passes(contracts: ContractRegistry) -> None:
    message = make_request(
        source="platform.gateway",
        target="assess.test01",
        action=Action.ASSESS_HAZARD.value,
        payload={"region_code": "540121"},
        trace_id="trc_" + "a" * 16,
        reply_to="reply.gw00000001.inbox",
        deadline_ms=1000,
    )
    contracts.validate_message(json.loads(message.model_dump_json(exclude_none=True)))


def test_event_with_unicast_target_is_valid(contracts: ContractRegistry) -> None:
    message = AgentMessage(
        source="perceive.test01",
        target="platform.telemetry_sink",
        kind=MessageKind.EVENT,
        action=Action.PERCEIVE_ANOMALY.value,
        payload={"station_id": "RG-01"},
        trace_id="trc_" + "b" * 16,
    )
    contracts.validate_message(json.loads(message.model_dump_json(exclude_none=True)))


def test_broadcast_only_for_events(contracts: ContractRegistry) -> None:
    with pytest.raises(ValidationError, match="广播目标"):
        AgentMessage(
            source="platform.gateway",
            target="*",
            kind=MessageKind.REQUEST,
            action=Action.ASSESS_HAZARD.value,
            payload={},
            reply_to="reply.gw1.inbox",
            deadline_ms=500,
        )


def test_request_requires_reply_and_deadline(contracts: ContractRegistry) -> None:
    with pytest.raises(ValidationError, match="reply_to"):
        AgentMessage(
            source="platform.gateway",
            target="assess.test01",
            kind=MessageKind.REQUEST,
            action=Action.ASSESS_HAZARD.value,
            payload={},
        )


def test_response_requires_causation() -> None:
    with pytest.raises(ValidationError, match="causation_id"):
        AgentMessage(
            source="assess.test01",
            target="platform.gateway",
            kind=MessageKind.RESPONSE,
            action=Action.ASSESS_HAZARD.value,
            payload={"risk_level": 2},
        )


def test_extra_property_rejected(contracts: ContractRegistry, valid_message_payload) -> None:
    payload = dict(valid_message_payload)
    payload["sneaky_field"] = "x"
    with pytest.raises(SchemaInvalidError, match="Additional properties"):
        contracts.validate_message(payload)


@pytest.mark.parametrize(
    "field,value",
    [
        ("msg_id", "msg_short"),
        ("trace_id", "trc_ZZZZ"),
        ("source", "UPPER.case"),
        ("source", "no_dot_here"),
        ("action", "noDot"),
        ("action", "single"),
        ("priority", 0),
        ("priority", 6),
        ("ttl_ms", -1),
        ("deadline_ms", 50),
        ("deadline_ms", 10_000_000),
    ],
)
def test_boundary_values_rejected_by_schema(contracts: ContractRegistry, valid_message_payload, field, value) -> None:
    payload = dict(valid_message_payload)
    payload[field] = value
    with pytest.raises(SchemaInvalidError):
        contracts.validate_message(payload)


def test_stu_boundary_values_rejected(contracts: ContractRegistry, valid_stu_payload) -> None:
    cases = [
        ("region_code", "5401"),  # 太短
        ("region_code", "540121abc"),  # 含小写
        ("sla_seconds", 0),
        ("sla_seconds", 90_000),
        ("priority", 6),
        ("required_capabilities", []),
        ("hazard_type", "volcano"),  # 不在灾种枚举
        ("task_type", "party"),
        ("owner_role", "alien"),
        ("dependencies", ["not-an-stu-id"]),
        ("outputs", ["warning_unreleased"]),
        ("fallback_policy", {"mode": "explode"}),
    ]
    for field, value in cases:
        payload = json.loads(json.dumps(valid_stu_payload))
        payload[field] = value
        with pytest.raises(SchemaInvalidError) as info:
            contracts.validate_stu(payload)
        assert field in str(info.value.detail), f"错误定位未包含字段名: {field}"


def test_stu_missing_required_rejected(contracts: ContractRegistry, valid_stu_payload) -> None:
    payload = dict(valid_stu_payload)
    del payload["fallback_policy"]
    with pytest.raises(SchemaInvalidError):
        contracts.validate_stu(payload)


def test_stu_errors_reports_index_and_path(contracts: ContractRegistry, valid_stu_payload) -> None:
    bad = json.loads(json.dumps(valid_stu_payload))
    bad["sla_seconds"] = 0
    errors = contracts.stu_errors([valid_stu_payload, bad])
    assert len(errors) == 1
    assert errors[0]["index"] == 1
    assert "sla_seconds" in errors[0]["path"]


def test_contracts_dir_missing_raises() -> None:
    with pytest.raises(FileNotFoundError):
        ContractRegistry(Path("/nonexistent/contracts-dir"))
