"""分钟物化纯函数与事实行的边界测试：口径唯一真源必须可复现、可乱序、不受脏值污染。

`materialize_minutes` 同时是 ClickHouse 服务端物化视图与 DuckDB 边缘 rollup 的对账基准，
因此这里守的不是实现细节而是**口径**：桶归属只看 observed_at、重复 event_id 只计一次、
缺测参与计数但不参与数值聚合、空窗产空表而非 0。
"""

from __future__ import annotations

import math
import random
from datetime import UTC, datetime, timedelta, timezone

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from aegis.analytics.port import (
    BUCKET_SECONDS,
    FACT_COLUMNS,
    NOT_MEASURED,
    AnalyticsSchemaError,
    FactRow,
    as_utc,
    bucket_minute,
    dedupe_rows,
    materialize_minutes,
    metrics_from_facts,
    percentile,
)
from aegis.domain.enums import RiskLevel
from aegis.domain.messages import TelemetryReading, WarningRecord

TIBET = timezone(timedelta(hours=8))


def row(
    *,
    event_id: str,
    kind: str = "telemetry",
    observed_at: datetime,
    region_code: str = "540121",
    value: float | None = None,
    risk_level: int | None = None,
    latency_ms: float | None = None,
) -> FactRow:
    return FactRow(
        event_id=event_id,
        kind=kind,  # type: ignore[arg-type]
        observed_at=observed_at,
        region_code=region_code,
        value=value,
        risk_level=risk_level,
        latency_ms=latency_ms,
    )


BASE = datetime(2026, 9, 30, 3, 0, 0, tzinfo=UTC)


# ------------------------------------------------------------------ 事实行归一


class TestFactRowNormalization:
    def test_unknown_kind_rejected(self) -> None:
        with pytest.raises(AnalyticsSchemaError, match="未知事实类型"):
            row(event_id="e1", kind="flood", observed_at=BASE)  # type: ignore[arg-type]

    def test_naive_and_offset_timestamps_both_land_on_utc_instant(self) -> None:
        naive = FactRow(event_id="n", kind="telemetry", observed_at=datetime(2026, 9, 30, 3, 0), region_code="540121")
        offset = FactRow(event_id="o", kind="telemetry", observed_at=datetime(2026, 9, 30, 11, 0, tzinfo=TIBET), region_code="540121")
        assert naive.observed_at == offset.observed_at == BASE
        assert naive.observed_at.tzinfo == UTC

    def test_ingested_at_defaults_to_observed_at(self) -> None:
        single = row(event_id="e", observed_at=BASE, value=1.0)
        assert single.ingested_at == BASE

    @pytest.mark.parametrize("dirty", [float("nan"), float("inf"), float("-inf")])
    def test_non_finite_measurements_become_none(self, dirty: float) -> None:
        assert row(event_id="e", observed_at=BASE, value=dirty).value is None
        assert row(event_id="e", observed_at=BASE, kind="latency", latency_ms=dirty).latency_ms is None

    def test_bool_is_not_a_measurement(self) -> None:
        """True/False 不是读数：混进来的布尔必须落 None，否则分钟里会多出 1.0 的幽灵峰值。"""
        assert row(event_id="e", observed_at=BASE, value=True).value is None
        assert row(event_id="e", observed_at=BASE, risk_level=True).risk_level is None

    def test_measure_dispatches_by_kind(self) -> None:
        assert row(event_id="a", observed_at=BASE, value=12.5).measure() == 12.5
        assert row(event_id="b", kind="latency", observed_at=BASE, latency_ms=7.0).measure() == 7.0
        assert row(event_id="c", kind="hazard", observed_at=BASE, risk_level=3).measure() == 3.0
        assert row(event_id="d", kind="hazard", observed_at=BASE).measure() is None

    def test_as_tuple_matches_column_contract(self) -> None:
        fact = row(event_id="e", observed_at=BASE, value=1.0)
        assert len(fact.as_tuple()) == len(FACT_COLUMNS)
        assert fact.as_tuple()[0] == "e"
        assert fact.as_tuple()[-2:] == (BASE, BASE)

    def test_is_spatial_needs_both_coordinates(self) -> None:
        assert FactRow("e", "telemetry", BASE, "540121", lat=29.6, lon=91.1).is_spatial() is True
        assert FactRow("e", "telemetry", BASE, "540121", lat=29.6).is_spatial() is False
        assert FactRow("e", "telemetry", BASE, "540121", lon=91.1).is_spatial() is False


