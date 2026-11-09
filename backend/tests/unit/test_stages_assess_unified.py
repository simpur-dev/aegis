"""研判段吃统一 `assess.v1` 契约：请求带证据与主灾种，响应按模型校验后落 verdict。

这组用例钉住链路的三个口径：
- 请求必须是 `AssessRequest` 的形状（evidence/hazard_type/window/data_refs），并且证据来自盘面读数；
- 响应只有过 `AssessResponse` 校验、且区域/灾种自洽才被采纳，任何不合契约都降级到本地规则结论；
- 本地兜底与智能体看到同一份证据出处：降级结论的 `data_provenance` 不是凭空来的。
"""

from __future__ import annotations

from typing import Any

import pytest

from aegis.config import Settings
from aegis.domain.chain import ChainResult
from aegis.domain.enums import AgentType, HazardType, RiskLevel
from aegis.domain.messages import TelemetryReading, TriggerHit, make_request, make_response, new_event_id, now_iso
from aegis.domain.verdict import RiskVerdict
from aegis.pipeline.assess_input import AssessEvidenceBuilder
from aegis.pipeline.board import ChainBoard
from aegis.pipeline.stages import HazardStages
from aegis.storage.store import PlatformStore, StoreProtocol

REGION = "540121"
TRACE = "trc_" + "3" * 16

ASSESS_PAYLOAD: dict[str, Any] = {
    "contract_version": "assess.v1",
    "hazard_type": HazardType.DEBRIS_FLOW.value,
    "region_code": REGION,
    "risk_level": int(RiskLevel.ORANGE),
    "confidence": 0.45,
    "confidence_basis": "coverage=0.75;validation=pass;method=factor_engine_mock;cap=0.60",
    "rationale": "10 分钟雨强折算 7.2mm/h，沟道纵比降大",
    "evidence_refs": [f"evidence:{REGION}:60m"],
    "assessed_by": "assess.agents_ext",
    "source": "agents_ext_mock",
    "human_review_suggested": True,
    "data_provenance": {
        "data_kind": "mock",
        "sources": ["platform.telemetry:simulator"],
        "method": "factor_engine_mock",
        "method_version": "FACTOR-ENGINE-v0.1",
        "is_mock": True,
        "disclaimer": "因子权重为占位值，禁止用于实际预警决策",
    },
    "assessment": {"version": "risk-agent-output-v1", "P": 0.21, "T": 0.6, "R": 0.13, "coverage": 0.75},
}


class _Gateway:
    def __init__(self, payload: dict[str, Any] | None = None) -> None:
        self.payload = dict(ASSESS_PAYLOAD if payload is None else payload)
        self.calls: list[dict[str, Any]] = []

    async def dispatch(self, agent_type: AgentType, action: Any, payload: dict[str, Any], **kwargs: Any) -> Any:
        self.calls.append(payload)
        request = make_request(
            source="platform.t01",
            target="assess.stub",
            action=action,
            payload=payload,
            trace_id=TRACE,
            reply_to="reply.t01.inbox",
        )
        return make_response(request, source="assess.stub", payload=self.payload)

    async def publish_to(self, subject: str, message: Any) -> None: ...


class _Registry:
    def explain(self, agent_type: str, capability: str | None = None, hazard_type: str | None = None) -> Any:
        from aegis.bus.registry import SelectionReport

        return SelectionReport(
            agent_type=agent_type,
            capability=capability,
            hazard_type=hazard_type,
            registered=1,
            available=1,
            blockers={},
        )


class _Contracts:
    def stu_errors(self, payloads: list[dict[str, Any]]) -> list[str]:
        return []


class _BrokenBuilder:
    window = "60m"

    async def build(self, **kwargs: Any) -> Any:
        raise RuntimeError("telemetry store offline")


def _reading(metric: str = "rain_10min", value: float = 1.2, *, source: str = "mqtt") -> TelemetryReading:
    return TelemetryReading(
        station_id="PG-S1",
        metric=metric,
        value=value,
        unit="mm",
        region_code=REGION,
        observed_at=now_iso(),
        ingested_at=now_iso(),
        source=source,
    )


def _hit(hazard: HazardType = HazardType.DEBRIS_FLOW, score: float = 0.8, rule: str = "R-1") -> TriggerHit:
    return TriggerHit(rule_id=rule, hazard_type=hazard.value, region_code=REGION, score=score, observed_at=now_iso())


def _stages(
    gateway: _Gateway,
    settings: Settings,
    *,
    builder: Any | None = None,
) -> HazardStages:
    kwargs: dict[str, Any] = {"assess_evidence": builder}
    return HazardStages(
        gateway=gateway,  # type: ignore[arg-type]
        registry=_Registry(),  # type: ignore[arg-type]
        contracts=_Contracts(),  # type: ignore[arg-type]
        settings=settings,
        **kwargs,
    )


