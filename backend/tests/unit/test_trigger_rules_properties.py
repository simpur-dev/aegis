"""规则引擎性质测试：命中结果必须可溯源、可复现，且永不凭空造出输入里没有的站点/区域。

`RuleEngine` 是"≥5 类灾害、预警准确率 ≥80%"的判据来源，也是回放评测的分母。
随机读数比手写样例更能证明它没有把不相干的站点拼进证据里。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from aegis.domain.messages import TelemetryReading
from aegis.services.trigger_rules import RuleEngine, RuleEvaluation, default_rulebook

METRICS = ["rainfall_mm_h", "mud_level_cm", "soil_moisture_pct", "vibration_count", "displacement_mm"]
REGIONS = ["540121", "540122", "540127"]
T0 = datetime(2026, 9, 30, 3, 0, tzinfo=UTC)

QUALITY_FLAGS = ["ok", "suspect", "missing", "drift"]
quality_flags = st.sampled_from(QUALITY_FLAGS)


@st.composite
def readings(draw: st.DrawMethod) -> list[TelemetryReading]:
    count = draw(st.integers(min_value=0, max_value=14))
    out: list[TelemetryReading] = []
    for index in range(count):
        out.append(
            TelemetryReading(
                station_id=f"ST-{index % 4}",
                metric=draw(st.sampled_from(METRICS)),
                value=draw(st.floats(min_value=-50.0, max_value=400.0, allow_nan=False, allow_infinity=False)),
                unit="raw",
                region_code=draw(st.sampled_from(REGIONS)),
                observed_at=(T0 + timedelta(minutes=index)).isoformat(),
                ingested_at=(T0 + timedelta(minutes=index, seconds=20)).isoformat(),
                quality_flag=draw(quality_flags),
            )
        )
    return out


def names(result: RuleEvaluation) -> set[tuple[str, str]]:
    return {(hit.rule_id, hit.region_code) for hit in result.hits}


class TestEvaluationInvariants:
    def test_empty_input_evaluates_rules_but_hits_nothing(self) -> None:
        engine = RuleEngine()
        result = engine.evaluate([], now=T0)
        assert result.hits == []
        assert result.evaluated == len(default_rulebook())
        assert result.stations == 0

    @given(items=readings())
    @settings(max_examples=80, deadline=None)
    def test_every_hit_is_traceable_to_the_input(self, items: list[TelemetryReading]) -> None:
        """命中里的区域、证据必须是输入读数给过的，绝不能凭空出现一个没上报过的站点。"""
        result = RuleEngine().evaluate(items, now=T0)
        regions = {reading.region_code for reading in items}
        stations = {reading.station_id for reading in items}
        rule_ids = {rule.rule_id for rule in default_rulebook()}
        for hit in result.hits:
            assert hit.region_code in regions
            assert hit.rule_id in rule_ids
            assert hit.evidence_refs
            assert any(evidence.split(":", 1)[0] in stations for evidence in hit.evidence_refs)
            assert 0.0 <= hit.score <= 1.0

    @given(items=readings())
    @settings(max_examples=60, deadline=None)
    def test_only_ok_quality_readings_can_trigger(self, items: list[TelemetryReading]) -> None:
        """质量标志不是装饰：suspect/missing/drift 的读数一律不得参与定级。"""
        clean = [reading for reading in items if reading.quality_flag == "ok"]
        assert not clean or names(RuleEngine().evaluate(clean, now=T0)) == names(RuleEngine().evaluate(items, now=T0))

    @given(items=readings())
    @settings(max_examples=40, deadline=None)
    def test_evaluation_is_deterministic(self, items: list[TelemetryReading]) -> None:
        """同一批读数两次求值必须完全一致，否则回放准确率指标不可复现。"""
        engine = RuleEngine()
        first = engine.evaluate(items, now=T0).hits
        second = engine.evaluate(list(items), now=T0).hits
        assert [(h.rule_id, h.region_code, round(h.score, 9)) for h in first] == [
            (h.rule_id, h.region_code, round(h.score, 9)) for h in second
        ]

    @given(items=readings())
    @settings(max_examples=40, deadline=None)
    def test_region_filter_only_narrows(self, items: list[TelemetryReading]) -> None:
        """按区域过滤只能是子集：过滤后冒出别的区域就是过滤器失效。"""
        engine = RuleEngine()
        every = names(engine.evaluate(items, now=T0))
        for region in REGIONS:
            subset = names(engine.evaluate(items, now=T0, region_code=region))
            assert {pair for pair in subset if pair[1] == region} == subset
            assert subset <= every

    @given(items=readings(), shift_minutes=st.integers(min_value=1, max_value=720))
    @settings(max_examples=40, deadline=None)
    def test_time_translation_with_explicit_now_is_stable(self, items: list[TelemetryReading], shift_minutes: int) -> None:
        """窗口是相对量：整体平移读数与判定时刻，命中集合不应改变。

        只在有读数时断言——空输入下 `now` 完全不参与计算，平移是恒等的。
        """
        engine = RuleEngine()
        base = names(engine.evaluate(items, now=T0))
        if not items:
            assert base == set()
            return
        delta = timedelta(minutes=shift_minutes)
        moved = [
            reading.model_copy(
                update={
                    "observed_at": (_as_dt(reading.observed_at) + delta).isoformat(),
                    "ingested_at": (_as_dt(reading.ingested_at) + delta).isoformat(),
                }
            )
            for reading in items
        ]
        assert names(engine.evaluate(moved, now=T0 + delta)) == base


def _as_dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


class TestRulebookShape:
    """规则本自身的边界：这些是"≥5 类灾害"与可解释性的前提。"""

    def test_rulebook_covers_the_required_hazard_count(self) -> None:
        hazards = {rule.hazard_type.value for rule in default_rulebook()}
        assert len(hazards) >= 5, hazards

    def test_rule_ids_are_unique(self) -> None:
        ids = [rule.rule_id for rule in default_rulebook()]
        assert len(ids) == len(set(ids))

    def test_engine_exposes_the_same_rules_it_evaluates(self) -> None:
        engine = RuleEngine()
        assert [r.rule_id for r in engine.rules] == [r.rule_id for r in default_rulebook()]
        assert engine.rule_by_id(engine.rules[0].rule_id) is not None
        assert engine.rule_by_id("不存在") is None

    @pytest.mark.parametrize("hazard", sorted({rule.hazard_type.value for rule in default_rulebook()}))
    def test_rules_for_hazard_are_a_subset(self, hazard: str) -> None:
        from aegis.domain.enums import HazardType

        engine = RuleEngine()
        selected = engine.rules_for(HazardType(hazard))
        assert selected, hazard
        assert all(rule.hazard_type.value == hazard for rule in selected)
        assert len(selected) <= len(engine.rules)
