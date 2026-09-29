"""内存总线语义与并发行为测试（NATS 实现的等价基线）。"""

from __future__ import annotations

import asyncio

import pytest

from aegis.bus import subjects
from aegis.bus.inmemory import InMemoryBus
from aegis.bus.transport import Subscription, decode, encode
from aegis.domain.enums import Action, MessageKind
from aegis.domain.messages import AgentMessage, make_event, make_request, make_response
from aegis.errors import BusNotReadyError, DeadlineExceededError

TRACE = "trc_" + "e" * 16


def event(payload: dict[str, object] | None = None, *, action: str = Action.PERCEIVE_ANOMALY.value) -> AgentMessage:
    return make_event(source="perceive.test01", target="platform.sink", action=action, payload=payload or {}, trace_id=TRACE)


def request_msg(**overrides: object) -> AgentMessage:
    params: dict[str, object] = {
        "source": "platform.gateway",
        "target": "assess.test01",
        "action": Action.ASSESS_HAZARD.value,
        "payload": {},
        "trace_id": TRACE,
        "reply_to": "reply.gw00000001.inbox",
        "deadline_ms": 400,
    }
    params.update(overrides)
    return make_request(**params)  # type: ignore[arg-type]


class TestConnection:
    async def test_publish_before_connect_raises(self) -> None:
        bus = InMemoryBus()
        with pytest.raises(BusNotReadyError):
            await bus.publish("data.x.y", event())

    async def test_context_manager_connects_and_closes(self) -> None:
        async with InMemoryBus() as bus:
            assert bus.connected
        assert not bus.connected


class TestPubSub:
    async def test_broadcast_to_all_non_queue_subscribers(self, bus: InMemoryBus) -> None:
        seen: list[AgentMessage] = []

        async def collect(message: AgentMessage) -> None:
            seen.append(message)

        await bus.subscribe(subjects.agent_out("perceive"), collect)
        await bus.subscribe(subjects.agent_out("perceive"), collect)
        await bus.publish(subjects.agent_out("perceive"), event())
        await bus.idle()
        assert len(seen) == 2

    async def test_wildcard_subscription(self, bus: InMemoryBus) -> None:
        seen: list[str] = []

        async def collect(message: AgentMessage) -> None:
            seen.append(message.action)

        await bus.subscribe("agent.*.out", collect)
        await bus.publish(subjects.agent_out("assess"), event(action=Action.ASSESS_RISK_LEVEL.value))
        await bus.publish(subjects.alert(1, "540121"), event())
        await bus.idle()
        assert seen == [Action.ASSESS_RISK_LEVEL.value]

    async def test_queue_group_delivers_to_single_instance(self, bus: InMemoryBus) -> None:
        counts = {"a": 0, "b": 0}

        def handler(key: str):
            async def _inner(message: AgentMessage) -> None:
                counts[key] += 1

            return _inner

        await bus.subscribe(subjects.agent_in("assess"), handler("a"), queue="workers")
        await bus.subscribe(subjects.agent_in("assess"), handler("b"), queue="workers")
        for _ in range(6):
            await bus.publish(subjects.agent_in("assess"), event())
        await bus.idle()
        assert counts["a"] + counts["b"] == 6
        assert counts["a"] == 3 and counts["b"] == 3, "队列组应轮询均衡分配"

    async def test_no_subscriber_counts_drop(self, bus: InMemoryBus) -> None:
        await bus.publish("data.nobody.metric", event())
        assert bus.dropped_no_subscriber == 1

    async def test_handler_exception_is_isolated(self, bus: InMemoryBus) -> None:
        reached: list[str] = []

        async def boom(message: AgentMessage) -> None:
            raise RuntimeError("handler 内部异常")

        async def ok(message: AgentMessage) -> None:
            reached.append(message.action)

        await bus.subscribe(subjects.agent_out("perceive"), boom)
        await bus.subscribe(subjects.agent_out("perceive"), ok)
        await bus.publish(subjects.agent_out("perceive"), event())
        await bus.idle()
        assert reached == [Action.PERCEIVE_ANOMALY.value]

    async def test_cancel_stops_delivery(self, bus: InMemoryBus) -> None:
        seen = 0

        async def collect(message: AgentMessage) -> None:
            nonlocal seen
            seen += 1

        sub = await bus.subscribe(subjects.agent_out("perceive"), collect)
        await bus.publish(subjects.agent_out("perceive"), event())
        await bus.idle()
        await sub.cancel()
        await bus.publish(subjects.agent_out("perceive"), event())
        await bus.idle()
        assert seen == 1