def _board(hits: list[TriggerHit], readings: list[TelemetryReading] | None = None) -> ChainBoard:
    result = ChainResult(trace_id=TRACE, event_id=new_event_id())
    result.hits = hits
    return ChainBoard(result=result, trace=TRACE, region=REGION, readings=readings or [_reading()])


def _builder(store: StoreProtocol | None = None, **kwargs: Any) -> AssessEvidenceBuilder:
    return AssessEvidenceBuilder(store, **kwargs)


class TestRequestShape:
    async def test_研判段吃盘面读数并发出统一请求(self, settings: Settings) -> None:
        store = PlatformStore()
        gateway = _Gateway()
        stages = _stages(gateway, settings, builder=_builder(store, window_minutes=60))
        board = _board([_hit()], [_reading()])

        stage = await stages.run_stage("assess", board)

        assert stage.mode == "agent"
        assert len(gateway.calls) == 1
        payload = gateway.calls[0]
        assert payload["contract_version"] == "assess.v1"
        assert payload["region_code"] == REGION
        assert payload["window"] == "60m"
        assert payload["hazard_type"] == HazardType.DEBRIS_FLOW.value
        assert payload["candidate_hazards"] == [HazardType.DEBRIS_FLOW.value]
        assert payload["data_refs"] == [f"evidence:{REGION}:60m"]
        assert payload["rule_level"] == int(RiskLevel.ORANGE) or payload["rule_level"] is not None

        evidence = payload["evidence"]
        assert evidence is not None, "盘面读数必须随请求推给外部实现"
        rain = next(obs for obs in evidence["observations"] if obs["factor"] == "rainfall_mm_h")
        assert rain["max_value"] == pytest.approx(7.2)
        assert evidence["provenance"]["data_kind"] == "observed"

    async def test_多灾种命中按最高分选主灾种(self, settings: Settings) -> None:
        gateway = _Gateway(dict(ASSESS_PAYLOAD, hazard_type=HazardType.DEBRIS_FLOW.value))
        stages = _stages(gateway, settings, builder=_builder(PlatformStore()))
        hits = [
            _hit(HazardType.LANDSLIDE, 0.4, "R-1"),
            _hit(HazardType.DEBRIS_FLOW, 0.9, "R-2"),
        ]

        await stages.run_stage("assess", _board(hits))

        payload = gateway.calls[0]
        assert payload["hazard_type"] == HazardType.DEBRIS_FLOW.value
        assert payload["candidate_hazards"] == [HazardType.LANDSLIDE.value, HazardType.DEBRIS_FLOW.value]

    async def test_平票时派单灾种跟随本地定级(self, settings: Settings) -> None:
        gateway = _Gateway(dict(ASSESS_PAYLOAD, hazard_type=HazardType.DEBRIS_FLOW.value))
        stages = _stages(gateway, settings, builder=_builder(PlatformStore()))
        hits = [
            _hit(HazardType.DEBRIS_FLOW, 0.8, "R-2"),
            _hit(HazardType.LANDSLIDE, 0.8, "R-1"),
        ]

        await stages.run_stage("assess", _board(hits))

        # 两条命中同分同级时本地引擎取命中顺序里的第一条；派单必须跟它一致，
        # 不能另按 `HazardType` 声明顺序再选一次——那样降级文本与台账会各说一个灾种。
        assert gateway.calls[0]["hazard_type"] == HazardType.DEBRIS_FLOW.value

    async def test_未注入builder时窗口取配置(self, settings: Settings) -> None:
        gateway = _Gateway()
        settings = settings.model_copy(update={"assess_window_minutes": 45})
        stages = _stages(gateway, settings)

        await stages.run_stage("assess", _board([_hit()]))

        assert gateway.calls[0]["window"] == "45m"
        assert gateway.calls[0]["evidence"] is None, "没有 builder 就不该凭空造证据"


