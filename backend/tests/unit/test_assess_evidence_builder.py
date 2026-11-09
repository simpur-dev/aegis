"""研判证据构建：派生表是唯一合法口径，禁止近似映射，出处必须如实。

这组用例钉住的是"证据包里出现了什么数、以及它凭什么出现在那里"：
- 每条派生都按声明公式换算（雨 ×6、位移 (last-first)/h×24、水位/坝高）；
- 没有把握的遥测进 `unmapped_metrics` 并写原因，不冒充因子；
- 非 ok 质量、非有限值、跨区域、窗口外读数一律剔除并在 note 里报数；
- 出处分档（observed/mock/mixed/unknown），混合也置 `is_mock=True`；
- 超限显式 `truncated=True`，不静默截断。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from aegis.domain.messages import TelemetryReading, now_iso
from aegis.domain.provenance import DataProvenance
from aegis.domain.site import SiteAttribute, SiteProfile
from aegis.pipeline.assess_input import AssessEvidenceBuilder
from aegis.storage.store import PlatformStore

REGION = "540121"
NOW = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)


def _reading(
    metric: str,
    value: float,
    *,
    station: str = "PG-S1",
    minutes_ago: float = 5,
    region: str = REGION,
    source: str = "simulator",
    quality: str = "ok",
    unit: str = "",
) -> TelemetryReading:
    observed = NOW - timedelta(minutes=minutes_ago)
    return TelemetryReading(
        station_id=station,
        metric=metric,
        value=value,
        unit=unit,
        region_code=region,
        observed_at=now_iso(observed),
        ingested_at=now_iso(observed),
        source=source,
        quality_flag=quality,
    )


async def _build(
    readings: list[TelemetryReading] | None = None,
    *,
    profile: SiteProfile | None = None,
    stored: list[TelemetryReading] | None = None,
    hits: list[Any] | None = None,
    intake: str = "telemetry",
    **kwargs: Any,
):
    store = PlatformStore()
    if stored:
        await store.telemetry.add(stored)
    if profile is not None:
        await store.upsert_site_profile(profile)
    builder = AssessEvidenceBuilder(store, **kwargs)
    return await builder.build(region=REGION, hits=hits or [], readings=readings or [], intake=intake, now=NOW)


def _factor(evidence: Any, name: str):
    matches = [obs for obs in evidence.observations if obs.factor == name]
    assert len(matches) == 1, [obs.factor for obs in evidence.observations]
    return matches[0]


def _demo_profile(**attributes: Any) -> SiteProfile:
    rows = [SiteAttribute(key=key, value=value, source="demo_survey") for key, value in attributes.items()]
    return SiteProfile(
        region_code=REGION,
        attributes=rows,
        provenance=DataProvenance(data_kind="demo", is_mock=True),
    )


class TestDerivations:
    async def test_十分钟雨量按小时折算(self) -> None:
        evidence = await _build([_reading("rain_10min", 1.2)])

        rain = _factor(evidence, "rainfall_mm_h")
        assert rain.max_value == pytest.approx(7.2)
        assert rain.unit == "mm/h"
        assert "×6" in rain.derivation
        assert rain.quality == "ok" and rain.samples == 1
        assert rain.station_ids == ["PG-S1"]

    async def test_位移按每站首末差换算日速率(self) -> None:
        evidence = await _build(
            [
                _reading("displacement_mm", 0.0, minutes_ago=120),
                _reading("displacement_mm", 2.0, minutes_ago=0),
            ],
            window_minutes=240,
        )

        disp = _factor(evidence, "displacement_mm_day")
        assert disp.max_value == pytest.approx(24.0)
        assert disp.unit == "mm/day"
        assert "×24" in disp.derivation

    async def test_位移单点样本不猜速率(self) -> None:
        evidence = await _build([_reading("displacement_mm", 2.0)])

        assert not [obs for obs in evidence.observations if obs.factor == "displacement_mm_day"]
        unmapped = {row.metric: row for row in evidence.unmapped_metrics}
        assert "displacement_mm" in unmapped
        assert "2 个 ok 样本" in unmapped["displacement_mm"].reason

    async def test_水位比按坝高派生(self) -> None:
        profile = _demo_profile(dam_height_m=3.5)
        evidence = await _build([_reading("lake_level_m", 3.0)], profile=profile)

        ratio = _factor(evidence, "water_level_ratio")
        assert ratio.max_value == pytest.approx(3.0 / 3.5)
        assert "dam_height_m" in ratio.derivation

    async def test_缺坝高时不派生水位比(self) -> None:
        evidence = await _build([_reading("lake_level_m", 3.0)])

        assert not [obs for obs in evidence.observations if obs.factor == "water_level_ratio"]
        unmapped = {row.metric: row for row in evidence.unmapped_metrics}
        assert "dam_height_m" in unmapped["lake_level_m"].reason

    async def test_裂缝与气温取窗口最大值(self) -> None:
        evidence = await _build(
            [
                _reading("crack_aperture_mm", 1.5, minutes_ago=10),
                _reading("crack_aperture_mm", 4.5, minutes_ago=5),
                _reading("air_temperature_c", -2.0, minutes_ago=10),
                _reading("air_temperature_c", 6.5, minutes_ago=5),
            ]
        )

        assert _factor(evidence, "crack_width_mm").max_value == pytest.approx(4.5)
        assert _factor(evidence, "air_temp_c").max_value == pytest.approx(6.5)


class TestNoApproximation:
    @pytest.mark.parametrize(
        ("metric", "forbidden_factor"),
        [
            ("new_snow_cm", "new_snow_24h_cm"),
            ("dam_seepage_turbidity_ntu", "seepage_level"),
            ("debris_level", "debris_flow_intensity"),
            ("rain_cumulative_24h", "rainfall_mm_h"),
        ],
    )
    async def test_没把握的遥测不冒充因子(self, metric: str, forbidden_factor: str) -> None:
        evidence = await _build([_reading(metric, 12.0)])

        assert not [obs for obs in evidence.observations if obs.factor == forbidden_factor]
        unmapped = {row.metric: row for row in evidence.unmapped_metrics}
        assert metric in unmapped
        assert unmapped[metric].reason, "不映射必须给出原因，不能只留一个空名"


class TestQualityFiltering:
    async def test_非ok质量读数被剔透并计数(self) -> None:
        evidence = await _build(
            [
                _reading("rain_10min", 1.2, quality="ok"),
                _reading("crack_aperture_mm", 3.0, quality="suspect", minutes_ago=6),
                _reading("crack_aperture_mm", 3.1, quality="drift", minutes_ago=5),
                _reading("crack_aperture_mm", 3.2, quality="missing", minutes_ago=4),
            ]
        )

        assert _factor(evidence, "rainfall_mm_h").samples == 1
        assert not [obs for obs in evidence.observations if obs.factor == "crack_width_mm"]
        assert any("quality_flag≠ok" in note and "3" in note for note in evidence.notes), evidence.notes

    async def test_非有限值被剔除并计数(self) -> None:
        evidence = await _build(
            [
                _reading("rain_10min", 1.2),
                _reading("crack_aperture_mm", float("nan"), minutes_ago=6),
                _reading("crack_aperture_mm", float("inf"), minutes_ago=5),
            ]
        )

        assert _factor(evidence, "rainfall_mm_h").max_value == pytest.approx(7.2)
        assert not [obs for obs in evidence.observations if obs.factor == "crack_width_mm"]
        assert any("非有限值" in note and "2" in note for note in evidence.notes), evidence.notes


class TestScopeAndDedup:
    async def test_board与库读数去重只算一次(self) -> None:
        one = _reading("rain_10min", 1.2)
        # 同 (station, metric, observed_at) 的库副本：内容相同，不该把 samples 变成 2
        evidence = await _build([one], stored=[one])

        rain = _factor(evidence, "rainfall_mm_h")
        assert rain.samples == 1
        assert rain.max_value == pytest.approx(7.2)

    async def test_窗口外读数不参与(self) -> None:
        evidence = await _build(
            [
                _reading("rain_10min", 1.2, minutes_ago=5),
                _reading("rain_10min", 99.0, minutes_ago=61),
            ],
            stored=[_reading("rain_10min", 77.0, minutes_ago=90)],
        )

        assert _factor(evidence, "rainfall_mm_h").max_value == pytest.approx(7.2)

    async def test_跨区域读数剔除并留痕(self) -> None:
        evidence = await _build(
            [
                _reading("rain_10min", 1.2),
                _reading("crack_aperture_mm", 5.0, region="540221"),
            ]
        )

        assert not [obs for obs in evidence.observations if obs.factor == "crack_width_mm"]
        assert any("跨区域" in note for note in evidence.notes), evidence.notes


class TestSiteProfile:
    async def test_静态属性原样透传(self) -> None:
        profile = _demo_profile(slope_angle_deg=38.0, aspect="N")
        evidence = await _build(profile=profile)

        slope = _factor(evidence, "slope_angle_deg")
        assert slope.quality == "site" and slope.samples == 1
        assert slope.max_value == pytest.approx(38.0)
        assert slope.derivation == "site_profile"
        assert "site_profile:demo_survey" in slope.sources

        aspect = _factor(evidence, "aspect")
        assert aspect.class_value == "N" and aspect.max_value is None

    async def test_只有site属性时出处取画像口径(self) -> None:
        evidence = await _build(profile=_demo_profile(slope_angle_deg=38.0))

        assert evidence.provenance.data_kind == "demo"
        assert evidence.provenance.is_mock is True
        assert "site_profile:demo_survey" in evidence.provenance.sources


class TestProvenance:
    async def test_全模拟来源记为mock(self) -> None:
        evidence = await _build([_reading("rain_10min", 1.2, source="simulator")])

        assert evidence.provenance.data_kind == "mock"
        assert evidence.provenance.is_mock is True
        assert evidence.provenance.disclaimer, "mock 证据必须自带免责声明"
        assert "platform.telemetry:simulator" in evidence.provenance.sources

    async def test_真实来源记为observed(self) -> None:
        evidence = await _build([_reading("rain_10min", 1.2, source="mqtt")])

        assert evidence.provenance.data_kind == "observed"
        assert evidence.provenance.is_mock is False
        assert evidence.provenance.disclaimer == ""

    async def test_混合来源记为mixed且仍标mock(self) -> None:
        evidence = await _build(
            [
                _reading("rain_10min", 1.2, source="simulator"),
                _reading("crack_aperture_mm", 3.0, source="mqtt"),
            ]
        )

        assert evidence.provenance.data_kind == "mixed"
        assert evidence.provenance.is_mock is True, "宁可读者高估，也不把 mock 当实测"

    async def test_空输入不抛异常且出处unknown(self) -> None:
        evidence = await _build([])

        assert evidence.observations == []
        assert evidence.provenance.data_kind == "unknown"
        assert any("证据为空" in note for note in evidence.notes), evidence.notes

    async def test_上报且无遥测记为reported(self) -> None:
        evidence = await _build([], intake="report")

        assert evidence.provenance.data_kind == "reported"


class TestBounds:
    async def test_观测超上限显式截断(self) -> None:
        attributes = {f"attr_{index:02d}": float(index) for index in range(50)}
        evidence = await _build(profile=_demo_profile(**attributes))

        assert len(evidence.observations) == 48
        assert evidence.truncated is True
        assert any("超过上限" in note for note in evidence.notes), evidence.notes

    async def test_字节超限显式截断(self) -> None:
        evidence = await _build(
            [
                _reading("rain_10min", 1.2),
                _reading("crack_aperture_mm", 3.0),
                _reading("air_temperature_c", 5.0),
            ],
            max_payload_bytes=400,
        )

        assert evidence.truncated is True
        assert len(evidence.observations) < 3
        assert any("字节" in note for note in evidence.notes), evidence.notes