# ------------------------------------------------------------------ 领域对象映射


class TestDomainMapping:
    def test_from_reading_is_idempotent_keyed(self) -> None:
        reading = TelemetryReading(
            station_id="ST-540121-01",
            metric="rainfall_mm_h",
            value=18.2,
            unit="mm/h",
            region_code="540121",
            observed_at="2026-09-30T03:00:00+00:00",
            ingested_at="2026-09-30T03:00:40+00:00",
        )
        first = FactRow.from_reading(reading, hazard_type="rainstorm", lat=29.65, lon=91.12)
        second = FactRow.from_reading(reading)
        assert first.event_id == second.event_id
        assert first.kind == "telemetry"
        assert first.hazard_type == "rainstorm"
        assert first.value == pytest.approx(18.2)
        assert (first.ingested_at - first.observed_at) == timedelta(seconds=40)

    def test_from_reading_distinguishes_milliseconds(self) -> None:
        base = {"station_id": "ST-1", "metric": "mud_level_cm", "value": 1.0, "unit": "cm", "region_code": "540121"}
        early = TelemetryReading(**base, observed_at="2026-09-30T03:00:00.000+00:00")  # type: ignore[arg-type]
        late = TelemetryReading(**base, observed_at="2026-09-30T03:00:00.001+00:00")  # type: ignore[arg-type]
        assert FactRow.from_reading(early).event_id != FactRow.from_reading(late).event_id

    def test_from_warning_emits_one_fact_per_affected_region(self) -> None:
        record = WarningRecord(
            event_id="evt_1",
            trace_id="trc_0000000000000001",
            hazard_type="debris_flow",
            region_codes=["540121", "540122", "540123"],
            risk_level=RiskLevel.ORANGE,
            title_zh="泥石流预警",
            body_zh="正文",
            generated_at="2026-09-30T03:00:00+00:00",
        )
        facts = list(FactRow.from_warning(record))
        assert [f.region_code for f in facts] == ["540121", "540122", "540123"]
        assert {f.kind for f in facts} == {"hazard"}
        assert len({f.event_id for f in facts}) == 3
        assert facts[0].risk_level == int(RiskLevel.ORANGE)
        assert facts[0].trace_id == "trc_0000000000000001"

    def test_from_latency_reuses_existing_metric_names(self) -> None:
        fact = FactRow.from_latency(metric="sync_agent_to_gateway_ms", latency_ms=2100.5, observed_at=BASE, trace_id="trc_a")
        assert fact.kind == "latency"
        assert fact.unit == "ms"
        assert fact.measure() == pytest.approx(2100.5)
        assert fact.event_id.startswith("sync_agent_to_gateway_ms:")


# ------------------------------------------------------------------ 时间与分位数


class TestTimeHelpers:
    def test_as_utc_keeps_instant(self) -> None:
        moment = datetime(2026, 9, 30, 11, 0, tzinfo=TIBET)
        assert as_utc(moment) == BASE

    @pytest.mark.parametrize(
        ("moment", "expected"),
        [
            (datetime(2026, 9, 30, 3, 0, 0, tzinfo=UTC), datetime(2026, 9, 30, 3, 0, tzinfo=UTC)),
            (datetime(2026, 9, 30, 3, 0, 59, 999000, tzinfo=UTC), datetime(2026, 9, 30, 3, 0, tzinfo=UTC)),
            (datetime(2026, 9, 30, 3, 1, 0, tzinfo=UTC), datetime(2026, 9, 30, 3, 1, tzinfo=UTC)),
        ],
    )
    def test_minute_bucket_boundaries(self, moment: datetime, expected: datetime) -> None:
        assert bucket_minute(moment) == expected

    def test_bucket_is_instant_based_across_timezones(self) -> None:
        """同一瞬时的不同偏移必须落同一桶，否则区域统计会因客户端时区而裂开。"""
        assert bucket_minute(datetime(2026, 9, 30, 11, 0, 30, tzinfo=TIBET)) == bucket_minute(datetime(2026, 9, 30, 3, 0, 30, tzinfo=UTC))

    def test_sub_minute_buckets_are_supported(self) -> None:
        moment = datetime(2026, 9, 30, 3, 0, 37, tzinfo=UTC)
        assert bucket_minute(moment, bucket_seconds=15) == datetime(2026, 9, 30, 3, 0, 30, tzinfo=UTC)

    @pytest.mark.parametrize("bad", [0, -1])
    def test_non_positive_bucket_rejected(self, bad: int) -> None:
        with pytest.raises(AnalyticsSchemaError):
            bucket_minute(BASE, bucket_seconds=bad)

    def test_percentile_edges(self) -> None:
        assert percentile([], 95) == 0.0
        assert percentile([4.0], 0) == 4.0
        assert percentile([1.0, 2.0], 0) == 1.0
        assert percentile([1.0, 2.0], 100) == 2.0
        assert percentile([1.0, 2.0], 50) == pytest.approx(1.5)
        assert percentile([0.0, 10.0], 95) == pytest.approx(9.5)

    def test_percentile_is_input_order_independent(self) -> None:
        assert percentile([9.0, 1.0, 5.0], 50) == percentile([1.0, 5.0, 9.0], 50) == 5.0


