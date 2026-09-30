"""SLA 跨度助手测试：账本同源计时、预算与违约属性、异常语义、契约 trace_id 与 W3C 的映射。"""

from __future__ import annotations

import asyncio
import json

import pytest
from opentelemetry.trace import StatusCode

from aegis.bus.gateway import ContractRegistry
from aegis.config import Settings
from aegis.domain.enums import Action, MessageKind
from aegis.domain.messages import AgentMessage, now_iso
from aegis.errors import SchemaInvalidError
from aegis.observability import telemetry
from aegis.observability.instrumentation import (
    BREACH_ATTR,
    OVER_MS_ATTR,
    TRACE_ID_ATTR,
    register_sla_budgets,
    span_stage,
)
from aegis.observability.metrics import MetricsExporter
from aegis.observability.tracer import Tracer

TRACE = "trc_" + "4bf92f3577b34da6"
SPAN = "spn_" + "a3ce929d"
W3C_TRACE_HEX = "00000000000000004bf92f3577b34da6"


class _Boom(RuntimeError):
    """业务异常替身：用于验证跨度不改变异常类型与实例。"""


@pytest.fixture(autouse=True)
def _local_recording_provider():
    """默认装配"记录但不导出"的 provider：跨度属性可断言，且不依赖任何服务。"""
    telemetry.init_telemetry(deployment_environment="test", otlp_endpoint="")
    yield
    telemetry.shutdown_telemetry()
    telemetry._spans_dropped = 0


class TestW3cIdMapping:
    def test_contract_trace_id_maps_to_32_hex_by_left_zero_padding(self) -> None:
        assert telemetry.w3c_trace_id(TRACE) == W3C_TRACE_HEX
        assert int(W3C_TRACE_HEX, 16) != 0, "全零 trace id 在 W3C 非法，必须被拒绝"
        assert telemetry.w3c_span_id(SPAN) == "00000000a3ce929d"

    def test_mapping_is_reversible_to_the_contract_key(self) -> None:
        hex_id = telemetry.w3c_trace_id(TRACE)
        assert hex_id is not None
        assert f"trc_{hex_id[-16:]}" == TRACE, "验收报告里的 Jaeger trace 必须能反查回消息 trace_id"

    @pytest.mark.parametrize(
        "value",
        [
            None,
            "",
            "trc_4bf92f3577b34d",
            "trc_4bf92f3577b34da6zz",
            "4bf92f3577b34da6a3ce929d0e0e4736",
            "TRC_4BF92F3577B34DA6",
            "trc_0000000000000000",
        ],
        ids=["none", "empty", "short", "non-hex", "bare-32-hex", "uppercase", "all-zero"],
    )
    def test_illegal_or_unsafe_ids_return_none(self, value: str | None) -> None:
        assert telemetry.w3c_trace_id(value) is None

    def test_contract_still_accepts_trc_prefix_and_rejects_bare_otel_id(self, contracts: ContractRegistry) -> None:
        """决策留证：契约 pattern 是 ^trc_[0-9a-f]{16}$，32 hex 裸 ID 会被网关拒收，
        因此 OTel trace id 只作为跨度属性/映射存在，契约 trace_id 仍是关联主键。"""
        pattern = json.loads((contracts.contracts_dir / "agent_message.v1.schema.json").read_text(encoding="utf-8"))["properties"][
            "trace_id"
        ]["pattern"]
        assert pattern == "^trc_[0-9a-f]{16}$"

        def build(trace_id: str) -> dict[str, object]:
            return {
                "schema_version": "1.0",
                "msg_id": "msg_" + "1" * 16,
                "trace_id": trace_id,
                "ts": now_iso(),
                "source": "platform.gateway",
                "target": "assess.t01",
                "kind": "event",
                "action": Action.PERCEIVE_ANOMALY.value,
                "payload": {},
            }

        contracts.validate_message(build(TRACE))
        with pytest.raises(SchemaInvalidError, match=r"\^trc_\[0-9a-f\]\{16\}\$"):
            contracts.validate_message(build(W3C_TRACE_HEX))


