"""快腿 EWMA 漂移判据（§3.1）的行为口径：判"像不像它自己"，灾种归属只认平台规则表。

这组用例的规则表一律注入 `default_rulebook()` 真源，而不是手抄一份映射——手抄的表会跟着
规则版本漂移，而"歧义指标不报"恰恰是内置规则表里 `rain_cumulative_24h` 的真实形状：
它同时指向泥石流与滑坡，本判据就对该指标闭口。数值断言都取自指数加权基线的实际算式，
带内抖动一条不报、带外跳变必报，才是"弱信号增强"而不是"换个阈值再报一遍"。
"""

from __future__ import annotations

import pytest

from aegis.agents.perceive import EwmaDriftDetector, PerceiveAgent
from aegis.domain.enums import AgentType, HazardType
from aegis.domain.messages import TriggerHit, now_iso
from aegis.services.rules.trigger_rules import default_rulebook

STATION = "ST-A0001"
REGION = "540121"
METRIC = "displacement_mm"  # 内置规则表里唯一指向 landslide


def reading(value: float, station: str = STATION, metric: str = METRIC, at: str | None = None) -> dict[str, object]:
    return {
        "station_id": station,
        "metric": metric,
        "value": value,
        "unit": "mm",
        "region_code": REGION,
        "observed_at": at or now_iso(),
    }


@pytest.fixture()
def detector() -> EwmaDriftDetector:
    return EwmaDriftDetector(rules=default_rulebook())


def test_规则表把指标指向多个灾种时本判据闭口(detector: EwmaDriftDetector) -> None:
    """歧义不猜：宁可丢一路弱信号，也不替指挥员把 24h 累计降雨判成泥石流或滑坡。"""
    ambiguous = {
        rule.hazard_type.value for rule in default_rulebook() for condition in rule.conditions if condition.metric == "rain_cumulative_24h"
    }
    assert ambiguous == {HazardType.DEBRIS_FLOW.value, HazardType.LANDSLIDE.value}, "内置规则表若不再歧义，这条用例的前提就变了"
    for value in [10.0] * 8 + [900.0]:
        assert detector.flag(reading(value, metric="rain_cumulative_24h")) is None


def test_指标不在规则表里时不报(detector: EwmaDriftDetector) -> None:
    for value in [1.0] * 6 + [99.0]:
        assert detector.flag(reading(value, metric="soil_moisture_pct")) is None


def test_预热不足只积累基线不出异常(detector: EwmaDriftDetector) -> None:
    """变异点：若在 samples<min_samples 时就判，前几个点的方差还是 0，任何噪声都会被当"漂移"报出去。"""
    for index in range(detector.min_samples - 1):
        assert detector.flag(reading(2.0 + index)) is None
    assert detector.baseline_for(STATION, METRIC) is not None
    assert detector.baseline_for(STATION, METRIC)[2] == detector.min_samples - 1  # type: ignore[index]


def test_自身常态带内的抖动一条都不报(detector: EwmaDriftDetector) -> None:
    """这是 EWMA 与"再设一个阈值"的分界：幅度 1mm 的规律抖动，站点自己就认为这是常态。"""
    flags = [detector.flag(reading(value)) for value in [2.0, 3.0] * 10]
    assert [flag for flag in flags if flag is not None] == []


def test_跳出自身常态必须报且灾种来自规则表(detector: EwmaDriftDetector) -> None:
    for value in [2.0, 3.0] * 10:
        detector.flag(reading(value))
    anomaly = detector.flag(reading(12.0))
    assert anomaly is not None
    assert (anomaly.hazard_type, anomaly.region_code, anomaly.station_id) == ("landslide", REGION, STATION)
    assert anomaly.value == 12.0 and anomaly.unit == "mm"
    assert 0.5 <= anomaly.base_score <= 0.95, "弱信号不自称满格"


def test_常数序列后的任何变化都算偏离但封顶095(detector: EwmaDriftDetector) -> None:
    """方差为零时限幅为零：分母没了就不做比例打分，直接给封顶值，避免除零或谎报 1.0。"""
    for value in [2.0] * 6:
        assert detector.flag(reading(value)) is None
    anomaly = detector.flag(reading(2.5))
    assert anomaly is not None and anomaly.base_score == 0.95
    assert detector.flag(reading(2.5)) is None, "同一读数已成新常态的一部分，不再重复报"


def test_偏离越大分数越高(detector: EwmaDriftDetector) -> None:
    """同一基线形状下先打小偏离再（换一条基线）打大偏离：分数必须单调，否则排序就反了。"""
    scores: list[float] = []
    for jump in (2.6, 6.0):
        fresh = EwmaDriftDetector(rules=default_rulebook())
        for value in [2.0, 2.2, 2.0, 2.2, 2.0, 2.2]:
            fresh.flag(reading(value))
        anomaly = fresh.flag(reading(jump))
        assert anomaly is not None
        scores.append(anomaly.base_score)
    assert scores[0] < scores[-1] and scores[-1] == 0.95


def test_站点各立基线互不污染(detector: EwmaDriftDetector) -> None:
    for value in [2.0] * 6:
        detector.flag(reading(value, station="ST-A0001"))
    assert detector.flag(reading(8.0, station="ST-A0002")) is None, "另一站还没建立自己的基线"
    assert detector.baseline_for("ST-A0002", METRIC)[2] == 1  # type: ignore[index]
    first = detector.baseline_for("ST-A0001", METRIC)
    assert first is not None and first[0] == pytest.approx(2.0)


def test_缺字段的读数一律不判(detector: EwmaDriftDetector) -> None:
    """没有时间戳就聚不了窗，也不能凭一条无时戳的读数声称"异常"。"""
    for value in [2.0] * 6:
        detector.flag(reading(value))
    broken = [
        {**reading(99.0), "station_id": ""},
        {**reading(99.0), "metric": ""},
        {**reading(99.0), "observed_at": ""},
        {**reading(99.0), "value": "很多"},
        {**reading(99.0), "value": None},
    ]
    for item in broken:
        assert detector.flag(item) is None


def test_两站同窗漂移被聚合成一条更强的命中(detector: EwmaDriftDetector) -> None:
    """EWMA 判出的异常要和平台聚集口径接得上：同区同灾种两站 → 一条时空聚集命中且分数被抬。"""
    at = "2026-10-11T03:00:00Z"
    for station in ("ST-A0001", "ST-A0002"):
        for value in [2.0] * 6:
            detector.flag(reading(value, station=station, at=at))
    readings = [reading(9.0, station="ST-A0001", at=at), reading(7.0, station="ST-A0002", at=at)]
    agent = PerceiveAgent(
        transport=None,
        agent_type=AgentType.PERCEIVE,
        agent_id="perceive.t01",
        capabilities=["anomaly_detect"],
        detector=detector,
    )  # type: ignore[arg-type]
    hits = agent.detect(readings)
    assert len(hits) == 1
    hit = hits[0]
    assert hit["rule_id"] == f"R-SPATIOTEMPORAL-{REGION}-landslide"
    assert hit["score"] > 0.95, "两站同步漂移应被抬到接近满格"
    assert len(hit["evidence_refs"]) == 2
    TriggerHit.model_validate(hit)  # 链路就是按这个模型收 trigger_hit 事件的


def test_规则表为空时不臆造任何灾种() -> None:
    """装配点没给规则表 → 整条 EWMA 腿沉默，而不是拿 metric 名当灾种往下游塞。"""
    empty = EwmaDriftDetector(rules=[])
    for value in [2.0] * 6:
        assert empty.flag(reading(value)) is None
