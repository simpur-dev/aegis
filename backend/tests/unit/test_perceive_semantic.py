"""perceive 语义腿（§3.1 文本线索）的口径：委派给平台三路融合解析器，缺位与异常都要留痕。

这双腿刻意【不复做融合】：规则/案例/LLM 与证据的三路融合已经在 `services.semantic_parser`
里实现并被 `submit_report` 用着，智能体再写一套就是"同一份文本两套结论"的两张皮。
所以这里的断言只验三件事：委派参数原样传下去、解析结果如实外显（decided_by/confidence/
needs_review/degradations）、以及这条腿拿不到结论时是【可见降级】而不是凭空造命中。
"""

from __future__ import annotations

import inspect
from typing import Any

from aegis.agents.factory import build_real_agents
from aegis.agents.perceive import PerceiveAgent, SemanticParser, StationAnomaly
from aegis.domain.enums import AgentType, HazardType
from aegis.domain.messages import TriggerHit, make_request, now_iso
from aegis.services.semantic_parser import DisasterTextParser

REGION = "540121"


class Stub:
    """顶替 ParsedDisaster 的最小形状（属性访问，与真解析器同名）。"""

    def __init__(
        self,
        *,
        hits: tuple[TriggerHit, ...] = (),
        confidence: float = 0.8,
        decided_by: str = "rule+llm",
        needs_review: bool = False,
        degradations: tuple[str, ...] = (),
    ) -> None:
        self.hits = hits
        self.confidence = confidence
        self.decided_by = decided_by
        self.needs_review = needs_review
        self.degradations = degradations


class FakeParser:
    def __init__(self, result: Stub | None = None, *, error: Exception | None = None) -> None:
        self.result = result or Stub()
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def parse(self, text: str, *, region_code: str | None = None, hazard_hint: str | None = None) -> Stub:
        self.calls.append({"text": text, "region_code": region_code, "hazard_hint": hazard_hint})
        if self.error is not None:
            raise self.error
        return self.result


def hit(rule_id: str = "R-REPORT-LLM", score: float = 0.7) -> TriggerHit:
    return TriggerHit(
        rule_id=rule_id,
        hazard_type=HazardType.DEBRIS_FLOW.value,
        region_code=REGION,
        evidence_refs=["note=河口有响声"],
        score=score,
        observed_at=now_iso(),
    )


def agent(parser: Any | None = None, **kw: Any) -> PerceiveAgent:
    return PerceiveAgent(
        transport=None,
        agent_type=AgentType.PERCEIVE,
        agent_id="perceive.t01",
        capabilities=["trigger_recognize"],
        semantic=parser,
        **kw,
    )  # type: ignore[arg-type]


def request(payload: dict[str, Any]) -> Any:
    return make_request(
        source="platform.t01",
        target="perceive.t01",
        action="perceive.scan",
        payload=payload,
        trace_id="trc_" + "0" * 16,
        reply_to="reply.t01.inbox",
        deadline_ms=3000,
    )


async def test_没有文本就完全不碰解析器() -> None:
    parser = FakeParser()
    reply = await agent(parser).handle(request({"readings": []}))
    assert reply.payload["semantic"]["status"] == "no_text"
    assert parser.calls == []


async def test_有文本但没有解析器时可见降级且不凭空造命中() -> None:
    """变异点：若缺位时改用"随便给一条命中"，这条会拿到非空 hits 而红。"""
    reply = await agent(None).handle(request({"text": "河口有响声，坡面开裂"}))
    semantic = reply.payload["semantic"]
    assert semantic["status"] == "skipped_no_parser"
    assert "hits" not in semantic, "合并后的命中只从 trigger_hits 一个出口走，不留第二份口径"
    assert reply.payload["trigger_hits"] == []
    assert semantic["human_review_suggested"] is True


async def test_委派时把区域与灾种提示原样传给平台解析器() -> None:
    parser = FakeParser(Stub(hits=(hit(),), confidence=0.82))
    reply = await agent(parser).handle(request({"text": "坡面开裂", "region_code": REGION, "hazard_hint": "landslide"}))
    assert parser.calls == [{"text": "坡面开裂", "region_code": REGION, "hazard_hint": "landslide"}]
    assert reply.payload["semantic"]["status"] == "ok"


async def test_note别名与text同权且结论字段如实外显() -> None:
    parser = FakeParser(Stub(hits=(hit(),), confidence=0.61, decided_by="rule+case", degradations=("LLM 腿超时",)))
    reply = await agent(parser).handle(request({"note": "河口有响声"}))
    assert parser.calls[0]["text"] == "河口有响声"
    semantic = reply.payload["semantic"]
    assert semantic["decided_by"] == "rule+case"
    assert semantic["confidence"] == 0.61
    assert semantic["degradations"] == ["LLM 腿超时"]
    assert len(reply.payload["trigger_hits"]) == 1
    TriggerHit.model_validate(reply.payload["trigger_hits"][0])


