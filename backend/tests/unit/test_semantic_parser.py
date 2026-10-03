"""三路融合解析的单测（批次 C1 的判据面）。

写这些用例的出发点是三个真会出事的点：
1. 规则腿必须是唯一判据——阈值只有一份，解析器不许自带第二套等级口径；
2. 申报值（"请求红色预警"）与测量值（泥位 1.2 米）必须可区分，且前者一律转人工核签；
3. LLM 的越界输出（非法灾种、等级 9、异常）必须被弃用并留痕，而不是把链路带崩。
"""

from __future__ import annotations

import pytest

from aegis.domain.enums import HazardType, RiskLevel
from aegis.services.semantic_parser import (
    CEILING_WITHOUT_RULE,
    LEG_LLM,
    LEG_RETRIEVAL,
    LEG_RULE,
    LEG_RULE_DECLARED,
    DisasterTextParser,
    EvidenceNote,
    detect_hazard,
    detect_level,
    extract_metrics,
    extract_region,
)

HEAVY_RAIN_REPORT = "区划540121：24小时累计降雨95毫米，沟道泥位抬升1.2米，下游300人受威胁"


class FakeEvidence:
    def __init__(self, notes: list[EvidenceNote] | None = None, *, error: Exception | None = None) -> None:
        self.notes = notes or []
        self.error = error
        self.calls: list[dict[str, object]] = []

    async def corroborate(self, *, query: str, hazard_type: str | None, region_code: str | None, limit: int) -> list[EvidenceNote]:
        self.calls.append({"query": query, "hazard_type": hazard_type, "region_code": region_code, "limit": limit})
        if self.error is not None:
            raise self.error
        return list(self.notes)


class FakeLlm:
    def __init__(self, payload: dict[str, object] | None = None, *, error: Exception | None = None, available: bool = True) -> None:
        self.payload = payload or {}
        self.error = error
        self.available = available
        self.calls = 0

    async def chat_json(self, messages: list[dict[str, str]], *, temperature: float = 0.1) -> dict[str, object]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return dict(self.payload)


# ---------- 词表与数字绑定（确定性口径，先钉死） ----------


def test_数字绑到最近的关键词而不是窗口里的全部关键词() -> None:
    """回归：只看窗口会把"位移 30 mm"也记成裂缝宽度，于是裂缝宽度虚高。"""
    metrics = {metric: value for metric, value, _unit in extract_metrics("裂缝宽度 25 毫米，位移 30 mm 持续增大")}
    assert metrics["crack_aperture_mm"] == 25.0
    assert metrics["displacement_mm"] == 30.0


def test_同一指标取最不利读数() -> None:
    metrics = {metric: value for metric, value, _unit in extract_metrics("10分钟降雨 12 毫米，随后短时强降雨 46 毫米")}
    assert metrics["rain_10min"] == 46.0


def test_没有关键词上下文的数字不产出读数() -> None:
    assert extract_metrics("撤离 300 人，海拔 3800 米") == []


def test_灾种取最早出现的线索且显式hint优先() -> None:
    # "最早出现"是稳定口径：先说现象再说背景，历史雪崩描述不该盖过本次泥石流
    assert detect_hazard("历史上曾发生雪崩，本次是泥石流") is HazardType.AVALANCHE
    assert detect_hazard("现场发现泥石流痕迹") is HazardType.DEBRIS_FLOW
    assert detect_hazard("现场发现泥石流痕迹", hint="landslide") is HazardType.LANDSLIDE
    assert detect_hazard("现场发现泥石流痕迹", hint="不存在的灾种") is HazardType.DEBRIS_FLOW


def test_等级词只认颜色与罗马阿拉伯数字() -> None:
    assert detect_level("发布橙色预警") is RiskLevel.ORANGE
    assert detect_level("三级响应") is RiskLevel.YELLOW
    assert detect_level("没有任何等级字样") is None


def test_区划码要带上下文不被海拔数字冒充() -> None:
    assert extract_region("区划540121 出现险情") == "540121"
    assert extract_region("540121") == "540121"
    assert extract_region("海拔 540121 米处发现裂缝") is None
    assert extract_region("没有区划码", default="540200") == "540200"


# ---------- 规则腿 ----------


@pytest.mark.asyncio
async def test_阈值命中由规则腿定级并留下规则证据() -> None:
    parsed = await DisasterTextParser().parse(HEAVY_RAIN_REPORT)
    assert parsed.decided_by == LEG_RULE
    assert parsed.risk_level is RiskLevel.RED
    assert [hit.rule_id for hit in parsed.hits] == ["R-DEBRIS-RAIN-2"]
    assert parsed.needs_review is False
    assert parsed.measured_by_rule is True
    assert parsed.reported_people == 300


@pytest.mark.asyncio
async def test_缺区划码时规则腿不判定() -> None:
    parsed = await DisasterTextParser().parse("24小时累计降雨95毫米，沟道泥位抬升1.2米")
    assert parsed.risk_level is None
    assert parsed.decided_by == "none"
    assert any("区划码" in finding.rationale for finding in parsed.legs if finding.leg == LEG_RULE)


@pytest.mark.asyncio
async def test_申报等级不算测量值且转人工核签() -> None:
    parsed = await DisasterTextParser().parse("540200 发生泥石流，请求红色预警")
    assert parsed.decided_by == LEG_RULE_DECLARED
    assert parsed.risk_level is RiskLevel.RED
    assert parsed.confidence <= CEILING_WITHOUT_RULE
    assert parsed.needs_review is True
    assert any("申报值" in item for item in parsed.degradations)


@pytest.mark.asyncio
async def test_够不上阈值又没有申报等级时不给等级() -> None:
    parsed = await DisasterTextParser().parse("540300 坡面有裂缝，暂无其它数据")
    assert parsed.risk_level is None
    assert parsed.needs_review is True