class TestRequestReply:
    async def test_roundtrip(self, bus: InMemoryBus) -> None:
        subject = subjects.agent_in("assess")

        async def responder(message: AgentMessage) -> None:
            await asyncio.sleep(0.01)
            await bus.publish(message.reply_to, make_response(message, source="assess.test01", payload={"risk_level": 2}))

        await bus.subscribe(subject, responder)
        reply = await bus.request(subject, request_msg())
        assert reply.kind is MessageKind.RESPONSE
        assert reply.payload["risk_level"] == 2

    async def test_timeout_raises_deadline(self, bus: InMemoryBus) -> None:
        subject = subjects.agent_in("assess")
        with pytest.raises(DeadlineExceededError) as info:
            await bus.request(subject, request_msg(deadline_ms=150))
        assert info.value.detail["deadline_ms"] == 150

    async def test_late_reply_is_dropped_safely(self, bus: InMemoryBus) -> None:
        subject = subjects.agent_in("assess")

        async def slow(message: AgentMessage) -> None:
            await asyncio.sleep(0.35)
            await bus.publish(message.reply_to, make_response(message, source="assess.test01", payload={}))

        await bus.subscribe(subject, slow)
        with pytest.raises(DeadlineExceededError):
            await bus.request(subject, request_msg(deadline_ms=100))
        await bus.idle(timeout=1.0)  # 迟到响应不得抛异常或污染总线

    async def test_mismatched_causation_ignored(self, bus: InMemoryBus) -> None:
        subject = subjects.agent_in("assess")

        async def wrong(message: AgentMessage) -> None:
            stranger = make_request(
                source="platform.gateway",
                target="assess.test01",
                action=Action.ASSESS_HAZARD.value,
                payload={},
                trace_id=TRACE,
                reply_to=message.reply_to or "reply.x.inbox",
                deadline_ms=200,
            )
            await bus.publish(
                message.reply_to or "",
                make_response(stranger, source="assess.test01", payload={"risk_level": 3}),
            )

        await bus.subscribe(subject, wrong)
        with pytest.raises(DeadlineExceededError):
            await bus.request(subject, request_msg(deadline_ms=200))

    async def test_request_without_reply_to_rejected(self, bus: InMemoryBus) -> None:
        message = event()
        with pytest.raises(ValueError, match="reply_to"):
            await bus.request(subjects.agent_in("assess"), message)

    async def test_concurrent_requests_are_isolated(self, bus: InMemoryBus) -> None:
        subject = subjects.agent_in("assess")

        async def responder(message: AgentMessage) -> None:
            await asyncio.sleep(0.02 if message.priority == 1 else 0.005)
            await bus.publish(message.reply_to, make_response(message, source="assess.test01", payload={"seq": message.payload["seq"]}))

        await bus.subscribe(subject, responder)
        messages = [request_msg(reply_to=subjects.reply(f"gw{index:06d}"), payload={"seq": index}) for index in range(20)]
        replies = await asyncio.gather(*(bus.request(subject, m) for m in messages))
        assert sorted(int(r.payload["seq"]) for r in replies) == list(range(20)), "并发请求不得串扰"


class TestCodec:
    def test_encode_decode_roundtrip(self) -> None:
        message = request_msg(payload={"a": 1})
        assert decode(encode(message)) == message

    def test_encode_omits_none(self) -> None:
        raw = encode(event())
        assert b"causation_id" not in raw

    async def test_idle_times_out_when_stuck(self, bus: InMemoryBus) -> None:
        async def forever(message: AgentMessage) -> None:
            await asyncio.sleep(5)

        sub = await bus.subscribe(subjects.agent_out("perceive"), forever)
        await bus.publish(subjects.agent_out("perceive"), event())
        with pytest.raises(TimeoutError):
            await bus.idle(timeout=0.1)
        await sub.cancel()

    async def test_subscription_metadata(self, bus: InMemoryBus) -> None:
        async def noop(message: AgentMessage) -> None:
            return None

        sub: Subscription = await bus.subscribe(subjects.agent_out("perceive"), noop, queue="q", durable="d")
        assert (sub.queue, sub.durable) == ("q", "d")
        await bus.publish(subjects.agent_out("perceive"), event())
        await bus.idle()
        assert sub.delivered == 1
        assert sub.errors == 0
