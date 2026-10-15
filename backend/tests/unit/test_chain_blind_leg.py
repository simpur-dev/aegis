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


def _assess_response(double_blind: str, disagreement: bool | None = None, mode: str = "") -> Any:
    request = make_request(
        source="platform.t01",
        target="assess.stub",
        action="assess.hazard",
        payload={"region_code": REGION},
        trace_id=TRACE,
        reply_to="reply.t01.inbox",
    )
    response = make_response(
        request,
        source="assess.stub",
        payload={
            "risk_level": int(RiskLevel.ORANGE),
            "hazard_type": HazardType.DEBRIS_FLOW.value,
            "confidence": 0.78,
            "rationale": "10 分钟降雨 28 毫米且泥位上行",
            "evidence_refs": ["R-DEBRIS-RAIN-1"],
            "double_blind": double_blind,
            "double_blind_mode": mode,
        },
    )
    # `disagreement` 是 D10 加在信封上的可选格：不带就是 None（没评估过），与 False（评估过且一致）不同
    return response if disagreement is None else response.model_copy(update={"disagreement": disagreement})


class _Gateway:
    def __init__(self, response: Any) -> None:
        self._response = response

    async def dispatch(self, agent_type: AgentType, action: Any, payload: dict[str, Any], **kwargs: Any) -> Any:
        return self._response

    async def publish_to(self, subject: str, message: Any) -> None: ...


class _Registry:
    def pick(self, agent_type: str, capability: str) -> object | None:
        return object() if agent_type == AgentType.ASSESS.value else None


class _NoAgentsRegistry:
    """一个智能体都不在册：研判必须落到规则降级轨，且不许冒充"双盲做过"。"""

    def pick(self, agent_type: str, capability: str) -> object | None:
        return None


class _Contracts:
    def stu_errors(self, payloads: list[dict[str, Any]]) -> list[str]:
        return []


async def _run(double_blind: str, disagreement: bool | None = None, mode: str = "") -> Any:
    chain = HazardResponseChain(
        HazardStages(
            gateway=_Gateway(_assess_response(double_blind, disagreement, mode)),  # type: ignore[arg-type]
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


class TestBlindFactReachesLedger:
    """§3.2 的双盲事实写在信封上（`double_blind` + D10 的 `disagreement`），链路必须把它落到台账。

    只读结论的后果是"两遍不一致、由仲裁改判"与"根本没做双盲"在 `chain_runs` 里长同一个样——
    事后既查不出这条链路当时矛盾过，也没法统计模型自相矛盾的频率。
    """

    async def test_一致与分歧都进研判段的note(self) -> None:
        agreed = await _run("agreed", disagreement=False)
        assert agreed.stage("assess") is not None and "双盲 agreed（两遍一致）" in agreed.stage("assess").note
        arbitrated = await _run("arbitrated", disagreement=True)
        assert arbitrated.stage("assess") is not None
        note = arbitrated.stage("assess").note
        assert "双盲 arbitrated（两遍不一致）" in note, note
        assert not any("研判增强腿失败" in text for text in arbitrated.degradations), "分歧不是故障，别把它染成降级"

    async def test_不带分歧格时说分歧格未带而不是两遍一致(self) -> None:
        """反向对照：把 None 折成 False 判据，这条立刻红——那正是"没评估"冒充"评估过且一致"。"""
        result = await _run("agreed")
        assert result.stage("assess") is not None
        note = result.stage("assess").note
        assert "分歧格未带" in note, note
        assert "两遍一致" not in note, note

    async def test_两腿是并行还是串行也进台账(self) -> None:
        """时延形状是个事实：预触发让两腿同时发车之后，台账要分得开"并行且一致"与"串行且一致"。"""
        result = await _run("agreed", disagreement=False, mode="parallel")
        assert result.stage("assess") is not None
        assert "双盲 parallel/agreed（两遍一致）" in result.stage("assess").note, result.stage("assess").note

    async def test_本地降级轨不提双盲(self) -> None:
        """规则定级这一程没有任何智能体参与，note 里不该出现"双盲"字样。"""
        chain = HazardResponseChain(
            HazardStages(
                gateway=_Gateway(_assess_response("agreed", disagreement=False)),  # type: ignore[arg-type]
                registry=_NoAgentsRegistry(),  # type: ignore[arg-type]
                contracts=_Contracts(),  # type: ignore[arg-type]
                settings=Settings(env="test", bus_backend="memory", store_backend="memory"),
            )
        )
        result = await chain.process([_reading()], region_code=REGION, preset_hits=[_hit()])
        assess = result.stage("assess")
        assert assess is not None and assess.mode == "local"
        assert "双盲" not in assess.note, assess.note
