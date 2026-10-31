"""真实 NATS JetStream 集成测试（CI 中随 nats service 启动）。

本地默认跳过：设置 AEGIS_TEST_NATS_URL=nats://127.0.0.1:4222 后执行
    uv run pytest -q -m nats tests/integration

覆盖单进程内存总线测不到的三件事：
1. 跨连接（模拟两个节点）的请求—响应；
2. JetStream 持久流下的 durable 消费者订阅与重启重放；
3. 契约校验与协同事务台账在真实投递路径上仍成立。

一条环境前提（2026-10-02 实测，四条用例曾整批红在同一处）：这套用例与真实应用**共用同一套
主题契约**（`data.>` 等），而 NATS 2.10 不允许两个流占同一主题（err_code 10065）。
同一台 server 上跑过 `--profile deployed` 的量测就会留下 `AEGIS_{DATA,AGENT,WF,OPS,REPLY}` 五个流，
把用例的建流顶失败。所以要么用一台没被应用占过的 JetStream，要么先删那五个流——
"换前缀"救不了这件事，因为主题本身是产品契约。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import uuid
from collections.abc import AsyncIterator

import nats
import pytest
from nats.js import api as js_api

from aegis.bus import subjects
from aegis.bus.gateway import AgentGateway
from aegis.bus.nats_bus import _STREAM_MAX_AGE_SECONDS, _STREAMS, NatsBus  # 删流/保留期要照同一份清单来，测试里不抄第二份
from aegis.bus.registry import AgentRegistry
from aegis.config import Settings
from aegis.contracts import ContractRegistry
from aegis.domain.enums import Action, AgentType, RiskLevel
from aegis.domain.messages import AgentMessage, CapabilityRegistration, make_response
from aegis.errors import BusNotReadyError
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
    try:
        await bus.connect()
    except BusNotReadyError as exc:
        # 主题重叠（err_code 10065）不是平台缺陷：`stream_prefix` 只隔离"用例 vs 用例"，
        # 隔离不了"用例 vs 一个正在跑着的部署"——双方用的是同一套契约主题，而契约主题不能改
        # （外部智能体按它接入）。所以这套 live 用例要的是一个干净的 JetStream。
        # 判 skip 而不是 fail：把"这台机器上此刻有平台实例在占主题"报成红，
        # 下一次部署形态取证就会被误读成总线坏了。
        if "10065" not in str(exc):
            raise
        pytest.skip(f"这套 JetStream 已被运行中的平台实例占用（{exc}）；本组用例需要干净的 JetStream，勿与部署共用")
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
            await agent_bus.subscribe(subjects.agent_in(AgentType.ASSESS, agent_id), agent_handler, queue="cg-assess", durable="cg-assess")
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

    async def test_streams_carry_bounded_retention(self, stream_prefix: str) -> None:
        """保留期不是服务端默认的"永久"：每个流都按平台口径设 max_age。"""
        bus = await _make_bus(stream_prefix)
        client = await nats.connect(NATS_URL)
        try:
            js = client.jetstream()
            for suffix, _patterns in _STREAMS:
                info = await js.stream_info(f"{stream_prefix}_{suffix}")
                assert info.config.max_age == pytest.approx(_STREAM_MAX_AGE_SECONDS[suffix])
        finally:
            await client.close()
            await bus.close()

    async def test_unacked_handler_is_redelivered_and_bounded_by_max_deliver(self, stream_prefix: str) -> None:
        """handler 抛错＝不回执：1s 后重投；投满 max_deliver 次即停，不会无限重投。

        这三条正是"智能体收到但未回执时崩溃"的重投语义。旧实现把 ack_wait/max_deliver
        留给服务端默认值（30s / 无限），平台从未用例钉住过它。
        """
        from aegis.domain.messages import make_event

        bus = NatsBus(NATS_URL, stream_prefix=stream_prefix, ack_wait_seconds=1.0, max_deliver=2)
        await bus.connect()
        attempts: list[bytes] = []

        async def poison(raw: bytes) -> None:
            attempts.append(raw)
            raise RuntimeError("handler failed")

        try:
            subject = subjects.data("rg01", "rain_10min")
            await bus._subscribe_raw(subject, poison, queue=f"cg_poison_{stream_prefix}", durable=f"d_poison_{stream_prefix}")
            await asyncio.sleep(0.3)  # 等消费者绑定完成
            await bus.publish(
                subject,
                make_event(
                    source="platform.connector_rg01",
                    action=Action.TELEMETRY_READING.value,
                    payload={"v": 1},
                    trace_id=TRACE,
                    target="platform.telemetry_sink",
                ),
            )
            await asyncio.sleep(3.0)
            assert len(attempts) == 2, f"应在 max_deliver=2 后停止重投，实际投递 {len(attempts)} 次"

            client = await nats.connect(NATS_URL)
            try:
                info = await client.jetstream().consumer_info(f"{stream_prefix}_DATA", f"d_poison_{stream_prefix}")
            finally:
                await client.close()
            assert info.config.ack_wait == pytest.approx(1.0)
            assert info.config.max_deliver == 2
        finally:
            await bus.close()

    async def test_existing_consumer_is_upgraded_to_the_platform_window(self, stream_prefix: str) -> None:
        """既存 durable 消费者（旧默认 30s/无限）在绑定时被就地升级，且投递 subject 不变。

        nats-py 在消费者已存在时会丢弃调用方传的 config；不对账就等于旧参数永远生效。
        投递 subject 不能换：其他实例可能正绑在它上面。
        """
        bus = NatsBus(NATS_URL, stream_prefix=stream_prefix, ack_wait_seconds=1.5, max_deliver=2)
        await bus.connect()
        durable = f"d_upgrade_{stream_prefix}"
        subject = subjects.agent_out(AgentType.PERCEIVE)
        legacy_subject = f"_INBOX.legacy_{stream_prefix}"

        async def sink(_raw: bytes) -> None:
            return None

        client = await nats.connect(NATS_URL)
        js = client.jetstream()
        try:
            await js.add_consumer(
                f"{stream_prefix}_AGENT",
                config=js_api.ConsumerConfig(
                    durable_name=durable,
                    deliver_group=durable,
                    deliver_subject=legacy_subject,
                    filter_subject=subject,
                    ack_wait=30.0,
                    max_deliver=-1,
                ),
            )
            sub = await bus._subscribe_raw(subject, sink, queue=f"cg_upgrade_{stream_prefix}", durable=durable)
            await sub.cancel()

            info = await js.consumer_info(f"{stream_prefix}_AGENT", durable)
            assert info.config.ack_wait == pytest.approx(1.5)
            assert info.config.max_deliver == 2
            assert info.config.deliver_subject == legacy_subject, "升级不得改投递 subject：其他实例可能正绑在它上面"
        finally:
            await client.close()
            await bus.close()
