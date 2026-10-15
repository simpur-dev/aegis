"""研判增强腿（双盲）在链路台账里的可见性（§3.2 + 铁律 7）。

判的是链路怎么记账，不是 assess 内环怎么合成结论——后者在 `tests/unit/test_assess_agent.py`。
两边各钉一半：内环那半保证主结论活下来，这一头保证"增强腿没做成"这件事不被读成"做成了且一致"。
"""

from __future__ import annotations

from typing import Any

from aegis.config import Settings
from aegis.domain.enums import AgentType, HazardType, RiskLevel
from aegis.domain.messages import TriggerHit, make_request, make_response, now_iso
from aegis.pipeline.chain import HazardResponseChain
from aegis.pipeline.stages import HazardStages

REGION = "540121"
TRACE = "trc_" + "1" * 16


def _reading() -> Any:
    from aegis.domain.messages import TelemetryReading

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


def _hit() -> Any:
    return TriggerHit(
        rule_id="R-DEBRIS-RAIN-1",
        hazard_type=HazardType.DEBRIS_FLOW.value,
        region_code=REGION,
        score=0.8,
        evidence_refs=["rain_10min=28.0"],
        observed_at=now_iso(),
    )


def _assess_response(double_blind: str) -> Any:
    request = make_request(
        source="platform.t01",
        target="assess.stub",
        action="assess.hazard",
        payload={"region_code": REGION},
        trace_id=TRACE,
        reply_to="reply.t01.inbox",
    )
    return make_response(
        request,
        source="assess.stub",
        payload={
            "risk_level": int(RiskLevel.ORANGE),
            "hazard_type": HazardType.DEBRIS_FLOW.value,
            "confidence": 0.78,
            "rationale": "10 分钟降雨 28 毫米且泥位上行",
            "evidence_refs": ["R-DEBRIS-RAIN-1"],
            "double_blind": double_blind,
        },
    )


class _Gateway:
    def __init__(self, response: Any) -> None:
        self._response = response

    async def dispatch(self, agent_type: AgentType, action: Any, payload: dict[str, Any], **kwargs: Any) -> Any:
        return self._response

    async def publish_to(self, subject: str, message: Any) -> None: ...


class _Registry:
    def pick(self, agent_type: str, capability: str) -> object | None:
        return object() if agent_type == AgentType.ASSESS.value else None


class _Contracts:
    def stu_errors(self, payloads: list[dict[str, Any]]) -> list[str]:
        return []


async def _run(double_blind: str) -> Any:
    chain = HazardResponseChain(
        HazardStages(
            gateway=_Gateway(_assess_response(double_blind)),  # type: ignore[arg-type]
            registry=_Registry(),  # type: ignore[arg-type]
            contracts=_Contracts(),  # type: ignore[arg-type]
            settings=Settings(env="test", bus_backend="memory", store_backend="memory"),
        )
    )
    return await chain.process([_reading()], region_code=REGION, preset_hits=[_hit()])


class TestBlindLegTrace:
    async def test_增强腿失败落进降级台账且主结论仍是智能体那一遍(self) -> None:
        result = await _run("failed_second:RuntimeError")
        assess = result.stage("assess")
        assert assess is not None and assess.mode == "agent", "主结论没被废：这一段仍是智能体产出"
        assert result.verdict is not None and int(result.verdict.risk_level) == int(RiskLevel.ORANGE)
        assert sum("研判增强腿失败" in note for note in result.degradations) == 1, result.degradations
        assert any("failed_second:RuntimeError" in note for note in result.degradations), "状态串要原样进台账，便于事后查是哪条腿"

    async def test_双盲做成时不写这条降级(self) -> None:
        """反向对照：把判据改成"只要键在就记"，这条就会红——否则上一条用例是空跑。"""
        result = await _run("agreed")
        assert not any("研判增强腿失败" in note for note in result.degradations), result.degradations

    async def test_旧版应答不带这一格时不炸也不记(self) -> None:
        """契约向后兼容：没有 `double_blind` 的应答（老智能体）不该被当成增强腿失败。"""
        request = make_request(
            source="platform.t01",
            target="assess.stub",
            action="assess.hazard",
            payload={"region_code": REGION},
            trace_id=TRACE,
            reply_to="reply.t01.inbox",
        )
        bare = make_response(request, source="assess.stub", payload={"risk_level": 2, "hazard_type": "debris_flow", "confidence": 0.7})
        chain = HazardResponseChain(
            HazardStages(
                gateway=_Gateway(bare),  # type: ignore[arg-type]
                registry=_Registry(),  # type: ignore[arg-type]
                contracts=_Contracts(),  # type: ignore[arg-type]
                settings=Settings(env="test", bus_backend="memory", store_backend="memory"),
            )
        )
        result = await chain.process([_reading()], region_code=REGION, preset_hits=[_hit()])
        assert result.stage("assess") is not None and result.stage("assess").mode == "agent"
        assert not any("研判增强腿失败" in note for note in result.degradations), result.degradations
