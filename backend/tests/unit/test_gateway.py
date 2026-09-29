"""总线网关测试：契约校验、幂等去重、动作白名单、协同事务台账、时延埋点、上行处理与注册。"""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta

import pytest

from aegis.bus import subjects
from aegis.bus.gateway import AgentGateway, ContractRegistry, TxnRecord
from aegis.bus.inmemory import InMemoryBus
from aegis.bus.registry import AgentRegistry
from aegis.domain.enums import Action, AgentType, MessageKind, RiskLevel
from aegis.domain.messages import (
    AgentMessage,
    CapabilityRegistration,
    TelemetryReading,
    make_error,
    make_event,
    make_response,
    now_iso,
    utc_now,
)
from aegis.errors import (
    AegisError,
    DeadlineExceededError,
    ErrorCode,
    NoCapableAgentError,
    SchemaInvalidError,
    UnregisteredActionError,
)
from aegis.observability.tracer import Tracer

TRACE = "trc_" + "f" * 16


def register(registry: AgentRegistry, agent_type: AgentType, capability: str = "risk_assess") -> str:
    agent_id = f"{agent_type.value}.t01"
    registry.register(
        CapabilityRegistration(
            agent_id=agent_id,
            agent_type=agent_type.value,
            capabilities=[capability],
            hazard_types=["debris_flow", "landslide", "rockfall", "avalanche", "lake_outburst"],
        )
    )
    return agent_id


async def attach_responder(
    bus: InMemoryBus,
    agent_type: AgentType,
    *,
    agent_id: str,
    delay_ms: int = 0,
    payload: dict | None = None,
) -> None:
    async def _handle(message: AgentMessage) -> None:
        if delay_ms:
            await asyncio.sleep(delay_ms / 1000)
        await bus.publish(
            message.reply_to or "",
            make_response(message, source=agent_id, payload=payload or {"risk_level": 2}),
        )

    await bus.subscribe(subjects.agent_in(agent_type), _handle)


class TestValidation:
    def test_valid_message_passes(self, gateway: AgentGateway) -> None:
        gateway.validate(make_event(source="perceive.t01", action=Action.PERCEIVE_ANOMALY.value, payload={}, trace_id=TRACE))

    def test_unregistered_action_rejected(self, gateway: AgentGateway) -> None:
        with pytest.raises(UnregisteredActionError, match="未注册动作"):
            gateway.validate(make_event(source="perceive.t01", action="perceive.teleport", payload={}, trace_id=TRACE))

    async def test_permissive_mode_allows_unknown_action(
        self, bus: InMemoryBus, registry: AgentRegistry, contracts: ContractRegistry, tracer: Tracer
    ) -> None:
        permissive = AgentGateway(bus, registry, contracts, tracer, allow_unregistered_action=True)
        await permissive.start()
        await permissive.publish_to(
            subjects.agent_out("perceive"),
            make_event(source="perceive.t01", action="perceive.teleport", payload={}, trace_id=TRACE),
        )
        await bus.idle()
        assert permissive.counters["outbound"] == 1
        await permissive.close()

    def test_schema_violation_rejected(self, gateway: AgentGateway) -> None:
        illegal = make_event(source="perceive.t01", action=Action.PERCEIVE_ANOMALY.value, payload={}, trace_id=TRACE)
        data = illegal.model_dump(exclude_none=True)
        data["refs"] = [{"type": "unknown_ref", "id": "x"}]
        with pytest.raises(SchemaInvalidError):
            gateway._contracts.validate_message(data)


class TestIdempotency:
    def test_duplicate_detection(self, gateway: AgentGateway) -> None:
        msg_id = make_event(source="perceive.t01", action=Action.PERCEIVE_ANOMALY.value, payload={}, trace_id=TRACE).msg_id
        assert gateway.is_duplicate(msg_id) is False
        assert gateway.is_duplicate(msg_id) is True
        assert gateway.counters["duplicate"] == 1

    def test_dedup_cache_evicts_oldest(self, gateway: AgentGateway) -> None:
        gateway._dedup_capacity = 2
        ids = [make_event(source="perceive.t01", action=Action.PERCEIVE_ANOMALY.value, payload={}, trace_id=TRACE).msg_id for _ in range(3)]
        for current in ids:
            gateway.is_duplicate(current)
        assert gateway.is_duplicate(ids[0]) is False, "超出容量的旧 ID 应被逐出"

    def test_inbox_unique_per_call(self, gateway: AgentGateway) -> None:
        assert len({gateway.new_inbox() for _ in range(500)}) == 500


