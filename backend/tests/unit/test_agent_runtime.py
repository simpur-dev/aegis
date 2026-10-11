"""装配点（§3/§6）的口径：越限判据与规则引擎【不许分歧】，漂移腿只在没碰阈值时补位。

这里不写死任何阈值：用例从 `RuleEngine` 的当前规则表里反推读数（比阈值大还是小由算符决定），
所以规则表改版后用例跟着改，而不是把某条阈值抄进测试里。真正钉住的是那条纪律——
detector 报不报，必须与引擎判不判一模一样；不一样就说明装配点上偷偷加了第二套判据。
"""

from __future__ import annotations

from typing import Any

import pytest

from aegis.agents.assess import AssessAgent
from aegis.agents.perceive import PerceiveAgent
from aegis.agents.rest_context import RestAssessContext
from aegis.agents.runtime import RuleEngineDetector, build_agent_runtime
from aegis.domain.enums import AgentType
from aegis.domain.messages import TelemetryReading, now_iso
from aegis.services.trigger_rules import RuleEngine

REGION = "540121"


def trigger_reading(rule: Any) -> dict[str, Any]:
    """按规则的算符造一条【必然满足该条件】的读数：阈值从规则表来，不抄进测试。"""
    condition = rule.conditions[0]
    delta = 1.0 if condition.op.startswith(">") else -1.0
    return {
        "station_id": "ST-A0001",
        "metric": condition.metric,
        "value": condition.threshold + delta,
        "unit": "raw",
        "region_code": REGION,
        "observed_at": now_iso(),
    }


def calm_reading(metric: str, value: float, station: str = "ST-A0001", at: str | None = None) -> dict[str, Any]:
    return {
        "station_id": station,
        "metric": metric,
        "value": value,
        "unit": "mm",
        "region_code": REGION,
        "observed_at": at or now_iso(),
    }


def test_越限判据与规则引擎不许有分歧() -> None:
    engine = RuleEngine()
    detector = RuleEngineDetector(engine)
    disagreements: list[str] = []
    for rule in engine.rules:
        reading = trigger_reading(rule)
        parsed = TelemetryReading.model_validate(reading)
        hits = engine.evaluate([parsed], region_code=REGION).hits
        anomaly = detector.flag(reading)
        if bool(hits) != (anomaly is not None):
            disagreements.append(f"{rule.rule_id}: 引擎 {len(hits)} 命中 / detector {anomaly}")
        elif anomaly is not None:
            assert anomaly.hazard_type == max(hits, key=lambda h: h.score).hazard_type
    assert disagreements == [], f"装配点上出现了第二套判据：{disagreements}"


def test_分数直接取自规则不给装配点加戏() -> None:
    engine = RuleEngine()
    rule = next(r for r in engine.rules if len(r.conditions) == 1 and r.conditions[0].agg == "max")
    reading = trigger_reading(rule)
    parsed = TelemetryReading.model_validate(reading)
    hits = engine.evaluate([parsed], region_code=REGION).hits
    assert hits, f"这条读数应能越限：{rule.rule_id}"
    expected = max(hits, key=lambda hit: hit.score).score
    anomaly = RuleEngineDetector(engine).flag(reading)
    assert anomaly is not None
    assert anomaly.base_score == expected, "越限分数必须由规则给，装配点不得重标定"


def test_不合形状的读数判不了就沉默() -> None:
    engine = RuleEngine()
    detector = RuleEngineDetector(engine)
    assert detector.flag({"station_id": "ST-1", "metric": "displacement_mm", "value": 999}) is None, "缺 region_code/时间戳"
    assert detector.flag({**calm_reading("displacement_mm", 999.0), "region_code": "坏编码"}) is None
    assert detector.flag({**calm_reading("displacement_mm", 999.0), "value": None}) is None


