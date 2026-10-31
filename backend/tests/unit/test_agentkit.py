"""agentkit 契约壳测试（§3.0）：注册/心跳/幂等/deadline/error.raise/D10 溯源。

同一套 §7 断言口径：真智能体替换 Mock 后须跑通；壳把客户端职责收敛后，这些行为在此钉死。
1-4 走真内存总线 + 网关（与 conformance 同法），5 直接驱动 `_on_request` 用假传输，
把内软超时与网关超时分开判，避免两者同时到期读成"网络抖"。
"""

from __future__ import annotations

import asyncio

import pytest

from aegis.agents.agentkit import AgentKit, Reply
from aegis.bus import subjects
from aegis.bus.gateway import AgentGateway
from aegis.bus.inmemory import InMemoryBus
from aegis.bus.naming import consumer_name
from aegis.bus.registry import AgentRegistry
from aegis.domain.enums import Action, AgentType
from aegis.domain.messages import AgentMessage, ModelInfo, make_request

TRACE = "trc_" + "9" * 16


class EchoAssess(AgentKit):
    async def handle(self, request: AgentMessage) -> dict:
        return {
            "hazard_type": "debris_flow",
            "risk_level": 2,
            "confidence": 0.6,
            "rationale": f"agentkit echo over {len(request.payload.get('hits', []))} hits",
            "assessed_by": self.agent_id,
        }


class ProvenanceAssess(AgentKit):
    async def handle(self, request: AgentMessage) -> Reply:
        return Reply(
            payload={
                "hazard_type": "debris_flow",
                "risk_level": 2,
                "confidence": 0.6,
                "rationale": "双盲分歧",
                "assessed_by": self.agent_id,
            },
            model_info=ModelInfo(model="qwen-plus", prompt_version="1.2.0"),
            disagreement=True,
        )


class BoomAssess(AgentKit):
    async def handle(self, request: AgentMessage) -> dict:
        raise RuntimeError("内环炸了")


class SlowAssess(AgentKit):
    async def handle(self, request: AgentMessage) -> dict:
        await asyncio.sleep(0.5)
        return {"risk_level": 1}


async def bring_up(
    bus: InMemoryBus, gateway: AgentGateway, agent_type: AgentType, cls: type[AgentKit], capability: str, agent_id: str
) -> AgentKit:
    agent = cls(transport=bus, agent_type=agent_type, agent_id=agent_id, capabilities=[capability])
    await gateway.start()
    await agent.start(heartbeat_interval=0.05)
    await bus.idle()
    await asyncio.sleep(0.01)
    await bus.idle()
    return agent