async def test_置信度低于门限就建议人工核签() -> None:
    low = await agent(FakeParser(Stub(confidence=0.4))).handle(request({"text": "好像有水声"}))
    assert low.payload["semantic"]["human_review_suggested"] is True
    high = await agent(FakeParser(Stub(confidence=0.9))).handle(request({"text": "坡面持续开裂"}))
    assert high.payload["semantic"]["human_review_suggested"] is False


async def test_解析器需要核签时不被置信度覆盖() -> None:
    reply = await agent(FakeParser(Stub(confidence=0.95, needs_review=True))).handle(request({"text": "有裂缝"}))
    assert reply.payload["semantic"]["needs_review"] is True
    assert reply.payload["semantic"]["human_review_suggested"] is True


async def test_解析器抛异常时快腿命中照常交付() -> None:
    """降级要可见但不能把另一条腿一起拖死：快腿是数值判据，与 LLM 无关。"""

    class DriftingDetector:
        def flag(self, reading: dict[str, Any]) -> Any:
            return StationAnomaly(
                station_id=reading["station_id"],
                region_code=REGION,
                hazard_type="landslide",
                value=float(reading["value"]),
                unit="mm",
                observed_at=reading["observed_at"],
                base_score=0.7,
            )

    subject = agent(FakeParser(error=RuntimeError("网关超时")), detector=DriftingDetector())
    reply = await subject.handle(request({"text": "有裂缝", "readings": [{"station_id": "ST-1", "value": 9.0, "observed_at": now_iso()}]}))
    semantic = reply.payload["semantic"]
    assert semantic["status"] == "degraded" and "RuntimeError" in semantic["reason"]
    assert semantic["human_review_suggested"] is True
    assert len(reply.payload["trigger_hits"]) == 1
    assert reply.payload["anomaly_count"] == 1, "anomaly_count 只数快腿，不能掺进语义命中"


async def test_两腿都有产出时命中合流且计数不混() -> None:
    parser = FakeParser(Stub(hits=(hit("R-REPORT-LLM"), hit("R-REPORT-RULE", 0.5))))

    class Two:
        def flag(self, reading: dict[str, Any]) -> Any:
            return StationAnomaly(
                station_id=reading["station_id"],
                region_code=REGION,
                hazard_type="debris_flow",
                value=float(reading["value"]),
                unit="mm",
                observed_at=reading["observed_at"],
                base_score=0.6,
            )

    reply = await agent(parser, detector=Two()).handle(
        request({"text": "河口冒浑水", "readings": [{"station_id": f"ST-{i}", "value": 3.0, "observed_at": now_iso()} for i in range(2)]})
    )
    # 两站同区同灾种 → 快腿聚合成 1 条；语义腿 2 条
    assert reply.payload["anomaly_count"] == 1
    assert len(reply.payload["trigger_hits"]) == 3
    assert {h["rule_id"] for h in reply.payload["trigger_hits"]} == {
        f"R-SPATIOTEMPORAL-{REGION}-debris_flow",
        "R-REPORT-LLM",
        "R-REPORT-RULE",
    }


def test_工厂把注入的解析器交到perceive手上() -> None:
    parser = FakeParser()
    perceive = next(a for a in build_real_agents(None, semantic=parser) if a.agent_type is AgentType.PERCEIVE)
    assert perceive.semantic is parser
    plain = next(a for a in build_real_agents(None) if a.agent_type is AgentType.PERCEIVE)
    assert plain.semantic is None, "默认不注入：没有解析器就走可见降级，不挂一个会谎报的替身"


def test_平台真解析器的签名与协议一致() -> None:
    """协议漂了就等于装配点递上去的东西不认——直接量 `DisasterTextParser.parse` 的形状。"""
    signature = inspect.signature(DisasterTextParser.parse)
    names = list(signature.parameters)
    assert names[:2] == ["self", "text"], "text 必须是位置参数：智能体就是这么传的"
    for keyword in ("region_code", "hazard_hint"):
        assert keyword in names[2:], f"{keyword} 得是关键字参数，否则委派会 TypeError"
        assert signature.parameters[keyword].kind is inspect.Parameter.KEYWORD_ONLY
    assert isinstance(FakeParser(), SemanticParser), "替身必须满足协议形状，否则这组用例测的不是同一条腿"
    assert hasattr(DisasterTextParser, "parse")