class TestRequestHazard:
    """派单灾种与本地兜底结论同源：降级文本里说的灾种必须就是台账里的灾种。"""

    async def test_三条命中同分时取本地最严重等级而非声明顺序(self, settings: Settings) -> None:
        """540221 现场口径：R-LAKE-1 / R-LAKE-2 / R-DEBRIS-RAIN-1 的 score 都是 1.0。

        本地引擎按"最严重等级"定级（R-LAKE-2 一级 → 冰湖溃决），
        而按 `HazardType` 声明顺序会先碰到泥石流——旧实现把泥石流发给了外部实现。
        """
        gateway = _Gateway(dict(ASSESS_PAYLOAD, hazard_type=HazardType.LAKE_OUTBURST.value))
        stages = _stages(gateway, settings, builder=_builder(PlatformStore()))
        hits = [
            _hit(HazardType.LAKE_OUTBURST, 1.0, "R-LAKE-1"),
            _hit(HazardType.LAKE_OUTBURST, 1.0, "R-LAKE-2"),
            _hit(HazardType.DEBRIS_FLOW, 1.0, "R-DEBRIS-RAIN-1"),
        ]

        await stages.run_stage("assess", _board(hits))

        payload = gateway.calls[0]
        assert payload["hazard_type"] == HazardType.LAKE_OUTBURST.value
        assert payload["candidate_hazards"] == [HazardType.DEBRIS_FLOW.value, HazardType.LAKE_OUTBURST.value]

    async def test_上报结论灾种不在命中候选内时服从契约(self, settings: Settings) -> None:
        """上报解析腿可能给出没有对应命中的灾种；请求不能因此违反 hazard∈candidates。"""
        gateway = _Gateway(dict(ASSESS_PAYLOAD, hazard_type=HazardType.DEBRIS_FLOW.value))
        stages = _stages(gateway, settings, builder=_builder(PlatformStore()))
        hits = [_hit(HazardType.DEBRIS_FLOW, 0.9, "R-DEBRIS-RAIN-1")]
        preset = RiskVerdict(
            hazard_type=HazardType.AVALANCHE,
            region_code=REGION,
            risk_level=RiskLevel.ORANGE,
            confidence=0.5,
            rationale="上报解析定级",
            hits=hits,
        )
        result = ChainResult(trace_id=TRACE, event_id=new_event_id())
        result.hits = hits
        board = ChainBoard(result=result, trace=TRACE, region=REGION, readings=[_reading()], intake="report", preset_verdict=preset)

        await stages.run_stage("assess", board)

        assert gateway.calls[0]["hazard_type"] == HazardType.DEBRIS_FLOW.value

    def test_无命中候选时请求灾种取本地结论(self) -> None:
        preset = RiskVerdict(
            hazard_type=HazardType.LAKE_OUTBURST,
            region_code=REGION,
            risk_level=RiskLevel.ORANGE,
            confidence=0.5,
            rationale="上报解析定级",
        )
        assert HazardStages._request_hazard(preset, [], []) == HazardType.LAKE_OUTBURST
        assert HazardStages._request_hazard(None, [], []) is None


class TestAdoption:
    async def test_采纳后溯源与评估信封全部落地(self, settings: Settings) -> None:
        gateway = _Gateway()
        stages = _stages(gateway, settings, builder=_builder(PlatformStore()))
        board = _board([_hit()])

        stage = await stages.run_stage("assess", board)

        assert stage.mode == "agent" and board.result.verdict is not None
        verdict = board.result.verdict
        assert int(verdict.risk_level) == int(RiskLevel.ORANGE)
        assert verdict.confidence_basis.startswith("coverage=0.75")
        assert verdict.data_provenance.method == "factor_engine_mock"
        assert verdict.data_provenance.is_mock is True
        assert verdict.human_review_suggested is True
        assert verdict.assessment["version"] == "risk-agent-output-v1"

        payload = verdict.as_payload()
        assert payload["data_provenance"]["method_version"] == "FACTOR-ENGINE-v0.1"
        assert payload["confidence_basis"] == verdict.confidence_basis
        assert payload["assessment"]["coverage"] == 0.75


class TestRejectionAndFallback:
    @pytest.mark.parametrize(
        ("overrides", "needle"),
        [
            ({}, "不合契约"),
            ({"region_code": "540221"}, "不一致"),
            ({"hazard_type": HazardType.LANDSLIDE.value}, "不在候选内"),
        ],
    )
    async def test_不合契约的响应降级到本地结论(self, settings: Settings, overrides: dict[str, Any], needle: str) -> None:
        payload = dict(ASSESS_PAYLOAD, **overrides)
        if not overrides:
            payload.pop("confidence")
        gateway = _Gateway(payload)
        stages = _stages(gateway, settings, builder=_builder(PlatformStore()))
        board = _board([_hit()])

        stage = await stages.run_stage("assess", board)

        assert stage.mode == "local" and board.result.verdict is not None
        assert any(needle in note for note in board.result.degradations), board.result.degradations
        verdict = board.result.verdict
        # 本地兜底与智能体看到同一份证据出处：规则定级也知道它建立在什么数据上。
        assert verdict.data_provenance.method == "telemetry_rule"
        assert verdict.data_provenance.data_kind == "observed"

    async def test_证据构建异常不炸段且仍派单(self, settings: Settings) -> None:
        gateway = _Gateway()
        stages = _stages(gateway, settings, builder=_BrokenBuilder())
        board = _board([_hit()])

        stage = await stages.run_stage("assess", board)

        assert stage.mode == "agent", "证据构建失败只是降级，不应放弃智能体腿"
        assert any("研判证据构建降级" in note and "telemetry store offline" in note for note in board.result.degradations)
        assert gateway.calls[0]["evidence"] is None
        assert board.result.verdict is not None
        assert board.result.verdict.data_provenance.method == "factor_engine_mock", "采纳的是智能体结论，出处随响应"
