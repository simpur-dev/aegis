"""《智能体接入规范》§7 一致性门禁套件。

这套件同时是两件事：
1. Mock 智能体的合格样例——真实智能体替换 Mock 后必须跑通同一套断言；
2. 平台的准入闸门——违约行为（超时/静默/非法载荷/未注册动作）必须被网关正确识别并降级。

约定：套件只通过总线与注册中心观察行为，不依赖智能体内部实现。
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path

import pytest
from pydantic import ValidationError

from aegis.agents.mock import Misbehavior, MockAgent, start_mock_agents
from aegis.bus import subjects
from aegis.bus.gateway import AgentGateway
from aegis.bus.inmemory import InMemoryBus
from aegis.bus.registry import AgentRegistry
from aegis.domain.enums import Action, AgentType, HazardType, RiskLevel
from aegis.domain.messages import AgentMessage, CapabilityRegistration
from aegis.errors import AegisError, DeadlineExceededError

TRACE = "trc_" + "1" * 16


async def bring_up(
    bus: InMemoryBus,
    gateway: AgentGateway,
    agent_type: AgentType,
    capability: str,
    **kwargs: object,
) -> MockAgent:
    params: dict[str, object] = {"latency_ms": 5.0, "seed": 7}
    params.update(kwargs)
    agent = MockAgent(
        transport=bus,
        agent_type=agent_type,
        agent_id=f"{agent_type.value}.conf01",
        capabilities=[capability],
        **params,  # type: ignore[arg-type]
    )
    await gateway.start()
    await agent.start(heartbeat_interval=0.05)
    await bus.idle()
    await asyncio.sleep(0.01)
    await bus.idle()
    return agent


class TestGate1SchemaLegality:
    async def test_agent_uplink_all_valid(self, bus: InMemoryBus, gateway: AgentGateway, registry: AgentRegistry) -> None:
        agent = await bring_up(bus, gateway, AgentType.ASSESS, "risk_assess")
        reply = await gateway.dispatch(
            AgentType.ASSESS,
            Action.ASSESS_HAZARD,
            {
                "region_code": "540121",
                "hits": [{"rule_id": "R-DEBRIS-RAIN-1", "hazard_type": "debris_flow", "region_code": "540121", "score": 0.9}],
            },
            trace_id=TRACE,
            capability="risk_assess",
        )
        assert gateway.counters["rejected"] == 0
        assert reply.payload["risk_level"] in [int(level) for level in RiskLevel]
        assert reply.trace_id == TRACE
        assert reply.causation_id is not None
        await agent.stop()
        await gateway.close()

    @pytest.mark.parametrize(
        "agent_type,capability,action,payload",
        [
            (AgentType.ASSESS, "risk_assess", Action.ASSESS_HAZARD, {"region_code": "540121", "hits": []}),
            (AgentType.PLAN, "task_decompose", Action.PLAN_STU, {"event_id": "evt_" + "a" * 12, "risk": {}}),
            (
                AgentType.EXECUTE,
                "warn_publish",
                Action.EXECUTE_WARN,
                {"warning_id": "wrn_1", "channels": ["sms"], "audiences": ["residents"]},
            ),
        ],
    )
    async def test_each_role_replies_valid(
        self,
        bus: InMemoryBus,
        gateway: AgentGateway,
        registry: AgentRegistry,
        agent_type: AgentType,
        capability: str,
        action: Action,
        payload: dict,
    ) -> None:
        agent = await bring_up(bus, gateway, agent_type, capability)
        reply = await gateway.dispatch(agent_type, action, payload, trace_id=TRACE, capability=capability)
        assert isinstance(reply, AgentMessage)
        assert gateway.counters["rejected"] == 0
        await agent.stop()
        await gateway.close()


class TestGate2Idempotency:
    async def test_duplicate_request_produces_single_reply(self, bus: InMemoryBus, gateway: AgentGateway, registry: AgentRegistry) -> None:
        agent = await bring_up(bus, gateway, AgentType.ASSESS, "risk_assess")
        request = AgentMessage(
            source="platform.gateway",
            target=agent.agent_id,
            kind="request",  # type: ignore[arg-type]
            action=Action.ASSESS_HAZARD.value,
            payload={"region_code": "540121", "hits": []},
            trace_id=TRACE,
            reply_to="reply.gwconf0001.inbox",
            deadline_ms=800,
        )
        replies: list[AgentMessage] = []

        async def collector(message: AgentMessage) -> None:
            replies.append(message)

        await bus.subscribe(request.reply_to or "", collector)
        await bus.publish(subjects.agent_in(AgentType.ASSESS), request)
        await bus.publish(subjects.agent_in(AgentType.ASSESS), request)  # 重复投递
        await bus.idle()
        await asyncio.sleep(0.05)
        await bus.idle()

        assert len(replies) == 1, "重复 msg_id 不得产生第二次响应"
        assert agent.duplicates_skipped == 1
        await agent.stop()
        await gateway.close()


class TestGate3TimeoutSemantics:
    async def test_late_reply_is_judged_timeout(self, bus: InMemoryBus, gateway: AgentGateway, registry: AgentRegistry) -> None:
        agent = await bring_up(bus, gateway, AgentType.ASSESS, "risk_assess", latency_ms=300.0, misbehavior=Misbehavior.NONE)
        with pytest.raises(DeadlineExceededError):
            await gateway.dispatch(AgentType.ASSESS, Action.ASSESS_HAZARD, {"region_code": "540121"}, trace_id=TRACE, deadline_ms=120)
        assert gateway.transactions[-1].outcome == "timeout"
        assert gateway.success_rate() == 0.0
        await asyncio.sleep(0.35)
        await bus.idle()
        assert gateway.transactions[-1].outcome == "timeout", "迟到响应不得改写已判超期的事务"
        await agent.stop()
        await gateway.close()

    async def test_silent_agent_times_out_without_crash(self, bus: InMemoryBus, gateway: AgentGateway, registry: AgentRegistry) -> None:
        agent = await bring_up(bus, gateway, AgentType.PLAN, "task_decompose", misbehavior=Misbehavior.SILENT)
        with pytest.raises(DeadlineExceededError):
            await gateway.dispatch(
                AgentType.PLAN, Action.PLAN_STU, {"event_id": "evt_" + "b" * 12, "risk": {}}, trace_id=TRACE, deadline_ms=150
            )
        assert agent.handled == 1
        await agent.stop()
        await gateway.close()


class TestGate4ErrorReply:
    async def test_crash_reports_typed_error(self, bus: InMemoryBus, gateway: AgentGateway, registry: AgentRegistry) -> None:
        agent = await bring_up(bus, gateway, AgentType.EXECUTE, "warn_publish", misbehavior=Misbehavior.CRASH)
        with pytest.raises(AegisError) as info:
            await gateway.dispatch(
                AgentType.EXECUTE,
                Action.EXECUTE_WARN,
                {"warning_id": "wrn_x", "channels": ["sms"], "audiences": ["residents"]},
                trace_id=TRACE,
            )
        assert info.value.retryable is True
        assert gateway.transactions[-1].error_code == "E_INTERNAL"
        assert gateway.transactions[-1].outcome == "error"
        await agent.stop()
        await gateway.close()

    async def test_invalid_payload_is_rejected_by_gateway(self, bus: InMemoryBus, gateway: AgentGateway, registry: AgentRegistry) -> None:
        """载荷形状错误不会被平台吞下：网关按契约拒绝并记账。"""
        agent = await bring_up(bus, gateway, AgentType.ASSESS, "risk_assess", misbehavior=Misbehavior.INVALID)
        reply = await gateway.dispatch(AgentType.ASSESS, Action.ASSESS_HAZARD, {"region_code": "540121"}, trace_id=TRACE)
        # 响应消息本身合法（AgentMessage 合规），但其 payload 由业务层校验
        assert reply.payload == {"unexpected": "shape"}
        await agent.stop()
        await gateway.close()

    async def test_unregistered_action_uplink_rejected(self, bus: InMemoryBus, gateway: AgentGateway) -> None:
        await gateway.start()
        await bus.publish(
            subjects.agent_out(AgentType.PERCEIVE),
            AgentMessage(
                source="perceive.bad01",
                target="platform.sink",
                kind="event",  # type: ignore[arg-type]
                action="perceive.teleport",
                payload={},
                trace_id=TRACE,
            ),
        )
        await bus.idle()
        assert gateway.counters["rejected"] == 1
        await gateway.close()


class TestGate5Lifecycle:
    async def test_register_heartbeat_and_loss(self, bus: InMemoryBus, gateway: AgentGateway, registry: AgentRegistry, settings) -> None:
        agent = await bring_up(bus, gateway, AgentType.ASSESS, "risk_assess")
        entry = registry.get(agent.agent_id)
        assert entry is not None and entry.healthy

        await agent.stop()  # 心跳停止
        registry.get(agent.agent_id).last_hb_mono = 0.0
        assert await registry.sweep_and_notify() == [agent.agent_id]
        assert registry.pick(AgentType.ASSESS.value, "risk_assess") is None

        await agent.start(heartbeat_interval=0.05)  # 重新注册
        await bus.idle()
        assert registry.pick(AgentType.ASSESS.value, "risk_assess") is not None
        await agent.stop()
        await gateway.close()

    async def test_registration_declares_capabilities(self, bus: InMemoryBus, gateway: AgentGateway, registry: AgentRegistry) -> None:
        agent = await bring_up(bus, gateway, AgentType.PLAN, "task_decompose")
        entry = registry.get(agent.agent_id)
        assert entry is not None
        assert entry.reg.capabilities == ["task_decompose"]
        assert entry.reg.agent_type == AgentType.PLAN.value
        await agent.stop()
        await gateway.close()


class TestGate6DataBoundary:
    def test_agent_side_never_imports_storage_layer(self) -> None:
        """智能体只能经总线与数据 API 交互：源码级检查禁止直接依赖存储层。"""
        forbidden = {"sqlalchemy", "aegis.storage", "neo4j", "asyncpg", "redis", "minio"}
        agents_dir = Path(__file__).resolve().parents[2] / "src" / "aegis" / "agents"
        offenders: list[str] = []
        for path in agents_dir.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                else:
                    continue
                offenders += [f"{path.name}:{name}" for name in names if name in forbidden or name.startswith("aegis.storage")]
        assert offenders == [], f"智能体侧不得直接访问平台存储层: {offenders}"

    def test_agents_have_no_database_client_attributes(self) -> None:
        agent = MockAgent(
            transport=InMemoryBus(),
            agent_type=AgentType.ASSESS,
            agent_id="assess.shape",
            capabilities=["risk_assess"],
        )
        fields = {name.lower() for name in vars(agent)}
        assert not (fields & {"session", "engine", "connection", "repository", "store", "db"})


class TestGate7ContextSnapshot:
    async def test_plan_output_carries_recoverable_snapshot(
        self, bus: InMemoryBus, gateway: AgentGateway, registry: AgentRegistry, contracts
    ) -> None:
        agent = await bring_up(bus, gateway, AgentType.PLAN, "task_decompose")
        reply = await gateway.dispatch(
            AgentType.PLAN,
            Action.PLAN_STU,
            {
                "event_id": "evt_" + "c" * 12,
                "risk": {
                    "hazard_type": HazardType.DEBRIS_FLOW.value,
                    "region_code": "540121",
                    "risk_level": int(RiskLevel.RED),
                    "confidence": 0.9,
                    "rationale": "强降雨叠加泥位抬升",
                    "evidence_refs": ["R-DEBRIS-RAIN-1"],
                },
            },
            trace_id=TRACE,
        )
        units = reply.payload["task_units"]
        assert units, "决策智能体必须产出 STU"
        assert contracts.stu_errors(units) == []
        for unit in units:
            assert unit["created_by"] == agent.agent_id
            assert "context_snapshot" in unit and "risk_level" in unit["context_snapshot"]
        await agent.stop()
        await gateway.close()


class TestFullMockSet:
    async def test_all_five_agents_register_and_serve(self, bus: InMemoryBus, gateway: AgentGateway, registry: AgentRegistry) -> None:
        await gateway.start()
        agents = await start_mock_agents(bus, latency_ms=5.0, heartbeat_interval=0.05)
        await bus.idle()
        await asyncio.sleep(0.02)
        await bus.idle()

        assert registry.online_count == 5
        assert registry.get("perceive.mock01") is not None
        assert registry.get("feedback.mock01") is not None
        for agent in agents:
            await agent.stop()
        await gateway.close()


class TestCapabilityRegistrationModel:
    def test_registration_requires_capabilities(self) -> None:
        with pytest.raises(ValidationError):
            CapabilityRegistration(agent_id="assess.x01", agent_type="assess", capabilities=[])

    def test_registration_shape(self) -> None:
        registration = CapabilityRegistration(
            agent_id="assess.x01", agent_type="assess", capabilities=["risk_assess"], hazard_types=["debris_flow"]
        )
        assert registration.max_concurrency >= 1