class TestLifecycleAndResponse:
    async def test_registers_and_serves_via_gateway(self, bus: InMemoryBus, gateway: AgentGateway, registry: AgentRegistry) -> None:
        agent = await bring_up(bus, gateway, AgentType.ASSESS, EchoAssess, "risk_assess", "assess.kit01")
        assert registry.get("assess.kit01") is not None
        reply = await gateway.dispatch(
            AgentType.ASSESS,
            Action.ASSESS_HAZARD,
            {"region_code": "540121", "hits": [{"rule_id": "R-1", "hazard_type": "debris_flow", "region_code": "540121", "score": 0.9}]},
            trace_id=TRACE,
            capability="risk_assess",
        )
        assert gateway.counters["rejected"] == 0
        assert reply.payload["risk_level"] == 2
        assert reply.payload["assessed_by"] == "assess.kit01"
        await agent.stop()
        await gateway.close()

    async def test_response_carries_d10_provenance(self, bus: InMemoryBus, gateway: AgentGateway, registry: AgentRegistry) -> None:
        agent = await bring_up(bus, gateway, AgentType.ASSESS, ProvenanceAssess, "risk_assess", "assess.kit02")
        reply = await gateway.dispatch(
            AgentType.ASSESS, Action.ASSESS_HAZARD, {"region_code": "540121", "hits": []}, trace_id=TRACE, capability="risk_assess"
        )
        assert gateway.counters["rejected"] == 0, "带 model_info/disagreement 的响应必须过契约"
        assert reply.model_info is not None and reply.model_info.model == "qwen-plus"
        assert reply.model_info.prompt_version == "1.2.0"
        assert reply.disagreement is True
        await agent.stop()
        await gateway.close()

    async def test_internal_crash_becomes_typed_error(self, bus: InMemoryBus, gateway: AgentGateway, registry: AgentRegistry) -> None:
        from aegis.errors import AegisError

        agent = await bring_up(bus, gateway, AgentType.ASSESS, BoomAssess, "risk_assess", "assess.kit03")
        with pytest.raises(AegisError) as info:
            await gateway.dispatch(AgentType.ASSESS, Action.ASSESS_HAZARD, {"region_code": "540121", "hits": []}, trace_id=TRACE)
        assert gateway.transactions[-1].error_code == "E_INTERNAL"
        assert gateway.transactions[-1].outcome == "error"
        assert info.value.retryable is True
        await agent.stop()
        await gateway.close()

    async def test_duplicate_request_single_reply(self, bus: InMemoryBus, gateway: AgentGateway, registry: AgentRegistry) -> None:
        agent = await bring_up(bus, gateway, AgentType.ASSESS, EchoAssess, "risk_assess", "assess.kit04")
        request = make_request(
            source="platform.gateway",
            target=agent.agent_id,
            action=Action.ASSESS_HAZARD.value,
            payload={"region_code": "540121", "hits": []},
            trace_id=TRACE,
            reply_to="reply.gw00000001.inbox",
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


class TestTargetedRouting:
    """P1-1：类型级 subject 是广播，派单必须落到被选中的本体，本体也只认点名给它的请求。

    修前网关一律发到 `agent.<type>.in`，每个同类型本体各订一个独立队列组 → 各收一份、
    各执行一遍；网关只采纳第一封回执，于是"N 个智能体各做了一次动作"在台账上看起来只是一次成功。
    """

    async def _second(self, bus: InMemoryBus, agent_id: str) -> EchoAssess:
        agent = EchoAssess(transport=bus, agent_type=AgentType.ASSESS, agent_id=agent_id, capabilities=["risk_assess"])
        await agent.start(heartbeat_interval=0.05)
        await bus.idle()
        return agent

    async def test_网关只派给一个本体_其余同类型不执行(self, bus: InMemoryBus, gateway: AgentGateway) -> None:
        first = await bring_up(bus, gateway, AgentType.ASSESS, EchoAssess, "risk_assess", "assess.route01")
        second = await self._second(bus, "assess.route02")

        reply = await gateway.dispatch(
            AgentType.ASSESS, Action.ASSESS_HAZARD, {"region_code": "540121", "hits": []}, trace_id=TRACE, capability="risk_assess"
        )
        await bus.idle()

        assert reply.payload["assessed_by"] in {"assess.route01", "assess.route02"}
        handled = {first.agent_id: first.handled, second.agent_id: second.handled}
        assert sum(handled.values()) == 1, f"同类型本体都执行了一遍：{handled}"
        await second.stop()
        await first.stop()
        await gateway.close()

    async def test_公共口上的定向请求只由点名者执行(self, bus: InMemoryBus, gateway: AgentGateway) -> None:
        """类型级公共口无法按地址投递：每个同类型本体都会收到一份，靠 request.target 过滤。"""
        first = await bring_up(bus, gateway, AgentType.ASSESS, EchoAssess, "risk_assess", "assess.route03")
        second = await self._second(bus, "assess.route04")
        request = make_request(
            source="platform.gateway",
            target=first.agent_id,
            action=Action.ASSESS_HAZARD.value,
            payload={"region_code": "540121", "hits": []},
            trace_id=TRACE,
            reply_to="reply.gw00000003.inbox",
            deadline_ms=800,
        )
        replies: list[AgentMessage] = []

        async def collector(message: AgentMessage) -> None:
            replies.append(message)

        await bus.subscribe(request.reply_to or "", collector)
        await bus.publish(subjects.agent_in(AgentType.ASSESS), request)
        await bus.idle()
        await asyncio.sleep(0.05)
        await bus.idle()

        assert [reply.source for reply in replies] == [first.agent_id], "公共口上的定向请求被非目标本体执行了"
        assert second.handled == 0
        await second.stop()
        await first.stop()
        await gateway.close()

    async def test_先建收件箱再宣告上线(self) -> None:
        """注册事件一到平台就可能派单：收件箱晚一步建立，第一条请求就落在空口上。"""
        calls: list[tuple[str, str]] = []
        sub_kwargs: list[dict[str, object]] = []

        class _Transport:
            name = "recording"

            async def publish(self, subject: str, _message: AgentMessage) -> None:
                calls.append(("publish", subject))

            async def subscribe(self, subject: str, _handler: object, **kwargs: object) -> object:
                calls.append(("subscribe", subject))
                sub_kwargs.append(kwargs)

                async def _cancel() -> None:
                    return None

                return type("Sub", (), {"cancel": staticmethod(_cancel)})()

            async def connect(self) -> None: ...
            async def close(self) -> None: ...

        kit = EchoAssess(transport=_Transport(), agent_type=AgentType.ASSESS, agent_id="assess.order01", capabilities=["risk_assess"])
        await kit.start(heartbeat_interval=5)
        await kit.stop()

        assert [kind for kind, _ in calls[:2]] == ["subscribe", "subscribe"], calls
        assert calls[2] == ("publish", subjects.agent_hb(AgentType.ASSESS)), calls
        assert calls[0][1] == subjects.agent_in(AgentType.ASSESS, "assess.order01")
        assert calls[1][1] == subjects.agent_in(AgentType.ASSESS)
        assert [kw.get("durable") for kw in sub_kwargs] == [
            consumer_name("d_in", "assess", "assess.order01"),
            consumer_name("d", "assess", "assess.order01"),
        ], "两个收件口都必须 durable：专属口是网关默认派发目标，核心订阅在断线窗口里会把请求丢在流外无人取"


class TestDeadlineDegradation:
    async def test_internal_timeout_emits_e_timeout_not_silent(self) -> None:
        published: list[tuple[str, AgentMessage]] = []

        class RecordingTransport:
            name = "recording"

            async def publish(self, subject: str, message: AgentMessage) -> None:
                published.append((subject, message))

            async def subscribe(self, *a: object, **k: object):  # pragma: no cover - 不经 start
                raise AssertionError("本用例直接驱动 _on_request，不应订阅")

            async def connect(self) -> None: ...
            async def close(self) -> None: ...

        kit = SlowAssess(transport=RecordingTransport(), agent_type=AgentType.ASSESS, agent_id="assess.slow", capabilities=["risk_assess"])
        request = make_request(
            source="platform.gateway",
            target="assess.slow",
            action=Action.ASSESS_HAZARD.value,
            payload={},
            trace_id=TRACE,
            reply_to="reply.gw00000002.inbox",
            deadline_ms=300,
        )
        await kit._on_request(request)

        assert len(published) == 1
        subject, message = published[0]
        assert subject == "reply.gw00000002.inbox"
        assert message.kind.value == "error"
        assert message.payload["code"] == "E_TIMEOUT"
        assert message.payload["retryable"] is True
        assert kit.degraded == 1


class TestArrivalAnchor:
    """内环预算与网关同锚：锚点是"这条请求到达本体的时刻"。

    实测边界写清楚：总线投递之前的排队在这一层看不见（网关从发出起计时），所以这一锚
    管的是"到达之后我还能花几次模型调用"，不是事务总时限——别把它当成超时的最终裁量者。
    """

    async def test_elapsed_ms_从到达时刻起算且不为未知请求编数(self) -> None:
        published: list[tuple[str, AgentMessage]] = []

        class RecordingTransport:
            name = "recording"

            async def publish(self, subject: str, message: AgentMessage) -> None:
                published.append((subject, message))

            async def subscribe(self, *a: object, **k: object):  # pragma: no cover
                raise AssertionError("直接驱动 _on_request，不该订阅")

            async def connect(self) -> None: ...
            async def close(self) -> None: ...

        kit = EchoAssess(
            transport=RecordingTransport(), agent_type=AgentType.ASSESS, agent_id="assess.anchor", capabilities=["risk_assess"]
        )
        request = make_request(
            source="platform.gateway",
            target="assess.anchor",
            action=Action.ASSESS_HAZARD.value,
            payload={"hits": []},
            trace_id=TRACE,
            reply_to="reply.gw00000009.inbox",
            deadline_ms=1000,
        )
        assert kit.elapsed_ms(request) == 0.0, "没到达过的请求不能凭空报已耗时"
        await kit._on_request(request)
        assert kit.elapsed_ms(request) >= 0.0
        # 到达记录按 msg_id 记账，处理完即清：不然长跑会攒成无界字典
        assert request.msg_id not in kit._arrivals, "完成后可达记录要清掉"
