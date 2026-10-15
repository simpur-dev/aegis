"""§3.5 反馈闭环的总线证据：链路真派发、结论真消费、失败真不拖垮已发布的预警。

为什么写在这里而不是"直调 handle"：这个洞的形状就是"内环单测全绿、生产链一次没调用过它"——
链路只 dispatch assess/plan/execute，feedback 段只广播一条状态事件，于是 `FeedbackAgent` 在册
却永远等不到请求，归因/阈值建议/教训写记忆在真栈上发生 0 次。这里跑真内存总线 + 真内环 + 真链路，
把派发摘掉就红。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from aegis.agents.agentkit import AgentKit
from aegis.agents.feedback import FeedbackAgent, FeedbackAttribution
from aegis.bus.inmemory import InMemoryBus
from aegis.config import Settings
from aegis.domain.enums import Action, AgentType, Channel, HazardType
from aegis.domain.messages import TelemetryReading, TriggerHit, now_iso
from aegis.observability.tracer import Tracer
from aegis.pipeline.chain import HazardResponseChain
from aegis.pipeline.stages import HazardStages
from aegis.services.delivery import DeliveryDispatcher, MockChannelAdapter
from aegis.services.memory import InMemoryAgentMemory, MemoryService

REGION = "540121"
TRACE = "trc_" + "9" * 16


class AttributionLLM:
    """按 task 回一份带阈值建议的归因，并留下调用足迹——证明这一趟钱花在 feedback 这条腿上。"""

    available = True

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def chat_structured(
        self, messages, *, schema, task="default", agent_id=None, trace_id=None, prompt_key=None, prompt_vars=None, **kw
    ):
        self.calls.append(task)
        return FeedbackAttribution(
            attribution="牧区短信通道回执缺失，村组责任人未确认",
            unreached_audiences=["herders"],
            lag_stage="触达回执",
            threshold_suggestion="泥位阈值建议由 42cm 收到 38cm（仅建议，需人工确认）",
            lesson="同类沟道两次都在回执环节迟滞：先补通道再调阈值",
        )

    def model_info_for_task(self, task: str) -> dict[str, Any]:
        return {"model": "qwen-plus", "fallback_model": "", "task": task, "prompt_version": "1.0.0"}


class UnavailableLLM(AttributionLLM):
    """网关在场但当前不可用：内环回落规则摘要，而 `model_info_for_task` 照样给得出模型名。

    这一档才是"冒领凭证"的真场景——`llm=None` 时本来就取不到凭证，守卫删掉也测不出来。
    """

    available = False


@dataclass
class BadFeedbackAgent(AgentKit):
    """内环交回不合契约的结论（缺效果分、或效果分越界）：链路必须弃用而非照单记数。"""

    memory: Any = None
    reply: dict[str, Any] = field(default_factory=lambda: {"warning_id": "wrn_whatever"})

    async def handle(self, request: Any) -> dict[str, Any]:
        return dict(self.reply)


@dataclass
class BoomFeedbackAgent(AgentKit):
    """内环直接炸：预警早已发布，这一腿只能塌自己。"""

    memory: Any = None

    async def handle(self, request: Any) -> dict[str, Any]:
        raise RuntimeError("归因内环炸了")


def _reading() -> TelemetryReading:
    return TelemetryReading(
        station_id="PG-S1",
        metric="rain_10min",
        value=28.0,
        unit="mm",
        region_code=REGION,
        observed_at=now_iso(),
        ingested_at=now_iso(),
        source="mqtt_edge",
    )


def _hit() -> TriggerHit:
    """现成的触发命中：这一段测的是反馈腿，不该被阈值数字牵着走。"""
    return TriggerHit(
        rule_id="R-DEBRIS-RAIN-1",
        hazard_type=HazardType.DEBRIS_FLOW.value,
        region_code=REGION,
        score=0.8,
        evidence_refs=["rain_10min=28.0"],
        observed_at=now_iso(),
    )


def _feedback_agent(bus: InMemoryBus, *, llm: Any, memory: Any, agent_cls: Any = FeedbackAgent, reply: dict[str, Any] | None = None) -> Any:
    extra: dict[str, Any] = {"reply": reply} if reply is not None else {}
    return agent_cls(
        transport=bus,
        agent_type=AgentType.FEEDBACK,
        agent_id="feedback.real01",
        capabilities=["reach_track"],
        llm=llm,
        memory=memory,
        **extra,
    )


def _chain(gateway: Any, registry: Any, contracts: Any, settings: Settings) -> HazardResponseChain:
    return HazardResponseChain(
        HazardStages(
            gateway=gateway,
            registry=registry,
            contracts=contracts,
            settings=settings,
            dispatcher=DeliveryDispatcher(
                {Channel.SMS: MockChannelAdapter(name=Channel.SMS, latency_ms=1.0, receipt_latency_ms=1.0)},
                Tracer(),
            ),
        )
    )


async def _quiesce(bus: InMemoryBus) -> None:
    await bus.idle()
    await asyncio.sleep(0.02)
    await bus.idle()


async def _bring_up(bus: InMemoryBus, gateway: Any, agent: Any) -> None:
    await gateway.start()
    await agent.start(heartbeat_interval=0.05)
    await _quiesce(bus)


async def _capture_ops(bus: InMemoryBus) -> list[dict[str, Any]]:
    """抓平台往 ops 流发的那条反馈状态事件——归因要在这条流上看得见才算闭环。"""
    from aegis.bus import subjects

    seen: list[dict[str, Any]] = []

    async def _handler(message: Any) -> None:
        seen.append(dict(message.payload or {}))

    await bus.subscribe(subjects.ops("feedback", "status"), _handler, queue="ops-capture", durable="ops-capture")
    return seen


class TestFeedbackClosedLoop:
    async def test_链路经总线派发归因且结论落进台账与记忆(
        self, bus: InMemoryBus, gateway: Any, registry: Any, contracts: Any, settings: Settings
    ) -> None:
        memory = MemoryService(InMemoryAgentMemory())
        llm = AttributionLLM()
        agent = _feedback_agent(bus, llm=llm, memory=memory)
        ops = await _capture_ops(bus)
        await _bring_up(bus, gateway, agent)
        try:
            result = await _chain(gateway, registry, contracts, settings).process([_reading()], region_code=REGION, preset_hits=[_hit()])
            stage = result.stage("feedback")
            assert stage is not None and stage.mode == "agent", "反馈段必须由在册智能体产出：只广播不派发就是这次要修的洞"
            assert "效果分" in stage.note and "pending_human_review" in stage.note, stage.note
            # 段上的 model_info 才是 chain_runs.stages 里那一份：只在 result 里过一遍不算落账
            assert stage.model_info is not None and stage.model_info.get("model") == "qwen-plus", stage.as_dict()
            assert result.stage_models.get("feedback"), "模型产出的归因要带 D10 凭证，兑现 README 那句随结论入库"
            assert llm.calls == ["feedback"], "这一趟模型调用发生在 feedback 这条腿上，不是蹭别的段"
            lessons = await memory.read_shared(kind="lesson", region_code=REGION, limit=5)
            assert len(lessons) == 1, "§3.5 的教训写记忆：修前在真栈上发生 0 次"
            assert "回执环节迟滞" in lessons[0].content
            assert result.warning is not None, "反馈腿不动已经发出去的预警"
            await _quiesce(bus)
            assert ops, "ops 反馈流上要有那条状态事件"
            assert ops[-1].get("attribution"), "归因正文要跟着状态事件走，运维侧看的是同一条流"
            score = ops[-1]["effect_score"]
            assert isinstance(score, float) and 0.0 <= score <= 1.0, ops[-1]
        finally:
            await agent.stop()
            await gateway.close()

    async def test_反馈智能体不在册时段仍为本地且无人被派发(
        self, bus: InMemoryBus, gateway: Any, registry: Any, contracts: Any, settings: Settings
    ) -> None:
        await gateway.start()
        try:
            result = await _chain(gateway, registry, contracts, settings).process([_reading()], region_code=REGION, preset_hits=[_hit()])
            stage = result.stage("feedback")
            assert stage is not None and stage.mode == "local"
            assert stage.note == "触达回执已归档", stage.note
            assert "model_info" not in stage.as_dict(), "没人应答的段不该带模型凭证"
            assert "feedback" not in result.stage_models
            assert not any("反馈" in note for note in result.degradations), "没起智能体不是故障，别记成降级"
        finally:
            await gateway.close()

    async def test_归因不合契约时弃用并留痕(
        self, bus: InMemoryBus, gateway: Any, registry: Any, contracts: Any, settings: Settings
    ) -> None:
        agent = _feedback_agent(bus, llm=None, memory=None, agent_cls=BadFeedbackAgent)
        await _bring_up(bus, gateway, agent)
        try:
            result = await _chain(gateway, registry, contracts, settings).process([_reading()], region_code=REGION, preset_hits=[_hit()])
            assert any("反馈结论不合契约" in note for note in result.degradations), result.degradations
            stage = result.stage("feedback")
            assert stage is not None and stage.mode == "local", "没有可信效果分就宁可不记，不许编一个"
            assert stage.model_info is None and "feedback" not in result.stage_models
            assert result.warning is not None
        finally:
            await agent.stop()
            await gateway.close()

    async def test_效果分越界时同样弃用(self, bus: InMemoryBus, gateway: Any, registry: Any, contracts: Any, settings: Settings) -> None:
        """0—1 之外的分数不是"效果分 1.7"，是内环算错了：给个 0 也比记一个越界数强。"""
        agent = _feedback_agent(
            bus, llm=None, memory=None, agent_cls=BadFeedbackAgent, reply={"effect_score": 1.7, "attribution": "越界的分数"}
        )
        await _bring_up(bus, gateway, agent)
        try:
            result = await _chain(gateway, registry, contracts, settings).process([_reading()], region_code=REGION, preset_hits=[_hit()])
            assert any("反馈结论不合契约" in note for note in result.degradations), result.degradations
            stage = result.stage("feedback")
            assert stage is not None and stage.mode == "local" and "效果分" not in stage.note, stage.note
        finally:
            await agent.stop()
            await gateway.close()

    async def test_归因腿失败只塌自己不拖垮已发布的预警(
        self, bus: InMemoryBus, gateway: Any, registry: Any, contracts: Any, settings: Settings
    ) -> None:
        agent = _feedback_agent(bus, llm=None, memory=None, agent_cls=BoomFeedbackAgent)
        await _bring_up(bus, gateway, agent)
        try:
            result = await _chain(gateway, registry, contracts, settings).process([_reading()], region_code=REGION, preset_hits=[_hit()])
            assert any("反馈归因降级" in note for note in result.degradations), result.degradations
            assert result.warning is not None, "预警早已发布：反馈腿的故障不能把它收回"
            assert result.ok, "反馈段的失败收口在反馈段，整条链仍算走通（与研判增强腿同一条判据）"
        finally:
            await agent.stop()
            await gateway.close()

    async def test_llm不可用时规则归因仍产出但不冒领模型凭证(
        self, bus: InMemoryBus, gateway: Any, registry: Any, contracts: Any, settings: Settings
    ) -> None:
        """网关在场却不可用：这一段确实是智能体应答的（mode=agent），但正文是规则摘要，不是模型写的。"""
        memory = MemoryService(InMemoryAgentMemory())
        llm = UnavailableLLM()
        agent = _feedback_agent(bus, llm=llm, memory=memory)
        await _bring_up(bus, gateway, agent)
        try:
            result = await _chain(gateway, registry, contracts, settings).process([_reading()], region_code=REGION, preset_hits=[_hit()])
            stage = result.stage("feedback")
            assert stage is not None and stage.mode == "agent" and "归因腿 local" in stage.note, stage.note
            assert llm.calls == [], "网关判不可用时不该还去敲它一次"
            assert "feedback" not in result.stage_models, "规则摘要记到模型头上=伪造审计证据（与规划回落本地剧本不记同判据）"
            assert stage.model_info is None, stage.as_dict()
            lessons = await memory.read_shared(kind="lesson", region_code=REGION, limit=5)
            assert len(lessons) == 1, "LLM 缺席也要产出状态与教训——§3.5 的恒产出边界"
        finally:
            await agent.stop()
            await gateway.close()


class TestFeedbackContractSurface:
    def test_派发用独立动作而不蹭状态事件(self) -> None:
        """请求动作与广播动作分开：混用一个动作时，"谁该回这条"没有判据。"""
        assert Action.FEEDBACK_ATTRIBUTE.value == "feedback.attribute"
        assert Action.FEEDBACK_ATTRIBUTE.value != Action.FEEDBACK_STATUS.value