# ------------------------------------------------------------------ 去重


class TestDedupe:
    def test_first_occurrence_wins(self) -> None:
        rows = [
            row(event_id="a", observed_at=BASE, value=1.0),
            row(event_id="a", observed_at=BASE, value=99.0),
            row(event_id="b", observed_at=BASE, value=2.0),
        ]
        kept = list(dedupe_rows(rows))
        assert [f.event_id for f in kept] == ["a", "b"]
        assert kept[0].value == 1.0

    def test_blank_event_ids_are_never_deduped(self) -> None:
        """空 event_id 表示"无幂等键"，把两条独立读数当重复丢掉是数据事故。"""
        rows = [row(event_id="", observed_at=BASE, value=1.0), row(event_id="", observed_at=BASE, value=2.0)]
        assert len(list(dedupe_rows(rows))) == 2


# ------------------------------------------------------------------ 分钟物化口径


class TestMaterializeMinutes:
    def test_empty_input_produces_no_rows(self) -> None:
        """空窗不零填充：把"没数据"读成 0 会让误报率统计说谎。"""
        assert materialize_minutes([]) == []

    def test_single_bucket_aggregates(self) -> None:
        rows = [row(event_id=f"e{i}", observed_at=BASE + timedelta(seconds=i), value=float(i)) for i in range(4)]
        facts = materialize_minutes(rows)
        assert len(facts) == 1
        fact = facts[0]
        assert (fact.minute, fact.region_code, fact.kind) == (BASE, "540121", "telemetry")
        assert (fact.count, fact.measured) == (4, 4)
        assert fact.total == pytest.approx(6.0)
        assert (fact.peak, fact.floor) == (3.0, 0.0)
        assert fact.mean == pytest.approx(1.5)
        assert fact.p95 == pytest.approx(2.85)
        assert fact.skipped_missing == 0

    def test_out_of_order_input_lands_in_correct_buckets(self) -> None:
        late = row(event_id="late", observed_at=BASE, value=1.0)
        early = row(event_id="early", observed_at=BASE + timedelta(minutes=2), value=3.0)
        facts = materialize_minutes([early, late])
        assert [f.minute for f in facts] == [BASE, BASE + timedelta(minutes=2)]

    def test_duplicate_event_ids_counted_once(self) -> None:
        rows = [row(event_id="dup", observed_at=BASE, value=5.0)] * 3
        facts = materialize_minutes(rows)
        assert len(facts) == 1
        assert facts[0].count == 1
        assert facts[0].total == pytest.approx(5.0)

    def test_missing_values_count_but_do_not_aggregate(self) -> None:
        rows = [
            row(event_id="a", observed_at=BASE, value=10.0),
            row(event_id="b", observed_at=BASE, value=None),
            row(event_id="c", observed_at=BASE, value=float("nan")),
        ]
        (fact,) = materialize_minutes(rows)
        assert fact.count == 3
        assert fact.skipped_missing == 2
        assert fact.measured == 1
        assert (fact.total, fact.peak, fact.floor, fact.mean, fact.p95) == (10.0, 10.0, 10.0, 10.0, 10.0)

    def test_all_missing_bucket_reports_zero_without_fabricating_data(self) -> None:
        rows = [row(event_id="a", observed_at=BASE, value=None), row(event_id="b", observed_at=BASE, value=None)]
        (fact,) = materialize_minutes(rows)
        assert fact.count == 2
        assert fact.measured == 0
        assert (fact.total, fact.peak, fact.floor, fact.mean, fact.p95) == (0.0, 0.0, 0.0, 0.0, 0.0)

    def test_kinds_are_not_mixed_within_a_bucket(self) -> None:
        rows = [
            row(event_id="t", observed_at=BASE, value=2.0),
            row(event_id="h", kind="hazard", observed_at=BASE, risk_level=4),
            row(event_id="l", kind="latency", observed_at=BASE, latency_ms=30.0),
        ]
        facts = materialize_minutes(rows)
        assert [f.kind for f in facts] == ["hazard", "latency", "telemetry"]
        assert {f.count for f in facts} == {1}
        assert next(f for f in facts if f.kind == "hazard").worst_risk_level == 4

    def test_worst_risk_level_is_the_most_dangerous_not_the_largest(self) -> None:
        """RiskLevel 是倒序枚举（1=红最危险）：物化必须取数值最小者，写成 max() 会把红色报成无风险。"""
        rows = [
            row(event_id="h1", kind="hazard", observed_at=BASE, risk_level=int(RiskLevel.ORANGE)),
            row(event_id="h2", kind="hazard", observed_at=BASE + timedelta(seconds=30), risk_level=int(RiskLevel.NONE)),
            row(event_id="h3", kind="hazard", observed_at=BASE + timedelta(seconds=45), risk_level=int(RiskLevel.YELLOW)),
        ]
        (fact,) = materialize_minutes(rows)
        assert fact.worst_risk_level == int(RiskLevel.ORANGE)
        assert fact.total == pytest.approx(2.0 + 5.0 + 3.0)

    def test_worst_risk_level_ignores_red_hazard_never_beating_orange(self) -> None:
        rows = [
            row(event_id="h1", kind="hazard", observed_at=BASE, risk_level=int(RiskLevel.BLUE)),
            row(event_id="h2", kind="hazard", observed_at=BASE, risk_level=int(RiskLevel.RED)),
        ]
        (fact,) = materialize_minutes(rows)
        assert fact.worst_risk_level == int(RiskLevel.RED) == 1

    def test_regions_are_split(self) -> None:
        rows = [
            row(event_id="a", observed_at=BASE, region_code="540121", value=1.0),
            row(event_id="b", observed_at=BASE, region_code="540122", value=2.0),
        ]
        facts = materialize_minutes(rows)
        assert [f.region_code for f in facts] == ["540121", "540122"]

    def test_output_is_stably_sorted_by_key(self) -> None:
        rows = [
            row(event_id="x", observed_at=BASE + timedelta(minutes=1), region_code="540129", value=1.0),
            row(event_id="y", observed_at=BASE, region_code="540122", value=1.0),
            row(event_id="z", observed_at=BASE, region_code="540121", value=1.0),
        ]
        facts = materialize_minutes(rows)
        assert facts == sorted(facts, key=lambda f: f.key)

    def test_custom_bucket_size(self) -> None:
        rows = [
            row(event_id="a", observed_at=BASE, value=1.0),
            row(event_id="b", observed_at=BASE + timedelta(seconds=20), value=2.0),
        ]
        facts = materialize_minutes(rows, bucket_seconds=15)
        assert [f.count for f in facts] == [1, 1]

    def test_float_summation_is_stable(self) -> None:
        """0.1×10 的经典坑：用 fsum 保证与 SQL 侧 sum 对账时不误报差异。"""
        rows = [row(event_id=f"e{i}", observed_at=BASE + timedelta(seconds=i), value=0.1) for i in range(10)]
        (fact,) = materialize_minutes(rows)
        assert fact.total == pytest.approx(1.0, abs=1e-15)

    def test_non_finite_values_do_not_poison_peak(self) -> None:
        rows = [
            row(event_id="a", observed_at=BASE, value=1.0),
            row(event_id="b", observed_at=BASE, value=float("inf")),
        ]
        (fact,) = materialize_minutes(rows)
        assert math.isfinite(fact.peak)
        assert fact.peak == 1.0