class TestDispatch:
    async def test_no_capable_agent(self, gateway: AgentGateway, registry: AgentRegistry) -> None:
        with pytest.raises(NoCapableAgentError):
            await gateway.dispatch(AgentType.ASSESS, Action.ASSESS_HAZARD, {"region_code": "540121"}, trace_id=TRACE)
        assert gateway.counters["no_agent"] == 1
        last = gateway.transactions[-1]
        assert last.outcome == "no_agent"
        assert last.error_code == ErrorCode.NO_CAPABLE_AGENT.value

    async def test_saturated_agent_fails_fast(self, gateway: AgentGateway, registry: AgentRegistry) -> None:
        """容量饱和时快速失败（而非无界排队）：过载必须由降级/重调度显式处理。"""
        agent_id = register(registry, AgentType.ASSESS)
        registry.get(agent_id).inflight = registry.get(agent_id).reg.max_concurrency
        with pytest.raises(NoCapableAgentError):
            await gateway.dispatch(AgentType.ASSESS, Action.ASSESS_HAZARD, {}, trace_id=TRACE)
        assert gateway.counters["no_agent"] == 1

    async def test_happy_path(self, bus: InMemoryBus, gateway: AgentGateway, registry: AgentRegistry) -> None:
        agent_id = register(registry, AgentType.ASSESS)
        await gateway.start()
        await attach_responder(bus, AgentType.ASSESS, agent_id=agent_id, payload={"risk_level": int(RiskLevel.RED)})

        reply = await gateway.dispatch(AgentType.ASSESS, Action.ASSESS_HAZARD, {"region_code": "540121"}, trace_id=TRACE)
        assert reply.kind is MessageKind.RESPONSE
        assert reply.payload["risk_level"] == 1
        assert gateway.success_rate() == 1.0
        assert gateway.counters["outbound"] == 1
        assert gateway.counters["inbound"] == 1
        assert gateway.transactions[-1].outcome == "ok"
        assert gateway.transactions[-1].agent_id == agent_id
        assert registry.get(agent_id).served == 1
        assert registry.get(agent_id).inflight == 0
        await gateway.close()

    async def test_timeout_records_failed_txn(self, bus: InMemoryBus, gateway: AgentGateway, registry: AgentRegistry) -> None:
        agent_id = register(registry, AgentType.ASSESS)
        await gateway.start()
        await attach_responder(bus, AgentType.ASSESS, agent_id=agent_id, delay_ms=400)

        with pytest.raises(DeadlineExceededError):
            await gateway.dispatch(AgentType.ASSESS, Action.ASSESS_HAZARD, {}, trace_id=TRACE, deadline_ms=150)

        assert gateway.success_rate() == 0.0
        assert gateway.transactions[-1].outcome == "timeout"
        assert registry.get(agent_id).inflight == 0, "超时后必须释放并发额度"
        assert registry.get(agent_id).failed == 1
        await gateway.close()

    async def test_error_reply_propagates(self, bus: InMemoryBus, gateway: AgentGateway, registry: AgentRegistry) -> None:
        agent_id = register(registry, AgentType.ASSESS)

        async def failing(message: AgentMessage) -> None:
            await bus.publish(
                message.reply_to or "",
                make_error(message, source=agent_id, code="E_INTERNAL", message="炸了", retryable=True),
            )

        await gateway.start()
        await bus.subscribe(subjects.agent_in(AgentType.ASSESS), failing)

        with pytest.raises(AegisError) as info:
            await gateway.dispatch(AgentType.ASSESS, Action.ASSESS_HAZARD, {}, trace_id=TRACE)
        assert info.value.retryable is True
        assert gateway.transactions[-1].outcome == "error"
        assert gateway.transactions[-1].error_code == "E_INTERNAL"
        await gateway.close()

    async def test_response_violating_contract_is_rejected(self, bus: InMemoryBus, gateway: AgentGateway, registry: AgentRegistry) -> None:
        agent_id = register(registry, AgentType.ASSESS)

        async def illegal_reply(message: AgentMessage) -> None:
            payload = {
                **make_response(message, source=agent_id, payload={}).model_dump(exclude_none=True),
                "action": "assess.teleport",
            }
            await bus._publish_raw(message.reply_to or "", json.dumps(payload).encode())

        await gateway.start()
        await bus.subscribe(subjects.agent_in(AgentType.ASSESS), illegal_reply)
        with pytest.raises(UnregisteredActionError):
            await gateway.dispatch(AgentType.ASSESS, Action.ASSESS_HAZARD, {}, trace_id=TRACE)
        await gateway.close()

    def test_success_rate_window(self, gateway: AgentGateway) -> None:
        assert gateway.success_rate() is None
        for outcome in ("ok", "timeout", "ok", "ok"):
            gateway._record_txn(
                TxnRecord(
                    msg_id="msg_" + "0" * 16,
                    trace_id=TRACE,
                    action="assess.hazard",
                    agent_id="assess.t01",
                    outcome=outcome,
                    latency_ms=1.0,
                    at=now_iso(),
                )
            )
        assert gateway.success_rate() == 0.75
        assert gateway.success_rate(window=3) == pytest.approx(2 / 3)
        assert gateway.success_rate(window=1) == 1.0


