"""风险定级引擎测试：等级选择、置信度、区域分组、未知规则兜底。"""

from __future__ import annotations

import pytest

from aegis.domain.enums import HazardType, RiskLevel
from aegis.domain.messages import TriggerHit
from aegis.services.risk_engine import RiskEngine


def hit(rule_id: str, hazard: str = "debris_flow", region: str = "540121", score: float = 1.0) -> TriggerHit:
    return TriggerHit(rule_id=rule_id, hazard_type=hazard, region_code=region, score=score)


@pytest.fixture
def engine() -> RiskEngine:
    return RiskEngine()


class TestBasics:
    def test_no_hits_returns_none(self, engine: RiskEngine) -> None:
        assert engine.assess([]) is None

    def test_single_hit_level_and_confidence(self, engine: RiskEngine) -> None:
        verdict = engine.assess([hit("R-DEBRIS-RAIN-1")])
        assert verdict.risk_level is RiskLevel.ORANGE
        assert verdict.hazard_type is HazardType.DEBRIS_FLOW
        assert 0.0 <= verdict.confidence <= 0.99
        assert "R-DEBRIS-RAIN-1" in verdict.rationale

    def test_red_wins_over_orange(self, engine: RiskEngine) -> None:
        verdict = engine.assess([hit("R-DEBRIS-RAIN-1"), hit("R-DEBRIS-RAIN-2")])
        assert verdict.risk_level is RiskLevel.RED

    def test_corroboration_raises_confidence(self, engine: RiskEngine) -> None:
        single = engine.assess([hit("R-DEBRIS-RAIN-1")])
        double = engine.assess([hit("R-DEBRIS-RAIN-1"), hit("R-DEBRIS-RAIN-2")])
        assert double.confidence >= single.confidence

    def test_confidence_capped(self, engine: RiskEngine) -> None:
        hits = [hit("R-DEBRIS-RAIN-1"), hit("R-DEBRIS-RAIN-2"), hit("R-LAKE-1"), hit("R-LAKE-2")]
        verdict = engine.assess(hits)
        assert verdict.confidence <= 0.99


class TestTieBreaksAndFallbacks:
    def test_unknown_rule_defaults_to_yellow(self, engine: RiskEngine) -> None:
        verdict = engine.assess([hit("R-DOES-NOT-EXIST", hazard="rockfall")])
        assert verdict.risk_level is RiskLevel.YELLOW
        assert verdict.hazard_type is HazardType.ROCKFALL

    def test_tie_break_prefers_higher_weighted_score(self, engine: RiskEngine) -> None:
        # 两条同为 ORANGE：R-AVALANCHE-1 weight=1.0，R-ROCKFALL-1 weight=1.0，用 score 区分
        verdict = engine.assess([hit("R-AVALANCHE-1", hazard="avalanche", score=0.4), hit("R-ROCKFALL-1", hazard="rockfall", score=1.0)])
        assert verdict.hazard_type is HazardType.ROCKFALL

    def test_low_score_hit_still_levels(self, engine: RiskEngine) -> None:
        verdict = engine.assess([hit("R-LAKE-1", hazard="lake_outburst", score=0.2)])
        assert verdict.risk_level is RiskLevel.ORANGE
        assert verdict.confidence < 0.5

    def test_region_scoping(self, engine: RiskEngine) -> None:
        verdict = engine.assess(
            [hit("R-DEBRIS-RAIN-1", region="540121"), hit("R-LAKE-1", hazard="lake_outburst", region="540221")],
            region_code="540221",
        )
        assert verdict.hazard_type is HazardType.LAKE_OUTBURST
        assert {h.region_code for h in verdict.hits} == {"540221"}

    def test_region_filter_falls_back_to_all_when_empty(self, engine: RiskEngine) -> None:
        verdict = engine.assess([hit("R-DEBRIS-RAIN-1", region="540121")], region_code="999999")
        assert verdict is not None
        assert verdict.region_code == "999999"


class TestAggregation:
    def test_assess_many_splits_by_region(self, engine: RiskEngine) -> None:
        verdicts = engine.assess_many(
            [
                hit("R-DEBRIS-RAIN-1", region="540121"),
                hit("R-LAKE-1", hazard="lake_outburst", region="540221"),
                hit("R-AVALANCHE-1", hazard="avalanche", region="540321"),
            ]
        )
        assert {v.region_code for v in verdicts} == {"540121", "540221", "540321"}

    def test_payload_shape(self, engine: RiskEngine) -> None:
        payload = engine.assess([hit("R-DEBRIS-RAIN-1")]).as_payload()
        assert payload["hazard_type"] == "debris_flow"
        assert isinstance(payload["risk_level"], int)
        assert payload["assessed_by"] == "platform.risk_engine"
        assert "evidence_refs" in payload


class TestMapping:
    def test_level_lookup(self, engine: RiskEngine) -> None:
        assert engine.level_of("R-DEBRIS-RAIN-2") is RiskLevel.RED
        assert engine.level_of("R-NOPE") is None

    def test_rules_exposed(self, engine: RiskEngine) -> None:
        assert len(engine.rules()) >= 9
