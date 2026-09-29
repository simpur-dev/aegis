"""真实 NATS JetStream 集成测试（CI 中随 nats service 启动）。

本地默认跳过：设置 AEGIS_TEST_NATS_URL=nats://127.0.0.1:4222 后执行
    uv run pytest -q -m nats tests/integration

覆盖单进程内存总线测不到的三件事：
1. 跨连接（模拟两个节点）的请求—响应；
2. JetStream 持久流下的 durable 消费者订阅与重启重放；
3. 契约校验与协同事务台账在真实投递路径上仍成立。
"""

from __future__ import annotations

import asyncio
import os

import pytest

from aegis.bus import subjects
from aegis.bus.gateway import AgentGateway, ContractRegistry
from aegis.bus.nats_bus import NatsBus
from aegis.bus.registry import AgentRegistry
from aegis.config import Settings
from aegis.domain.enums import Action, AgentType, RiskLevel
from aegis.domain.messages import AgentMessage, CapabilityRegistration, make_response
from aegis.observability.tracer import Tracer

NATS_URL = os.getenv("AEGIS_TEST_NATS_URL", "")
pytestmark = pytest.mark.nats

TRACE = "trc_" + "9" * 16


def _settings() -> Settings:
    return Settings(env="test", bus_backend="nats", nats_url=NATS_URL, default_request_deadline_ms=5_000)


@pytest.fixture
def stream_prefix() -> str:
    """每次运行使用独立流前缀，避免 CI 并发运行互相污染。"""
    import uuid

    return f"AEGIST_{uuid.uuid4().hex[:8]}"


async def _make_bus(stream_prefix: str) -> NatsBus:
    bus = NatsBus(NATS_URL, stream_prefix=stream_prefix)
    await bus.connect()
    return bus


@pytest.mark.skipif(not NATS_URL, reason="未设置 AEGIS_TEST_NATS_URL，跳过真实 NATS 测试")
class TestNatsBus:
    async def test_connect_close_and_stream_creation(self, stream_prefix: str) -> None:
        bus = await _make_bus(stream_prefix)
        try:
            assert bus.connected
        finally:
            await bus.close()
        assert not bus.connected

    async def test_cross_connection_request_reply(self, stream_prefix: str) -> None:
        platform_bus = await _make_bus(stream_prefix)
        agent_bus = await _make_bus(stream_prefix)
        registry = AgentRegistry(_settings())
        contracts = ContractRegistry()
        tracer = Tracer()
        gateway = AgentGateway(platform_bus, registry, contracts, tracer, default_deadline_ms=5_000)
        agent_id = "assess.node01"
        registry.register(CapabilityRegistration(agent_id=agent_id, agent_type=AgentType.ASSESS.value, capabilities=["risk_assess"]))

        async def agent_handler(message: AgentMessage) -> None:
            reply = make_response(
                message,
                source=agent_id,
                payload={
                    "hazard_type": "debris_flow",
                    "region_code": "540121",
                    "risk_level": int(RiskLevel.ORANGE),
                    "confidence": 0.77,
                    "rationale": "NATS 跨节点研判",
                },
            )
            await agent_bus.publish(message.reply_to or "", reply)

        try:
            await gateway.start()
            await agent_bus.subscribe(subjects.agent_in(AgentType.ASSESS), agent_handler, queue="cg-assess")
            reply = await gateway.dispatch(AgentType.ASSESS, Action.ASSESS_HAZARD, {"region_code": "540121"}, trace_id=TRACE)
            assert reply.payload["confidence"] == pytest.approx(0.77)
            assert gateway.success_rate() == 1.0
            assert tracer.ledger.stats("sync_agent_to_gateway_ms").count >= 1
        finally:
            await gateway.close()
            await platform_bus.close()
            await agent_bus.close()

    async def test_durable_consumer_receives_uplink(self, stream_prefix: str) -> None:
        platform_bus = await _make_bus(stream_prefix)
        agent_bus = await _make_bus(stream_prefix)
        registry = AgentRegistry(_settings())
        contracts = ContractRegistry()
        gateway = AgentGateway(platform_bus, registry, contracts, Tracer())
        received: list[AgentMessage] = []

        async def capture(message: AgentMessage) -> None:
            received.append(message)

        try:
            await gateway.start()
            await asyncio.sleep(0.2)  # 等 JetStream 消费者注册完成
            from aegis.domain.messages import make_event

            await agent_bus.publish(
                subjects.agent_out(AgentType.PERCEIVE),
                make_event(
                    source="perceive.node01",
                    action=Action.PERCEIVE_TRIGGER_HIT.value,
                    payload={"rule_id": "R-DEBRIS-RAIN-1", "hazard_type": "debris_flow", "region_code": "540121", "score": 0.9},
                    trace_id=TRACE,
                    target="platform.pipeline",
                ),
            )
            await asyncio.sleep(0.6)
            assert len(received) == 1
            assert gateway.counters["inbound"] == 1
        finally:
            await gateway.close()
            await platform_bus.close()
            await agent_bus.close()

    async def test_publish_to_subject_without_subscriber_is_retained(self, stream_prefix: str) -> None:
        """弱网友好：JetStream 持久流下，先发后订仍能取到历史（断点续传的底层保证）。"""
        bus = await _make_bus(stream_prefix)
        from aegis.domain.messages import make_event

        try:
            await bus.publish(
                subjects.data("rg01", "rain_10min"),
                make_event(
                    source="platform.connector_rg01",
                    action=Action.TELEMETRY_READING.value,
                    payload={"v": 1},
                    trace_id=TRACE,
                    target="platform.telemetry_sink",
                ),
            )
            seen: list[AgentMessage] = []

            async def late(message: AgentMessage) -> None:
                seen.append(message)

            sub = await bus._subscribe_raw(
                "data.>",
                lambda raw: late(AgentMessage.decode(raw)),
                durable=f"d_late_{stream_prefix}",
            )
            await asyncio.sleep(0.8)
            assert len(seen) == 1, "持久流应回放历史消息"
            await sub.cancel()
        finally:
            await bus.close()
