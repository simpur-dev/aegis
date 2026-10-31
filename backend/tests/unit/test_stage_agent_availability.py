""" "智能体在册却接不了单"必须写进降级台账，且与"一个都没注册"分开（铁律 7）。

这两种情形过去都只表现为 `mode=local`：值班员分不清"本来就是纯本地部署"和
"该跑智能体的段因为失联/饱和/能力错位退回了本地轨"。本组用例钉住四件事：
① 零注册不算降级；② 注册了但不可用时降级台账带原因；③ 不可用时不会向智能体派单；
④ 研判/决策/执行/反馈四段都接的是同一套判定。
"""

from __future__ import annotations

import time
from typing import Any

from aegis.bus.registry import AgentRegistry
from aegis.config import Settings
from aegis.domain.chain import ChainResult
from aegis.domain.enums import AgentType, HazardType, RiskLevel
from aegis.domain.messages import CapabilityRegistration, TriggerHit, new_event_id
from aegis.domain.verdict import RiskVerdict
from aegis.pipeline.stages import HazardStages

REGION = "540121"
TRACE = "trc_" + "2" * 16


class _Gateway:
    """不可用时不该有派单；`publish_to` 只为执行/反馈段的广播留出接口。"""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.published: list[str] = []

    async def dispatch(self, agent_type: AgentType, action: Any, payload: dict[str, Any], **kwargs: Any) -> Any:
        self.calls.append(agent_type.value)
        raise AssertionError("不可用的智能体不应收到派单")

    async def publish_to(self, subject: str, message: Any) -> None:
        self.published.append(subject)


class _Contracts:
    def stu_errors(self, payloads: list[dict[str, Any]]) -> list[str]:
        return []


def _stages(registry: AgentRegistry, gateway: _Gateway, settings: Settings) -> HazardStages:
    return HazardStages(
        gateway=gateway,  # type: ignore[arg-type]
        registry=registry,
        contracts=_Contracts(),  # type: ignore[arg-type]
        settings=settings,
    )


def _result() -> ChainResult:
    return ChainResult(trace_id=TRACE, event_id=new_event_id())


def _verdict() -> RiskVerdict:
    return RiskVerdict(
        hazard_type=HazardType.DEBRIS_FLOW,
        region_code=REGION,
        risk_level=RiskLevel.ORANGE,
        confidence=0.8,
        rationale="可用性用例",
        hits=[],
    )


def _hit() -> TriggerHit:
    return TriggerHit(rule_id="R-1", hazard_type=HazardType.DEBRIS_FLOW.value, region_code=REGION, score=0.8)


def _register(registry: AgentRegistry, agent_type: AgentType, capability: str, *, concurrency: int = 2) -> str:
    agent_id = f"{agent_type.value}.t01"
    registry.register(
        CapabilityRegistration(
            agent_id=agent_id,
            agent_type=agent_type.value,
            capabilities=[capability],
            max_concurrency=concurrency,
        )
    )
    return agent_id


class TestNoAgentsIsNotDegradation:
    async def test_零注册的研判段保持本地轨且不记降级(self, settings: Settings) -> None:
        gateway = _Gateway()
        result = _result()
        stage = await _stages(AgentRegistry(settings), gateway, settings).assess(result, [_hit()], REGION, TRACE)
        assert stage.mode == "local" and result.verdict is not None
        assert not any("智能体暂不可用" in note for note in result.degradations), result.degradations
        assert gateway.calls == []


class TestRegisteredButUnavailable:
    async def test_失联智能体留下原因且不派单(self, settings: Settings) -> None:
        registry = AgentRegistry(settings)
        agent_id = _register(registry, AgentType.ASSESS, "risk_assess")
        registry.get(agent_id).healthy = False
        gateway = _Gateway()
        result = _result()
        stage = await _stages(registry, gateway, settings).assess(result, [_hit()], REGION, TRACE)

        assert stage.mode == "local" and result.verdict is not None
        assert any("研判降级" in note and "失联 1" in note for note in result.degradations), result.degradations
        assert gateway.calls == [], "派单给已失联的本体只会白等一个超时"

    async def test_能力错位不冒充失联(self, settings: Settings) -> None:
        registry = AgentRegistry(settings)
        _register(registry, AgentType.ASSESS, "other_capability")
        result = _result()
        await _stages(registry, _Gateway(), settings).assess(result, [_hit()], REGION, TRACE)

        note = next(note for note in result.degradations if "研判降级" in note)
        assert "缺能力 1" in note and "失联" not in note, note

    async def test_决策段同样留痕(self, settings: Settings) -> None:
        registry = AgentRegistry(settings)
        agent_id = _register(registry, AgentType.PLAN, "task_decompose", concurrency=1)
        registry.get(agent_id).inflight = 1
        gateway = _Gateway()
        result = _result()
        result.verdict = _verdict()

        units = await _stages(registry, gateway, settings).plan(result, TRACE)

        assert units, "智能体不可用时应回落本地剧本，而不是交白卷"
        assert any("决策降级" in note and "饱和 1" in note for note in result.degradations), result.degradations
        assert gateway.calls == []

    async def test_执行段同样留痕(self, settings: Settings) -> None:
        registry = AgentRegistry(settings)
        agent_id = _register(registry, AgentType.EXECUTE, "warn_publish", concurrency=1)
        registry.get(agent_id).inflight = 1
        result = _result()
        result.verdict = _verdict()

        stage = await _stages(registry, _Gateway(), settings).execute(result, TRACE, started_at=time.perf_counter())

        assert stage.mode == "local" and stage.ok
        assert any("执行降级" in note and "饱和 1" in note for note in result.degradations), result.degradations

    async def test_反馈段同样留痕(self, settings: Settings) -> None:
        registry = AgentRegistry(settings)
        agent_id = _register(registry, AgentType.FEEDBACK, "reach_track", concurrency=1)
        registry.get(agent_id).inflight = 1
        gateway = _Gateway()
        stages = _stages(registry, gateway, settings)
        result = _result()
        draft = await stages._warning_service.generate(_verdict(), event_id=result.event_id, trace_id=TRACE)
        result.warning = draft.record

        stage = await stages.feedback(result, TRACE)

        assert stage.mode == "local"
        assert any("反馈降级" in note and "饱和 1" in note for note in result.degradations), result.degradations
        assert gateway.calls == []
