"""触发条件规则引擎单元测试与边界测试（含时间窗、聚合、质量标记、站点隔离）。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import ClassVar

import pytest

from aegis.domain.enums import HazardType
from aegis.domain.messages import TelemetryReading
from aegis.services.trigger_rules import (
    AGGREGATIONS,
    Condition,
    Rule,
    RuleEngine,
    aggregate,
    default_rulebook,
    metric_value,
    windowed_series,
)

REF = datetime(2026, 9, 29, 12, 0, 0, tzinfo=UTC)


def series(*points: tuple[int, float]) -> list[tuple[datetime, float]]:
    """(相对 REF 的秒偏移, 数值) → 绝对时刻序列。"""
    return [(REF + timedelta(seconds=offset), value) for offset, value in points]


def reading(
    metric: str,
    value: float,
    *,
    offset: int = -60,
    station: str = "RG-01",
    region: str = "540121",
    quality: str = "ok",
    moment: datetime | None = None,
) -> TelemetryReading:
    ts = (moment or REF) + timedelta(seconds=offset)
    return TelemetryReading(
        station_id=station,
        metric=metric,
        value=value,
        unit="mm",
        region_code=region,
        observed_at=ts.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        ingested_at=ts.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        quality_flag=quality,
    )


class TestValidation:
    @pytest.mark.parametrize("op", ["=>", "!=", "", "≥"])
    def test_bad_operator(self, op: str) -> None:
        with pytest.raises(ValueError, match="比较算子"):
            Condition("rain_10min", op, 1.0)

    @pytest.mark.parametrize("agg", ["median", "", "MAX"])
    def test_bad_aggregation(self, agg: str) -> None:
        with pytest.raises(ValueError, match="聚合方式"):
            Condition("rain_10min", ">=", 1.0, agg)

    def test_bad_window(self) -> None:
        with pytest.raises(ValueError, match="window_seconds"):
            Condition("rain_10min", ">=", 1.0, "max", 0)
        with pytest.raises(ValueError, match="window_seconds"):
            Condition("rain_10min", ">=", 1.0, "max", -5)

    def test_rule_requires_conditions(self) -> None:
        with pytest.raises(ValueError, match="至少含一个条件"):
            Rule("R-X", HazardType.LANDSLIDE, "空规则", ())

    @pytest.mark.parametrize("mode", ["ALL", "either", ""])
    def test_rule_bad_mode(self, mode: str) -> None:
        with pytest.raises(ValueError, match="组合模式"):
            Rule("R-X", HazardType.LANDSLIDE, "d", (Condition("m", ">=", 1.0),), mode)

    @pytest.mark.parametrize("weight", [0.0, -1.0, 11.0])
    def test_rule_bad_weight(self, weight: float) -> None:
        with pytest.raises(ValueError, match="weight"):
            Rule("R-X", HazardType.LANDSLIDE, "d", (Condition("m", ">=", 1.0),), weight=weight)


class TestAggregation:
    def test_empty_returns_none(self) -> None:
        for agg in AGGREGATIONS:
            if agg == "rate":
                continue
            assert aggregate([], agg) is None

    def test_each_aggregation(self) -> None:
        values = [3.0, 1.0, 4.0, 2.0]
        assert aggregate(values, "last") == 2.0
        assert aggregate(values, "max") == 4.0
        assert aggregate(values, "sum") == 10.0
        assert aggregate(values, "avg") == 2.5
        assert aggregate(values, "count") == 4.0

    def test_unknown_aggregation_raises(self) -> None:
        with pytest.raises(ValueError):
            aggregate([1.0], "median")


class TestWindow:
    def test_excludes_future_and_too_old(self) -> None:
        data = series((-3600, 5.0), (-10, 7.0), (60, 9.0))
        windowed = windowed_series(data, 300, REF)
        assert [v for _t, v in windowed] == [7.0]

    def test_boundary_inclusive_at_window_edge(self) -> None:
        data = series((-600, 5.0))
        assert len(windowed_series(data, 600, REF)) == 1

    def test_sorts_out_of_order_input(self) -> None:
        data = series((-100, 2.0), (-300, 1.0))
        windowed = windowed_series(data, 600, REF)
        assert [v for _t, v in windowed] == [1.0, 2.0]

    def test_empty_window(self) -> None:
        assert windowed_series([], 60, REF) == []


class TestMetricValue:
    def test_missing_metric(self) -> None:
        assert metric_value([], Condition("rain_10min", ">=", 1.0), REF) is None

    def test_rate_requires_two_points(self) -> None:
        value = metric_value(series((-60, 3.0)), Condition("disp", ">=", 1.0, "rate", 3600), REF)
        assert value is None

    def test_rate_math_per_hour(self) -> None:
        value = metric_value(series((-3600, 10.0), (0, 40.0)), Condition("disp", ">=", 1.0, "rate", 7200), REF)
        assert value == pytest.approx(30.0)

    def test_rate_zero_elapsed_is_none(self) -> None:
        value = metric_value(series((0, 5.0), (0, 9.0)), Condition("disp", ">=", 1.0, "rate", 3600), REF)
        assert value is None

    @pytest.mark.parametrize(
        ("agg", "expected"),
        [("last", 4.0), ("max", 4.0), ("sum", 9.0), ("avg", 3.0), ("count", 3.0)],
    )
    def test_aggs_over_window(self, agg: str, expected: float) -> None:
        data = series((-100, 2.0), (-50, 3.0), (-10, 4.0))
        assert metric_value(data, Condition("m", ">=", 0.0, agg, 3600), REF) == pytest.approx(expected)


class TestEvaluate:
    def test_no_readings(self) -> None:
        engine = RuleEngine()
        result = engine.evaluate([])
        assert result.hits == []
        assert result.stations == 0

    def test_all_suspect_readings_are_ignored(self) -> None:
        engine = RuleEngine([Rule("R-T", HazardType.DEBRIS_FLOW, "t", (Condition("rain_10min", ">=", 1.0),))])
        result = engine.evaluate([reading("rain_10min", 99.0, quality="suspect")])
        assert result.hits == []

    def test_threshold_equality_hits(self) -> None:
        engine = RuleEngine([Rule("R-T", HazardType.DEBRIS_FLOW, "t", (Condition("rain_10min", ">=", 30.0, "max", 3600),))])
        assert len(engine.evaluate([reading("rain_10min", 30.0)]).hits) == 1

    def test_just_below_threshold_misses(self) -> None:
        engine = RuleEngine([Rule("R-T", HazardType.DEBRIS_FLOW, "t", (Condition("rain_10min", ">=", 30.0, "max", 3600),))])
        assert engine.evaluate([reading("rain_10min", 29.999)]).hits == []

    def test_mode_all_requires_every_condition(self) -> None:
        rule = Rule(
            "R-ALL",
            HazardType.LANDSLIDE,
            "双条件",
            (Condition("rain_cumulative_24h", ">=", 60.0, "max", 86_400), Condition("displacement_mm", ">=", 20.0, "max", 3600)),
            mode="all",
        )
        engine = RuleEngine([rule])
        assert engine.evaluate([reading("rain_cumulative_24h", 100.0)]).hits == []
        hits = engine.evaluate([reading("rain_cumulative_24h", 100.0), reading("displacement_mm", 25.0)]).hits
        assert len(hits) == 1
        assert hits[0].score == 1.0

    def test_mode_any_hits_on_single_condition(self) -> None:
        rule = Rule(
            "R-ANY",
            HazardType.AVALANCHE,
            "任一",
            (Condition("new_snow_cm", ">=", 25.0, "max", 3600), Condition("wind_speed_ms", ">=", 14.0, "max", 3600)),
            mode="any",
        )
        hits = RuleEngine([rule]).evaluate([reading("wind_speed_ms", 20.0)]).hits
        assert len(hits) == 1
        assert hits[0].score == pytest.approx(0.5)

    def test_stations_are_not_mixed(self) -> None:
        rule = Rule(
            "R-SPLIT",
            HazardType.DEBRIS_FLOW,
            "跨站点不得合并",
            (Condition("rain_10min", ">=", 30.0, "max", 3600), Condition("debris_level", ">=", 1.0, "max", 3600)),
        )
        engine = RuleEngine([rule])
        result = engine.evaluate(
            [
                reading("rain_10min", 50.0, station="A"),
                reading("debris_level", 2.0, station="B"),
            ]
        )
        assert result.hits == []

    def test_two_stations_each_hit(self) -> None:
        rule = Rule("R-P", HazardType.DEBRIS_FLOW, "p", (Condition("rain_10min", ">=", 30.0, "max", 3600),))
        result = RuleEngine([rule]).evaluate([reading("rain_10min", 40.0, station="A"), reading("rain_10min", 50.0, station="B")])
        assert len(result.hits) == 2
        assert {h.evidence_refs[0].split(":")[0] for h in result.hits} == {"A", "B"}

    def test_region_filter(self) -> None:
        rule = Rule("R-P", HazardType.DEBRIS_FLOW, "p", (Condition("rain_10min", ">=", 30.0, "max", 3600),))
        result = RuleEngine([rule]).evaluate(
            [reading("rain_10min", 40.0, station="A", region="540121"), reading("rain_10min", 60.0, station="B", region="540221")],
            region_code="540221",
        )
        assert len(result.hits) == 1
        assert result.hits[0].region_code == "540221"

    def test_out_of_window_reading_ignored(self) -> None:
        rule = Rule("R-W", HazardType.LAKE_OUTBURST, "w", (Condition("lake_level_m", ">=", 0.5, "max", 600),))
        engine = RuleEngine([rule])
        old = reading("lake_level_m", 5.0, offset=-3600)
        assert engine.evaluate([old], now=REF).hits == []
        fresh = reading("lake_level_m", 5.0, offset=-300)
        assert len(engine.evaluate([fresh], now=REF).hits) == 1

    def test_future_reading_ignored(self) -> None:
        rule = Rule("R-F", HazardType.DEBRIS_FLOW, "f", (Condition("rain_10min", ">=", 30.0, "max", 3600),))
        future = reading("rain_10min", 99.0, offset=+300)
        assert RuleEngine([rule]).evaluate([future], now=REF).hits == []

    def test_hit_carries_level_metadata(self) -> None:
        result = RuleEngine().evaluate(
            [
                reading("rain_10min", 45.0, station="RG-1"),
                reading("rain_cumulative_24h", 100.0, station="RG-1"),
                reading("debris_level", 1.5, station="RG-1"),
            ],
            now=REF,
        )
        rule_ids = {h.rule_id for h in result.hits}
        assert {"R-DEBRIS-RAIN-1", "R-DEBRIS-RAIN-2"} <= rule_ids
        assert all(h.hazard_type == "debris_flow" for h in result.hits)


class TestRulebook:
    REQUIRED_HAZARDS: ClassVar[set[HazardType]] = {
        HazardType.LANDSLIDE,
        HazardType.ROCKFALL,
        HazardType.DEBRIS_FLOW,
        HazardType.AVALANCHE,
        HazardType.LAKE_OUTBURST,
    }

    def test_covers_five_hazards(self) -> None:
        assert {r.hazard_type for r in default_rulebook()} >= self.REQUIRED_HAZARDS

    def test_rule_ids_unique(self) -> None:
        ids = [r.rule_id for r in default_rulebook()]
        assert len(ids) == len(set(ids))

    def test_lookups(self) -> None:
        engine = RuleEngine()
        assert engine.rule_by_id("R-LAKE-1").hazard_type is HazardType.LAKE_OUTBURST
        assert engine.rule_by_id("nope") is None
        assert len(engine.rules_for(HazardType.AVALANCHE)) == 2

    def test_evaluation_metadata(self) -> None:
        result = RuleEngine().evaluate([reading("rain_10min", 40.0, station="S1")], now=REF)
        assert result.evaluated == len(default_rulebook())
        assert result.stations == 1
        assert "rain_10min" in result.metrics_used