class TestUplink:
    async def test_valid_event_counted_traced_and_forwarded(
        self, bus: InMemoryBus, gateway: AgentGateway, registry: AgentRegistry, tracer: Tracer
    ) -> None:
        await gateway.start()
        seen: list[AgentMessage] = []

        async def handler(message: AgentMessage) -> None:
            seen.append(message)

        gateway.set_result_handler(handler)
        message = make_event(source="perceive.t01", action=Action.PERCEIVE_ANOMALY.value, payload={"v": 1}, trace_id=TRACE)
        await bus.publish(subjects.agent_out("perceive"), message)
        await bus.idle()

        assert gateway.counters["inbound"] == 1
        assert tracer.ledger.stats("sync_agent_to_gateway_ms").count == 1
        assert [m.msg_id for m in seen] == [message.msg_id]
        await gateway.close()

    async def test_unregistered_action_rejected_by_whitelist(self, bus: InMemoryBus, gateway: AgentGateway) -> None:
        await gateway.start()
        message = make_event(source="perceive.t01", action="perceive.teleport", payload={}, trace_id=TRACE)
        await bus.publish(subjects.agent_out("perceive"), message)
        await bus.idle()
        assert gateway.counters["rejected"] == 1
        assert gateway.counters["inbound"] == 1  # 收到了，但被白名单拒绝
        await gateway.close()

    async def test_structurally_invalid_json_does_not_reach_business(self, bus: InMemoryBus, gateway: AgentGateway) -> None:
        await gateway.start()
        broken = make_event(source="perceive.t01", action=Action.PERCEIVE_ANOMALY.value, payload={}, trace_id=TRACE)
        data = broken.model_dump(exclude_none=True)
        data["kind"] = "request"  # request 但缺 reply_to/deadline：解码层即失败
        await bus._publish_raw(subjects.agent_out("perceive"), json.dumps(data).encode())
        await bus.idle()
        assert gateway.counters["inbound"] == 0, "非法 JSON 不应进入业务层"
        await gateway.close()

    async def test_expired_ttl_dropped(self, bus: InMemoryBus, gateway: AgentGateway) -> None:
        await gateway.start()
        stale = make_event(source="perceive.t01", action=Action.PERCEIVE_ANOMALY.value, payload={}, trace_id=TRACE, ttl_ms=1)
        stale = stale.model_copy(
            update={"ts": (utc_now() - timedelta(seconds=5)).isoformat(timespec="milliseconds").replace("+00:00", "Z")}
        )
        await bus.publish(subjects.agent_out("perceive"), stale)
        await bus.idle()
        assert gateway.counters["dropped_ttl"] == 1
        await gateway.close()

    async def test_duplicate_uplink_not_forwarded(self, bus: InMemoryBus, gateway: AgentGateway) -> None:
        await gateway.start()
        seen: list[AgentMessage] = []

        async def handler(message: AgentMessage) -> None:
            seen.append(message)

        gateway.set_result_handler(handler)
        message = make_event(source="perceive.t01", action=Action.PERCEIVE_ANOMALY.value, payload={}, trace_id=TRACE)
        await bus.publish(subjects.agent_out("perceive"), message)
        await bus.publish(subjects.agent_out("perceive"), message)
        await bus.idle()

        assert len(seen) == 1, "重复消息不得二次投递给业务层"
        assert gateway.counters["duplicate"] == 1
        await gateway.close()

    async def test_registration_message_registers_agent(self, bus: InMemoryBus, gateway: AgentGateway, registry: AgentRegistry) -> None:
        await gateway.start()
        registration = CapabilityRegistration(agent_id="assess.newborn", agent_type="assess", capabilities=["risk_assess"])
        await bus.publish(
            subjects.agent_hb("assess"),
            make_event(
                source="assess.newborn",
                action=Action.AGENT_REGISTER.value,
                payload={"registration": registration.model_dump()},
                trace_id=TRACE,
                target="platform.registry",
            ),
        )
        await bus.idle()
        assert registry.get("assess.newborn") is not None
        await gateway.close()

    async def test_bad_registration_is_rejected(self, bus: InMemoryBus, gateway: AgentGateway, registry: AgentRegistry) -> None:
        await gateway.start()
        await bus.publish(
            subjects.agent_hb("assess"),
            make_event(
                source="assess.bad01",
                action=Action.AGENT_REGISTER.value,
                payload={"registration": {"agent_id": "assess.bad01"}},
                trace_id=TRACE,
                target="platform.registry",
            ),
        )
        await bus.idle()
        assert registry.get("assess.bad01") is None
        await gateway.close()

    async def test_unknown_agent_heartbeat_is_harmless(self, bus: InMemoryBus, gateway: AgentGateway) -> None:
        await gateway.start()
        await bus.publish(
            subjects.agent_hb("assess"),
            make_event(source="assess.ghost1", action=Action.AGENT_HEARTBEAT.value, payload={}, trace_id=TRACE, target="platform.registry"),
        )
        await bus.idle()
        assert gateway.counters["rejected"] == 0
        await gateway.close()

    def test_result_handler_can_be_cleared(self, gateway: AgentGateway) -> None:
        gateway.set_result_handler(None)
        assert gateway._result_handler is None


