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
import contextlib
import os
import uuid
from collections.abc import AsyncIterator

import nats
import pytest

from aegis.bus import subjects
from aegis.bus.gateway import AgentGateway, ContractRegistry
from aegis.bus.nats_bus import _STREAMS, NatsBus  # 删流要照同一份清单来，测试里不抄第二份
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
async def stream_prefix() -> AsyncIterator[str]:
    """每条用例一套自己的持久流，用完删掉。

    NATS 2.10 会拒绝"主题与既有流重叠"的新流（err_code 10065），所以"每条换前缀"
    必须搭配"每条删干净"——否则同一台服务器上从第二条用例起就再也建不出流。
    今晚接上真实 JetStream 才把这条暴露出来：旧代码把建流失败咽在 debug 里，
    于是这些用例看着"连上了"，实际每条发布都在失败。
    """
    prefix = f"AEGIST_{uuid.uuid4().hex[:8]}"
    yield prefix
    await _drop_streams(prefix)


async def _drop_streams(prefix: str) -> None:
    client = await nats.connect(NATS_URL)
    jetstream = client.jetstream()
    try:
        for suffix, _patterns in _STREAMS:
            # 建流就没成功的用例本来没有这条流：删不到不算问题。
            with contextlib.suppress(Exception):
                await jetstream.delete_stream(f"{prefix}_{suffix}")
    finally:
        await client.close()


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
            # durable 而不是临时队列订阅：订阅注册是一次往返，dispatch 紧跟着发出去时
            # 临时订阅可能还没生效，消息只留在流里没人收——这条用例就会偶发超时。
            # 站端在生产里用的本来就是 durable 消费者，这里按生产形态订。
            await agent_bus.subscribe(subjects.agent_in(AgentType.ASSESS), agent_handler, queue="cg-assess", durable="cg-assess")
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
            # 这条订阅原本只是定义了 capture 却没订上去，于是 received 永远是空表：
            # 断言看着严格，其实没有任何东西会往里写。真实 JetStream 上跑第一次就露馅。
            await agent_bus.subscribe(subjects.agent_out(AgentType.PERCEIVE), capture, durable="cg_capture")
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