# ---------- 佐证腿 ----------


@pytest.mark.asyncio
async def test_佐证腿不单独抬等级只加出处() -> None:
    evidence = FakeEvidence([EvidenceNote(source="case:c1", hazard_type="debris_flow", typical_level=1, refs=("c1",))])
    parsed = await DisasterTextParser(evidence=evidence).parse("540121 沟道泥位抬升 0.2 米")
    assert evidence.calls, "佐证腿没被调用，这条用例在空转"
    # 未达阈值 → 等级来自案例典型等级，但仍属"未测量"，置信压在天花板下
    assert parsed.decided_by == LEG_RETRIEVAL
    assert parsed.confidence <= CEILING_WITHOUT_RULE
    assert parsed.needs_review is True


@pytest.mark.asyncio
async def test_规则命中时佐证只加置信不改等级() -> None:
    evidence = FakeEvidence([EvidenceNote(source="case:c1", hazard_type="debris_flow", typical_level=4, refs=("c1",))])
    parsed = await DisasterTextParser(evidence=evidence).parse(HEAVY_RAIN_REPORT)
    assert parsed.decided_by == LEG_RULE
    assert parsed.risk_level is RiskLevel.RED  # 案例说四级也不能把红色压低


@pytest.mark.asyncio
async def test_佐证异常只留降级痕迹() -> None:
    evidence = FakeEvidence(error=RuntimeError("图谱连不上"))
    parsed = await DisasterTextParser(evidence=evidence).parse(HEAVY_RAIN_REPORT)
    assert parsed.decided_by == LEG_RULE
    retrieval_leg = next(leg for leg in parsed.legs if leg.leg == LEG_RETRIEVAL)
    assert "佐证降级" in retrieval_leg.rationale


# ---------- LLM 腿 ----------


@pytest.mark.asyncio
async def test_llm与规则冲突时按规则取并留下分歧() -> None:
    llm = FakeLlm({"hazard_type": "avalanche", "risk_level": 3, "confidence": 0.9, "reason": "误读"})
    parsed = await DisasterTextParser(llm=llm).parse(HEAVY_RAIN_REPORT)
    assert parsed.risk_level is RiskLevel.RED
    assert parsed.hazard_type is HazardType.DEBRIS_FLOW
    assert any("灾种分歧" in item for item in parsed.conflicts)
    assert any("等级分歧" in item for item in parsed.conflicts)
    assert llm.calls == 1


@pytest.mark.asyncio
async def test_llm非法输出逐字段弃用() -> None:
    llm = FakeLlm({"hazard_type": "flood", "risk_level": 9, "confidence": 3.5})
    parsed = await DisasterTextParser(llm=llm).parse("540121 坡面有裂缝")
    leg = next(leg for leg in parsed.legs if leg.leg == LEG_LLM)
    assert leg.hazard_type is None
    assert leg.risk_level is None
    assert leg.confidence == 1.0  # 置信被夹回 [0,1]


@pytest.mark.asyncio
async def test_llm抛错时裁决腿降级而解析照旧() -> None:
    llm = FakeLlm(error=RuntimeError("超时"))
    parsed = await DisasterTextParser(llm=llm).parse(HEAVY_RAIN_REPORT)
    assert parsed.decided_by == LEG_RULE
    assert "LLM 裁决降级" in next(leg for leg in parsed.legs if leg.leg == LEG_LLM).rationale


@pytest.mark.asyncio
async def test_无llm时裁决腿如实写未配置() -> None:
    parsed = await DisasterTextParser(llm=FakeLlm(available=False)).parse(HEAVY_RAIN_REPORT)
    assert "未配置" in next(leg for leg in parsed.legs if leg.leg == LEG_LLM).rationale


@pytest.mark.asyncio
async def test_llm建议可以在两腿都空白时抬等级但必须核签() -> None:
    llm = FakeLlm({"hazard_type": "landslide", "risk_level": 2, "confidence": 0.9})
    parsed = await DisasterTextParser(llm=llm).parse("540400 坡体出现持续变形，疑似滑动")
    assert parsed.decided_by == LEG_LLM
    assert parsed.risk_level is RiskLevel.ORANGE
    assert parsed.needs_review is True
    assert parsed.confidence <= CEILING_WITHOUT_RULE


# ---------- 进链路的形状 ----------


@pytest.mark.asyncio
async def test_解析结果可转为链路结论() -> None:
    parsed = await DisasterTextParser().parse(HEAVY_RAIN_REPORT)
    verdict = parsed.to_verdict()
    assert verdict is not None
    assert verdict.assessed_by == "platform.semantic_parser"
    assert verdict.region_code == "540121"
    assert verdict.hits and verdict.hits[0].rule_id == "R-DEBRIS-RAIN-2"


@pytest.mark.asyncio
async def test_三腿原始结论全部留痕可回放() -> None:
    parsed = await DisasterTextParser(llm=FakeLlm({"hazard_type": "debris_flow", "risk_level": 1, "confidence": 0.8})).parse(
        HEAVY_RAIN_REPORT
    )
    legs = {leg.leg for leg in parsed.legs}
    assert legs == {LEG_RULE, LEG_RULE_DECLARED, LEG_RETRIEVAL, LEG_LLM}
    payload = parsed.as_dict()
    assert {row["leg"] for row in payload["legs"]} == legs
    assert payload["trigger_hits"][0]["rule_id"] == "R-DEBRIS-RAIN-2"


@pytest.mark.asyncio
async def test_空文本直接报错而不是给个假结论() -> None:
    with pytest.raises(ValueError):
        await DisasterTextParser().parse("   ")