class TestMaterializeProperties:
    """口径性质：乱序不变 + 幂等。这是跨端对账（Python/ClickHouse/DuckDB）的前提。"""

    @given(
        samples=st.lists(
            st.tuples(
                st.integers(min_value=0, max_value=7200),
                st.floats(min_value=-1e6, max_value=1e6, allow_nan=False, allow_infinity=False),
            ),
            min_size=1,
            max_size=40,
        ),
        seed=st.integers(min_value=0, max_value=10_000),
    )
    @settings(max_examples=80, deadline=None)
    def test_permutation_invariant(self, samples: list[tuple[int, float]], seed: int) -> None:
        rows = [
            row(event_id=f"e{i}", observed_at=BASE + timedelta(seconds=offset), value=value) for i, (offset, value) in enumerate(samples)
        ]
        reference = materialize_minutes(rows)
        shuffled = list(rows)
        random.Random(seed).shuffle(shuffled)
        assert materialize_minutes(shuffled) == reference

    @given(count=st.integers(min_value=0, max_value=30))
    @settings(max_examples=31, deadline=None)
    def test_row_count_is_conserved(self, count: int) -> None:
        rows = [row(event_id=f"e{i}", observed_at=BASE + timedelta(seconds=i), value=float(i)) for i in range(count)]
        facts = materialize_minutes(rows)
        assert sum(f.count for f in facts) == count
        assert sum(f.measured for f in facts) == count

    def test_bucket_seconds_default_matches_contract(self) -> None:
        assert BUCKET_SECONDS == 60