class TestPublish:
    async def test_publish_telemetry_records_two_stages(self, bus: InMemoryBus, gateway: AgentGateway, tracer: Tracer) -> None:
        await gateway.start()

        async def sink(message: AgentMessage) -> None:
            return None

        await bus.subscribe("data.RG-01.rain_10min", sink)
        reading = TelemetryReading(
            station_id="RG-01",
            metric="rain_10min",
            value=12.0,
            unit="mm",
            region_code="540121",
            observed_at=now_iso(utc_now() - timedelta(seconds=20)),
        )
        await gateway.publish_telemetry(
            reading,
            make_event(
                source="platform.connector_rg_01",
                action=Action.TELEMETRY_READING.value,
                payload=reading.model_dump(),
                trace_id=TRACE,
                target="platform.telemetry_sink",
            ),
        )
        await bus.idle()
        assert tracer.ledger.stats("ingest_store_ms").count == 1
        assert tracer.ledger.stats("ingest_publish_ms").count == 1
        assert tracer.ledger.stats("ingest_store_ms").max >= 19_000
        await gateway.close()

    async def test_publish_alert_subject_carries_level(self, bus: InMemoryBus, gateway: AgentGateway) -> None:
        await gateway.start()
        seen: list[AgentMessage] = []

        async def sink(message: AgentMessage) -> None:
            seen.append(message)

        await bus.subscribe(subjects.alert(1, "540121"), sink)
        await gateway.publish_alert(
            RiskLevel.RED,
            "540121",
            make_event(
                source="platform.pipeline",
                action=Action.EXECUTE_ACK.value,
                payload={"warning_id": "wrn_x"},
                trace_id=TRACE,
                target="platform.broadcast_1",
            ),
        )
        await bus.idle()
        assert len(seen) == 1
        await gateway.close()

    def test_snapshot_shape(self, gateway: AgentGateway) -> None:
        snap = gateway.snapshot()
        assert {"counters", "transactions", "success_rate", "agents", "agents_online", "latency"} <= set(snap)
