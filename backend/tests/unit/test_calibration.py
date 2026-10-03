"""阈值标定闭环的算式侧（批次 C3）。

这个文件里最要紧的断言不是"能算出精度"，而是**没有真值时算不出精度**：
标定闭环最怕的就是拿一份合成数据算出来的比例，对外说成"阈值已经过标定"。
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from aegis.domain.enums import HazardType, RiskLevel
from aegis.services.calibration import (
    LOW_PRECISION,
    MIN_RULE_SAMPLES,
    VERDICT_MEASURED_OK,
    VERDICT_NEVER_FIRED,
    VERDICT_NOT_MEASURED,
    VERDICT_SUSPECT_PRECISION,
    RuleSample,
    calibration_report,
    collect_rule_hits,
)
from aegis.services.trigger_rules import RuleEngine


def _rule(rule_id: str = "R-DEBRIS-RAIN-1") -> Any:
    rule = RuleEngine().rule_by_id(rule_id)
    assert rule is not None
    return rule


def test_台账命中次数按规则累计() -> None:
    chains = [
        {"hits": [{"rule_id": "R-DEBRIS-RAIN-1"}, {"rule_id": "R-LAKE-1"}, {"rule_id": None}]},
        {"hits": [{"rule_id": "R-DEBRIS-RAIN-1"}]},
        {"hits": []},
    ]
    assert collect_rule_hits(chains) == {"R-DEBRIS-RAIN-1": 2, "R-LAKE-1": 1}


def test_命中计数也吃链路对象而不只吃字典() -> None:
    from aegis.domain.messages import TriggerHit

    class Row:
        def __init__(self, hits: list[Any]) -> None:
            self.hits = hits

    hit = TriggerHit(rule_id="R-LAKE-2", hazard_type="lake_outburst", region_code="540500", score=1.0)
    assert collect_rule_hits([Row([hit])]) == {"R-LAKE-2": 1}
    assert collect_rule_hits([{"hits": [{"rule_id": "R-LAKE-2"}]}]) == {"R-LAKE-2": 1}


def test_没有真值时精度一律未测得且建议为空() -> None:
    report = calibration_report(RuleEngine().rules, {"R-DEBRIS-RAIN-1": RuleSample(rule_id="R-DEBRIS-RAIN-1", hits=4)})
    assert report.status == "not_measured"
    assert report.auto_applied is False
    assert all(row.precision is None and row.suggestion is None for row in report.advice)
    assert "未测得" in report.note
    assert report.fired_rules == 1
    assert "E1" in report.note, "缺真值这件事要指名它是哪一批的事，而不是留一句空话"


def test_样本不足一条规则也不给精度结论() -> None:
    sample = RuleSample(rule_id="R-DEBRIS-RAIN-1", hits=9, true_positive=1, false_positive=1)
    report = calibration_report([_rule()], {sample.rule_id: sample}, dataset_kind="field", dataset_cases=40)
    row = report.advice[0]
    assert row.precision is None
    assert row.verdict == VERDICT_NOT_MEASURED
    assert row.hits == 9, "命中次数是实测，不该因为没真值就被丢掉"
    assert report.status == "not_measured"


def test_误报占比高的规则给出复核建议但不生效() -> None:
    sample = RuleSample(rule_id="R-DEBRIS-RAIN-1", hits=20, true_positive=2, false_positive=14, cases=16)
    report = calibration_report([_rule()], {sample.rule_id: sample}, dataset_kind="field", dataset_cases=40)
    row = report.advice[0]
    assert row.precision is not None and row.precision < LOW_PRECISION
    assert row.verdict == VERDICT_SUSPECT_PRECISION
    assert row.applied is False and report.auto_applied is False
    assert "阈值" in (row.suggestion or "") and "人工审核" in (row.suggestion or "")
    assert report.status == "measured"


def test_精度达标的规则不被建议打扰() -> None:
    sample = RuleSample(rule_id="R-DEBRIS-RAIN-1", hits=12, true_positive=11, false_positive=1, cases=12)
    report = calibration_report([_rule()], {sample.rule_id: sample}, dataset_kind="field", dataset_cases=30)
    assert report.advice[0].verdict == VERDICT_MEASURED_OK
    assert report.advice[0].suggestion is None


def test_从未命中的规则在样本够多时被点名() -> None:
    rule = replace(_rule("R-LAKE-2"), hazard_type=HazardType.AVALANCHE, triggered_level=RiskLevel.YELLOW)
    report = calibration_report([rule], {}, dataset_kind="field", dataset_cases=40)
    row = report.advice[0]
    assert row.hits == 0
    assert row.verdict == VERDICT_NEVER_FIRED
    assert "缺数" in (row.suggestion or "")


def test_合成数据集即便算出精度也只算部分完成() -> None:
    sample = RuleSample(rule_id="R-DEBRIS-RAIN-1", hits=5, true_positive=5, false_positive=0, cases=5)
    report = calibration_report([_rule()], {sample.rule_id: sample}, dataset_kind="synthetic", dataset_cases=20)
    assert report.advice[0].precision == 1.0
    assert report.status == "partial"
    assert "不构成标定完成结论" in report.note


def test_报表规模与灾种覆盖对得上内置规则集() -> None:
    report = calibration_report(RuleEngine().rules)
    assert report.rules == 9
    assert len(report.hazards_covered) >= 5
    assert all(row.calibration_basis for row in report.advice), "标定依据必须随行外显，否则报表看不出哪些还没标定"
    assert report.min_samples == MIN_RULE_SAMPLES