# ------------------------------------------------------------------ 指标聚合


class TestMetricsFromFacts:
    def test_empty_marks_not_measured(self) -> None:
        metrics = metrics_from_facts([])
        assert metrics[NOT_MEASURED] is True
        assert metrics["events"] == 0
        assert metrics["peak_value"] == 0.0
        assert metrics["worst_risk_level"] is None

    def test_worst_risk_is_the_smallest_level_code_across_buckets(self) -> None:
        rows = [
            row(event_id="h1", kind="hazard", observed_at=BASE, risk_level=int(RiskLevel.BLUE)),
            row(event_id="h2", kind="hazard", observed_at=BASE + timedelta(minutes=1), risk_level=int(RiskLevel.ORANGE)),
        ]
        metrics = metrics_from_facts(materialize_minutes(rows))
        assert metrics["worst_risk_level"] == int(RiskLevel.ORANGE)

    def test_telemetry_only_window_has_no_risk_claim(self) -> None:
        metrics = metrics_from_facts(materialize_minutes([row(event_id="t", observed_at=BASE, value=1.0)]))
        assert metrics["worst_risk_level"] is None
        assert metrics["hazard_alerts"] == 0

    def test_metrics_split_by_kind(self) -> None:
        rows = [
            row(event_id="t", observed_at=BASE, value=42.0),
            row(event_id="h", kind="hazard", observed_at=BASE, risk_level=3),
            row(event_id="l", kind="latency", observed_at=BASE, latency_ms=1234.0),
        ]
        metrics = metrics_from_facts(materialize_minutes(rows))
        assert metrics[NOT_MEASURED] is False
        assert metrics["telemetry_samples"] == 1
        assert metrics["hazard_alerts"] == 1
        assert metrics["latency_samples"] == 1
        assert metrics["peak_value"] == pytest.approx(42.0)
        assert metrics["worst_latency_p95_ms"] == pytest.approx(1234.0)
        assert metrics["worst_risk_level"] == 3

    def test_worst_latency_is_the_max_across_buckets(self) -> None:
        rows = [
            row(event_id="l1", kind="latency", observed_at=BASE, latency_ms=100.0),
            row(event_id="l2", kind="latency", observed_at=BASE + timedelta(minutes=1), latency_ms=900.0),
        ]
        metrics = metrics_from_facts(materialize_minutes(rows))
        assert metrics["worst_latency_p95_ms"] == pytest.approx(900.0)

    def test_minute_fact_shape_is_dict_exportable(self) -> None:
        (fact,) = materialize_minutes([row(event_id="a", observed_at=BASE, value=1.0)])
        payload = fact.as_dict()
        assert set(payload) == {
            "minute",
            "region_code",
            "kind",
            "count",
            "total",
            "peak",
            "floor",
            "mean",
            "p95",
            "skipped_missing",
            "worst_risk_level",
        }
        assert payload["minute"] == BASE
