"""感知段的智能体接线与命中隔离（P1-3）。

四条事实各自钉住一个曾会静默出错的读法：
① 感知段真的向 perceive 派单——"在册"不等于"接入"，此前五段里只有这一段从不发请求；
② 主动事件命中按 trace + 区域 + TTL 隔离，同区域并发链路不会互相吞并命中；
③ 应答 `trigger_hits` 逐条过契约，坏载荷与跨区域命中丢弃并在降级台账留名；
④ 派单失败只降级感知腿，本地规则命中照常产出（链路永不阻断）。
"""

from __future__ import annotations

from typing import Any

from aegis.bus.registry import AgentRegistry
from aegis.config import Settings
from aegis.domain.chain import ChainResult
from aegis.domain.enums import Action, AgentType, HazardType
from aegis.domain.messages import (
    AgentMessage,
    CapabilityRegistration,
    TriggerHit,
    make_event,
    make_request,
    make_response,
    new_event_id,
)
from aegis.errors import AegisError
from aegis.pipeline.stages import HazardStages
from conftest import REGION, readings_rain_burst

TRACE = "trc_" + "4" * 16
OTHER_TRACE = "trc_" + "5" * 16


class _Contracts:
    def stu_errors(self, payloads: list[dict[str, Any]]) -> list[str]:
        return []


class _Gateway:
    """记录派单并回一份感知应答；不碰总线（这一段的行为在这里量）。"""

    def __init__(self, payload: dict[str, Any] | None = None, *, error: AegisError | None = None) -> None:
        self.payload = {"trigger_hits": []} if payload is None else payload
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def dispatch(self, agent_type: AgentType, action: Action, payload: dict[str, Any], **kwargs: Any) -> AgentMessage:
        self.calls.append({"agent_type": agent_type, "action": action, "payload": payload, **kwargs})
        if self.error is not None:
            raise self.error
        request = make_request(
            source="platform.gateway",
            target=f"{agent_type.value}.t01",
            action=action.value,
            payload=payload,
            trace_id=kwargs["trace_id"],
            reply_to="reply.gw00000001.inbox",
            deadline_ms=kwargs["deadline_ms"],
        )
        return make_response(request, source=f"{agent_type.value}.t01", payload=self.payload)

    async def publish_to(self, subject: str, message: AgentMessage) -> None: ...


def _registry(settings: Settings) -> AgentRegistry:
    registry = AgentRegistry(settings)
    registry.register(CapabilityRegistration(agent_id="perceive.t01", agent_type="perceive", capabilities=["anomaly_detect"]))
    return registry


def _stages(
    settings: Settings,
    gateway: Any,
    *,
    registry: AgentRegistry | None = None,
    ttl: float | None = None,
) -> HazardStages:
    return HazardStages(
        gateway=gateway,
        registry=registry if registry is not None else AgentRegistry(settings),
        contracts=_Contracts(),  # type: ignore[arg-type]
        settings=settings,
        agent_hit_ttl_seconds=ttl,
    )


def _result(trace: str = TRACE) -> ChainResult:
    return ChainResult(trace_id=trace, event_id=new_event_id())


def _hit(rule_id: str = "R-AGENT-1", region: str = REGION) -> TriggerHit:
    return TriggerHit(rule_id=rule_id, hazard_type=HazardType.DEBRIS_FLOW.value, region_code=region, score=0.7)


def _event_hit(hit: TriggerHit, trace: str) -> AgentMessage:
    return make_event(source="perceive.t01", action=Action.PERCEIVE_TRIGGER_HIT.value, payload=hit.model_dump(), trace_id=trace)


