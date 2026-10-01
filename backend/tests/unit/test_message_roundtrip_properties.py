"""AgentMessage v1 编解码性质测试：契约是跨进程唯一边界，往返必须无损且幂等。

智能体由队友实现，平台与它们之间只有这条 JSON 线。这里守的不是某个字段，而是
"任何合法消息经过 encode→decode 都必须与原消息完全相等"这条更强的整体性质——
字面量用例只能覆盖想到的形状，随机生成才能覆盖没想到的。
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from aegis.bus.gateway import AegisError, ContractRegistry
from aegis.bus.transport import decode, encode
from aegis.domain.enums import RefType
from aegis.domain.messages import AgentMessage, Ref, make_event

json_values: st.SearchStrategy[Any] = st.recursive(
    st.none()
    | st.booleans()
    | st.integers(min_value=-(10**9), max_value=10**9)
    | st.floats(allow_nan=False, allow_infinity=False)
    | st.text(max_size=24),
    lambda children: st.lists(children, max_size=4) | st.dictionaries(st.text(max_size=12), children, max_size=4),
    max_leaves=12,
)
payloads = st.dictionaries(st.text(min_size=1, max_size=12), json_values, max_size=6)
hex16 = st.integers(min_value=0, max_value=16**16 - 1).map(lambda n: f"{n:016x}")


@st.composite
def events(draw: st.DrawMethod) -> AgentMessage:
    return make_event(
        source=f"platform.connector_{draw(st.integers(min_value=0, max_value=999))}",
        action="telemetry.reading.received",
        payload=draw(payloads),
        trace_id="trc_" + draw(hex16),
        priority=draw(st.integers(min_value=1, max_value=5)),
        ttl_ms=draw(st.integers(min_value=0, max_value=120_000)),
    )


class TestRoundTrip:
    @given(message=events())
    @settings(max_examples=100, deadline=None)
    def test_encode_decode_is_lossless(self, message: AgentMessage) -> None:
        assert decode(encode(message)) == message

    @given(message=events())
    @settings(max_examples=60, deadline=None)
    def test_wire_format_is_json_with_the_frozen_schema_version(self, message: AgentMessage) -> None:
        raw = encode(message)
        assert isinstance(raw, bytes)
        document = json.loads(raw.decode("utf-8"))
        assert document["schema_version"] == "1.0"
        assert document["msg_id"] == message.msg_id

    @given(message=events())
    @settings(max_examples=60, deadline=None)
    def test_encoding_is_idempotent(self, message: AgentMessage) -> None:
        """两次编码字节相同：总线重投与去重都依赖这个性质，否则同一条消息会有两个指纹。"""
        once = encode(message)
        assert encode(decode(once)) == once

    @given(payload=payloads)
    @settings(max_examples=60, deadline=None)
    def test_arbitrary_json_payload_survives(self, payload: dict[str, Any]) -> None:
        """payload 是开放对象：智能体放什么都必须原样到达，不能因编码选型丢键或改型。"""
        message = make_event(source="platform.agent_mock", action="assess.risk_level", payload=payload, trace_id="trc_" + "0" * 15 + "a")
        restored = decode(encode(message))
        assert restored.payload == message.payload

    @given(count=st.integers(min_value=0, max_value=4))
    @settings(max_examples=5, deadline=None)
    def test_refs_are_preserved_in_order(self, count: int) -> None:
        refs = [Ref(type=list(RefType)[i % len(RefType)], id=f"ref-{i}") for i in range(count)]
        message = make_event(
            source="platform.agent_mock",
            action="plan.task_units",
            payload={},
            trace_id="trc_" + "1" * 16,
            refs=refs,
        )
        assert decode(encode(message)).refs == refs


class TestContractRejection:
    """不合式的消息必须在解码期就被拒，而不是带病进链路。"""

    def test_extra_field_is_forbidden(self) -> None:
        message = make_event(source="platform.agent_mock", action="assess.risk_level", payload={}, trace_id="trc_" + "2" * 16)
        document = json.loads(encode(message).decode())
        document["unexpected"] = 1
        with pytest.raises(ValidationError):
            decode(json.dumps(document).encode())

    @pytest.mark.parametrize("field", ["source", "target", "action", "kind"])
    def test_fields_without_defaults_are_required_on_the_wire(self, field: str) -> None:
        message = make_event(source="platform.agent_mock", action="assess.risk_level", payload={}, trace_id="trc_" + "3" * 16)
        document = json.loads(encode(message).decode())
        document.pop(field)
        with pytest.raises(ValidationError):
            decode(json.dumps(document).encode())

    @pytest.mark.parametrize("field", ["msg_id", "trace_id"])
    def test_defaulted_ids_pass_the_codec_but_are_rejected_at_the_gateway(self, field: str) -> None:
        """模型给 msg_id/trace_id 留了构造默认值，但它们在网络上是必填字段。

        这正是"解码 + 网关契约校验"两段缺一不可的理由：少了网关这一环，
        一条没带 msg_id 的消息会被静默补号，去重与证据链就此失效。
        """
        message = make_event(source="platform.agent_mock", action="assess.risk_level", payload={}, trace_id="trc_" + "6" * 16)
        document = json.loads(encode(message).decode())
        document.pop(field)
        assert decode(json.dumps(document).encode()) is not None  # 解码期不拦（默认值补齐）
        with pytest.raises(AegisError):
            ContractRegistry().validate_message(document)

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("trace_id", "trc_ZZZZ"),
            ("trace_id", "msg_" + "0" * 16),
            ("msg_id", "msg_zzz"),
            ("priority", 9),
            ("priority", 0),
            ("deadline_ms", 1),
            ("schema_version", "2.0"),
        ],
    )
    def test_out_of_range_or_malformed_values_rejected(self, field: str, value: Any) -> None:
        message = make_event(source="platform.agent_mock", action="assess.risk_level", payload={}, trace_id="trc_" + "4" * 16)
        document = json.loads(encode(message).decode())
        document[field] = value
        with pytest.raises(ValidationError):
            decode(json.dumps(document).encode())

    def test_non_json_bytes_do_not_decode(self) -> None:
        with pytest.raises(Exception):  # noqa: B017 - 解码器异常类型不作契约，只要求"必然抛"
            decode(b"\x00\x01not json")

    def test_deadline_bounds_match_the_sla_window(self) -> None:
        """deadline 上限 600s = 10min：比最长考核窗口（≤3min 预警生成）宽，但不允许无限期挂请求。

        必须走构造而不是 `model_copy`：后者不重新校验，用它写断言等于什么都没测。
        """
        base = make_event(source="platform.agent_mock", action="assess.risk_level", payload={}, trace_id="trc_" + "5" * 16).model_dump()
        assert AgentMessage(**base | {"deadline_ms": 600_000}).deadline_ms == 600_000
        with pytest.raises(ValidationError):
            AgentMessage(**base | {"deadline_ms": 600_001})