def test_没碰阈值但偏离自身常态时漂移腿补位() -> None:
    """变异点：如果复合判据把越限当唯一来源，这条缓漂读数就不会产出任何异常。"""
    engine = RuleEngine()
    drift_only = {
        "station_id": "ST-A0002",
        "metric": "displacement_mm",
        "value": 7.5,
        "unit": "mm",
        "region_code": REGION,
        "observed_at": now_iso(),
    }
    assert RuleEngineDetector(engine).flag(drift_only) is None, "先确认这条读数真的没越限"
    agents = build_agent_runtime(_NullTransport(), rule_engine=engine, base_url="http://aegis.internal")
    perceive = next(a for a in agents if a.agent_type is AgentType.PERCEIVE)
    for value in [2.0] * 6:
        perceive.detector.flag(calm_reading("displacement_mm", value, station="ST-A0002"))  # type: ignore[union-attr]
    anomaly = perceive.detector.flag(drift_only)  # type: ignore[union-attr]
    assert anomaly is not None and anomaly.hazard_type == "landslide"
    assert anomaly.base_score > 0.5, "弱信号由漂移腿给出，分数不该是越限尺度"


def test_已越限的读数不被漂移腿重复计一次() -> None:
    engine = RuleEngine()
    agents = build_agent_runtime(_NullTransport(), rule_engine=engine, base_url="http://aegis.internal")
    perceive = next(a for a in agents if a.agent_type is AgentType.PERCEIVE)
    rule = next(r for r in engine.rules if len(r.conditions) == 1 and r.conditions[0].agg == "max")
    reading = trigger_reading(rule)
    quiet = {**reading, "value": 0.1}
    for _ in range(6):
        perceive.detector.flag(quiet)  # type: ignore[union-attr]
    anomaly = perceive.detector.flag(reading)  # type: ignore[union-attr]
    hits = engine.evaluate([TelemetryReading.model_validate(reading)], region_code=REGION).hits
    assert hits, f"单条件 max 规则应被这条读数越限：{rule.rule_id}"
    engine_score = max(hits, key=lambda hit: hit.score).score
    assert anomaly is not None
    assert anomaly.base_score == engine_score, "越限优先：不能同一条读数拿两条命中去抬聚集分数"
    assert anomaly.base_score != 0.95, "0.95 是漂移腿的封顶值，撞上它这条用例就分不出是谁给的"


class _NullTransport:
    name = "null"

    async def publish(self, *args: object, **kwargs: object) -> None: ...

    async def subscribe(self, *args: object, **kwargs: object) -> None: ...

    async def close(self) -> None: ...


def test_装配点把三条腿各就各位() -> None:
    class Parser:
        async def parse(self, text: str, *, region_code: str | None = None, hazard_hint: str | None = None) -> Any: ...

    engine = RuleEngine()
    parser = Parser()
    agents = build_agent_runtime(
        _NullTransport(), rule_engine=engine, base_url="http://aegis.internal/", parser=parser, service_token="tok"
    )
    assert {a.agent_type.value for a in agents} == {"perceive", "assess", "plan", "feedback"}, "execute 未获发送权，不装配"
    perceive = next(a for a in agents if isinstance(a, PerceiveAgent))
    assess = next(a for a in agents if isinstance(a, AssessAgent))
    assert perceive.detector is not None and perceive.detector.drift.rules == engine.rules  # type: ignore[union-attr]
    assert isinstance(perceive.detector.threshold, RuleEngineDetector)  # type: ignore[attr-defined]
    assert perceive.semantic is parser
    assert isinstance(assess.context, RestAssessContext), "研判佐证必须走 REST，不给内环读存储层的路"


@pytest.mark.parametrize("metric,base", [("displacement_mm", 2.0), ("crack_aperture_mm", 1.0)])
def test_漂移基线是按站点与指标分别立的(metric: str, base: float) -> None:
    engine = RuleEngine()
    agents = build_agent_runtime(_NullTransport(), rule_engine=engine, base_url="http://aegis.internal")
    perceive = next(a for a in agents if a.agent_type is AgentType.PERCEIVE)
    for value in [base] * 6:
        perceive.detector.flag(calm_reading(metric, value))  # type: ignore[union-attr]
    other_metric = "crack_aperture_mm" if metric == "displacement_mm" else "displacement_mm"
    assert perceive.detector.baseline_for("ST-A0001", other_metric) is None  # type: ignore[attr-defined], "换指标不得复用别的基线"
    state = perceive.detector.baseline_for("ST-A0001", metric)  # type: ignore[attr-defined]
    assert state is not None and state[0] == pytest.approx(base) and state[2] == 6