class TestScanDispatch:
    async def test_有读数就向感知智能体派单并合并应答命中(self, settings: Settings) -> None:
        gateway = _Gateway({"trigger_hits": [_hit("R-SCAN-1").model_dump()]})
        stages = _stages(settings, gateway, registry=_registry(settings))
        result = _result()

        stage = await stages.perceive(result, readings_rain_burst(), REGION, TRACE)

        call = gateway.calls[0]
        assert call["agent_type"] is AgentType.PERCEIVE
        assert call["action"] is Action.ASSESS_HAZARD, "规范里平台→perceive 的请求入口就是这条动作"
        assert call["capability"] == "anomaly_detect"
        assert len(call["payload"]["readings"]) == 3, "派单必须带上本批读数，否则智能体无从扫描"
        assert {h.rule_id for h in result.hits} == {"R-DEBRIS-RAIN-1", "R-SCAN-1"}
        assert stage.mode == "hybrid" and "智能体 1 条" in stage.note

    async def test_没有读数就不派单(self, settings: Settings) -> None:
        gateway = _Gateway()
        stages = _stages(settings, gateway, registry=_registry(settings))

        await stages.perceive(_result(), [], REGION, TRACE)

        assert gateway.calls == []

    async def test_派单失败只降级感知腿且本地命中照常(self, settings: Settings) -> None:
        gateway = _Gateway(error=AegisError("感知事务超时"))
        stages = _stages(settings, gateway, registry=_registry(settings))
        result = _result()

        stage = await stages.perceive(result, readings_rain_burst(), REGION, TRACE)

        assert any("感知降级" in note and "感知事务超时" in note for note in result.degradations), result.degradations
        assert stage.mode == "local"
        assert [h.rule_id for h in result.hits] == ["R-DEBRIS-RAIN-1"]

    async def test_应答里的坏命中与跨区域命中被丢弃并留痕(self, settings: Settings) -> None:
        gateway = _Gateway(
            {"trigger_hits": [{"rule_id": "R-BAD"}, _hit("R-FOREIGN", region="540200").model_dump(), _hit("R-SCAN-2").model_dump()]}
        )
        stages = _stages(settings, gateway, registry=_registry(settings))
        result = _result()

        await stages.perceive(result, readings_rain_burst(), REGION, TRACE)

        rule_ids = {h.rule_id for h in result.hits}
        assert "R-SCAN-2" in rule_ids
        assert "R-FOREIGN" not in rule_ids and "R-BAD" not in rule_ids
        assert any("不合契约" in note for note in result.degradations), result.degradations
        assert any("不属于区域" in note for note in result.degradations), result.degradations

    async def test_应答缺trigger_hits时可见降级而不是静默空手(self, settings: Settings) -> None:
        gateway = _Gateway({"anomalies": []})
        stages = _stages(settings, gateway, registry=_registry(settings))
        result = _result()

        stage = await stages.perceive(result, readings_rain_burst(), REGION, TRACE)

        assert any("不含 trigger_hits" in note for note in result.degradations), result.degradations
        assert stage.mode == "local"

    async def test_与本地同rule_id的应答命中不重复计数(self, settings: Settings) -> None:
        gateway = _Gateway({"trigger_hits": [_hit("R-DEBRIS-RAIN-1").model_dump()]})
        stages = _stages(settings, gateway, registry=_registry(settings))
        result = _result()

        stage = await stages.perceive(result, readings_rain_burst(), REGION, TRACE)

        assert [h.rule_id for h in result.hits] == ["R-DEBRIS-RAIN-1"]
        assert "智能体 0 条" in stage.note, "两路说的是同一条命中，不能计成两条"


class TestRealDetectorSharesRuleIds:
    def test_同一条越限在平台规则轨与智能体快腿用同一个rule_id(self) -> None:
        """真机装配的两条腿问的是同一张规则表：id 不同就会在合并时重复计证据、虚抬置信度。"""
        from aegis.agents.perceive import PerceiveAgent
        from aegis.agents.runtime import RuleEngineDetector
        from aegis.services.rules.trigger_rules import RuleEngine

        engine = RuleEngine()
        readings = readings_rain_burst()
        local = {hit.rule_id for hit in engine.evaluate(readings, region_code=REGION).hits}
        agent = PerceiveAgent(
            transport=None,  # type: ignore[arg-type]
            agent_type=AgentType.PERCEIVE,
            agent_id="perceive.t02",
            capabilities=["anomaly_detect"],
            detector=RuleEngineDetector(engine),
        )

        agent_ids = {hit["rule_id"] for hit in agent.detect([r.model_dump() for r in readings])}

        assert agent_ids, "这组读数应被快腿判成越限，否则这条用例证不到合并口径"
        assert agent_ids <= local, f"智能体自造了规则表之外的 id：{agent_ids - local}"


class TestEventHitIsolation:
    async def test_别的链路的命中不并入本链路且留在盘面(self, settings: Settings) -> None:
        stages = _stages(settings, _Gateway())
        await stages.handle_agent_message(_event_hit(_hit("R-OTHER"), OTHER_TRACE))
        result = _result()

        await stages.perceive(result, readings_rain_burst(), REGION, TRACE)

        assert "R-OTHER" not in {h.rule_id for h in result.hits}
        assert [h.rule_id for h in stages._drain_agent_hits(REGION, OTHER_TRACE)] == ["R-OTHER"], "它得留给自己那条链路"

    async def test_同trace同区域的事件命中会并入且只计一次(self, settings: Settings) -> None:
        stages = _stages(settings, _Gateway())
        await stages.handle_agent_message(_event_hit(_hit("R-EVT-1"), TRACE))
        result = _result()

        stage = await stages.perceive(result, readings_rain_burst(), REGION, TRACE)

        assert "R-EVT-1" in {h.rule_id for h in result.hits}
        assert stage.mode == "hybrid" and "智能体 1 条" in stage.note

    async def test_超过存活窗的事件命中被清出盘面(self, settings: Settings) -> None:
        stages = _stages(settings, _Gateway(), ttl=0.0)
        await stages.handle_agent_message(_event_hit(_hit("R-STALE"), TRACE))
        result = _result()

        await stages.perceive(result, readings_rain_burst(), REGION, TRACE)

        assert "R-STALE" not in {h.rule_id for h in result.hits}
        assert stages._drain_agent_hits(REGION, TRACE) == []

    async def test_感知标注不掺进命中合并(self, settings: Settings) -> None:
        """`perceive.anomaly` 是单点标注，没有 rule_id/hazard_type：它不参与链路命中，也不该被误读成丢了。"""
        stages = _stages(settings, _Gateway())
        await stages.handle_agent_message(
            make_event(
                source="perceive.t01",
                action=Action.PERCEIVE_ANOMALY.value,
                payload={"station_id": "RG-01", "metric": "rain_10min", "value": 42.0, "observed_at": "2026-10-31T00:00:00Z"},
                trace_id=TRACE,
            )
        )

        assert stages._drain_agent_hits(REGION, TRACE) == []