class TestSpanStageLedger:
    async def test_duration_lands_in_the_existing_ledger(self) -> None:
        tracer = Tracer()
        async with span_stage("stage_assess_ms", 5_000, tracer=tracer, trace_id=TRACE):
            await asyncio.sleep(0.02)

        stats = tracer.ledger.stats("stage_assess_ms")
        assert stats.count == 1
        assert stats.p50 >= 15.0, "计时必须真实（不是 0）"
        assert stats.budget_ms == 5_000, "预算需登记进账本，与 latency_report 同源"
        assert tracer.ledger.stats("stage_assess_ms").breaches == 0

    async def test_breach_attribute_and_over_ms_when_past_budget(self) -> None:
        tracer = Tracer()
        async with span_stage("sync_agent_to_gateway_ms", 5, tracer=tracer, trace_id=TRACE) as span:
            await asyncio.sleep(0.03)

        assert span.attributes[BREACH_ATTR] is True
        assert span.attributes["aegis.sla.budget_ms"] == 5.0
        assert span.attributes[OVER_MS_ATTR] > 0
        assert tracer.ledger.stats("sync_agent_to_gateway_ms").breaches == 1
        assert tracer.ledger.by_trace(TRACE)[0].name == "sync_agent_to_gateway_ms"

    async def test_budget_none_falls_back_to_ledger_budget(self) -> None:
        tracer = Tracer()
        tracer.ledger.set_budget("workflow_schedule_ms", 10.0)
        async with span_stage("workflow_schedule_ms", None, tracer=tracer) as span:
            await asyncio.sleep(0.03)
        assert span.attributes[BREACH_ATTR] is True
        assert span.attributes["aegis.sla.budget_ms"] == 10.0

    async def test_extra_attributes_are_written_on_the_span(self) -> None:
        tracer = Tracer()
        async with span_stage("warning_reach_ms", 1_000, tracer=tracer, region_code="540121", attempt=2, note="三通道并发") as span:
            assert span.attributes["region_code"] == "540121"
        assert span.attributes["attempt"] == 2
        assert span.attributes["note"] == "三通道并发"
        assert span.attributes["aegis.metric"] == "warning_reach_ms"

    async def test_without_initialization_ledger_still_records(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """未装配遥测（离线运行）时账本照常计时——遥测不是度量的前置条件。"""
        telemetry.shutdown_telemetry()
        monkeypatch.setattr(telemetry, "_OTEL_AVAILABLE", False)
        tracer = Tracer()
        async with span_stage("ingest_end_to_end_seconds", 1_000, tracer=tracer, trace_id=TRACE) as span:
            assert span.is_recording() is False
        assert tracer.ledger.stats("ingest_end_to_end_seconds").count == 1


class TestSpanStageErrorSemantics:
    async def test_exception_sets_error_status_and_propagates_unchanged(self) -> None:
        tracer = Tracer()
        boom = _Boom("研判智能体不可达")
        captured: _Boom | None = None
        with pytest.raises(_Boom) as info:
            async with span_stage("collab_txn", 1_000, tracer=tracer, trace_id=TRACE) as span:
                raise boom
        captured = info.value
        assert captured is boom, "跨度不得包装或替换业务异常"
        assert span.status.status_code is StatusCode.ERROR
        assert [event.name for event in span.events] == ["exception"]
        assert "研判智能体不可达" in str(span.events[0].attributes["exception.message"])
        assert span.attributes[BREACH_ATTR] is False

    async def test_failed_stage_still_counts_in_the_ledger_as_error(self) -> None:
        tracer = Tracer()
        with pytest.raises(_Boom):
            async with span_stage("collab_txn", 1_000, tracer=tracer, trace_id=TRACE, agent_id="assess.t01"):
                raise _Boom("超时")
        samples = tracer.ledger.stats("collab_txn").as_dict()
        assert samples["count"] == 1
        outcome = tracer.ledger._samples[-1].outcome  # 读侧断言：失败样本以 error 归类
        assert outcome == "error"
        assert tracer.ledger.stats("collab_txn", outcome="error").count == 1

    async def test_cancellation_is_recorded_as_cancelled(self) -> None:
        tracer = Tracer()
        with pytest.raises(asyncio.CancelledError):
            async with span_stage("stage_execute_ms", 1_000, tracer=tracer) as span:
                raise asyncio.CancelledError
        assert span.attributes["aegis.sla.outcome"] == "cancelled"
        assert tracer.ledger.stats("stage_execute_ms", outcome="cancelled").count == 1

    async def test_span_stage_is_reentrant_under_exception_without_leaking(self) -> None:
        tracer = Tracer()
        for _ in range(3):
            with pytest.raises(_Boom):
                async with span_stage("workflow_node_ms", 10, tracer=tracer):
                    raise _Boom("节点失败")
        assert tracer.ledger.stats("workflow_node_ms").count == 3


class TestTraceCorrelation:
    async def test_nested_stages_share_the_contract_derived_trace_id(self) -> None:
        tracer = Tracer()
        expected = int(W3C_TRACE_HEX, 16)
        async with span_stage("stage_plan_ms", 1_000, tracer=tracer, trace_id=TRACE) as outer:
            assert outer.get_span_context().trace_id == expected
            async with span_stage("collab_txn", 1_000, tracer=tracer, trace_id=TRACE) as inner:
                assert inner.get_span_context().trace_id == expected, "同一次灾害事件必须落在同一条 trace"
                assert inner.parent is not None
                assert inner.parent.span_id == outer.get_span_context().span_id, "内层应挂在当前跨度下"
        assert outer.attributes[TRACE_ID_ATTR] == TRACE

    async def test_missing_contract_trace_id_yields_a_fresh_valid_trace(self) -> None:
        tracer = Tracer()
        async with span_stage("workflow_instance_ms", 1_000, tracer=tracer) as span:
            assert span.get_span_context().trace_id not in (0, int(W3C_TRACE_HEX, 16))
            assert span.get_span_context().is_valid is True

    async def test_dropped_span_counter_is_exposed_for_metrics(self) -> None:
        assert telemetry.dropped_span_count() == 0
        telemetry.note_export_failure(3)
        assert telemetry.dropped_span_count() == 3


class TestSlaBudgets:
    def test_budgets_registered_from_settings(self, settings: Settings) -> None:
        tracer = Tracer()
        budgets = register_sla_budgets(tracer.ledger, settings)

        assert budgets["sync_agent_to_gateway_ms"] == 3_000.0
        assert budgets["workflow_schedule_ms"] == 2_000.0
        assert budgets["workflow_reschedule_ms"] == 10_000.0
        assert budgets["warning_reach_ms"] == 1_200_000.0
        assert tracer.ledger.budget_for("warning_generation_ms") == 180_000.0

    def test_ingest_budget_uses_the_emitted_unit_not_milliseconds(self, settings: Settings) -> None:
        """`ingest_end_to_end_seconds` 上报的是秒：预算必须同为秒，否则 6 分钟的接入时延也判不出违约。"""
        tracer = Tracer()
        register_sla_budgets(tracer.ledger, settings)
        tracer.ledger.record("ingest_end_to_end_seconds", 301.0, trace_id=TRACE)
        assert tracer.ledger.stats("ingest_end_to_end_seconds").breaches == 1

    def test_threshold_gauges_are_populated_in_seconds(self, settings: Settings, gateway, tracer: Tracer) -> None:
        exporter = MetricsExporter(gateway, tracer)
        budgets = register_sla_budgets(tracer.ledger, settings, exporter)
        text = exporter.collect().decode()
        assert 'aegis_sla_threshold_seconds{metric="sync_agent_to_gateway_ms"} 3' in text
        assert 'aegis_sla_threshold_seconds{metric="ingest_end_to_end_seconds"} 300' in text
        assert budgets["ingest_end_to_end_seconds"] == 300.0


class TestMetricsExport:
    async def test_breach_and_dropped_span_series_are_scrapable(self, gateway, tracer: Tracer) -> None:
        exporter = MetricsExporter(gateway, tracer)
        async with span_stage("sync_agent_to_gateway_ms", 5, tracer=tracer, trace_id=TRACE):
            await asyncio.sleep(0.03)
        telemetry.note_export_failure(2)

        text = exporter.collect().decode()
        assert 'aegis_sla_breaches_total{metric="sync_agent_to_gateway_ms"} 1.0' in text
        assert 'aegis_latency_ms_count{metric="sync_agent_to_gateway_ms"} 1.0' in text
        assert "aegis_telemetry_dropped_spans_total 2.0" in text

    async def test_registered_budgets_make_plain_record_sites_breakeable(self, settings: Settings, gateway, tracer: Tracer) -> None:
        """既有 `tracer.record` 埋点（未改造调用点）在预算登记后同样进入违约计数。"""
        register_sla_budgets(tracer.ledger, settings)
        tracer.ledger.record("workflow_schedule_ms", 2_500.0, trace_id=TRACE)
        text = MetricsExporter(gateway, tracer).collect().decode()
        assert 'aegis_sla_breaches_total{metric="workflow_schedule_ms"} 1.0' in text

    async def test_in_process_agent_message_stays_contract_valid(self, contracts: ContractRegistry, tracer: Tracer) -> None:
        """埋点不得改动消息本身：契约 trace_id 仍是 AgentMessage 的合法字段。"""
        message = AgentMessage(
            source="platform.gateway",
            target="assess.t01",
            kind=MessageKind.EVENT,
            action=Action.PERCEIVE_ANOMALY.value,
            payload={},
            trace_id=TRACE,
            span_id=SPAN,
        )
        async with span_stage("collab_txn", 1_000, tracer=tracer, trace_id=message.trace_id, span_id=message.span_id):
            pass
        contracts.validate_message(json.loads(message.model_dump_json(exclude_none=True)))
        assert message.trace_id == TRACE
