"""分规则命中与误报的对照 + 修订建议（批次 C3 的算式侧）。

这份报表的边界先说清楚，免得被读成"标定已完成"：

- **命中次数是实测**：来自平台链路台账（每条链路携带的触发命中），有样本就有数；
- **误报对照必须有现场真值**（批次 E1 的标注案例集）。缺真值时 `precision` 写 None、
  `status` 写 `not_measured`——不拿"看起来合理"的比例冒充准确率结论，
  与 `persistence/replay.py` 的 `official_accuracy` 同一套纪律；
- **建议一律 `applied=False`**：生效要人工审核并作为新版本写回 `trigger_rules`（批次 C2）。
  这条闭环里没有任何自动改阈值的路径，这是设计而不是待办。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from aegis.services.trigger_rules import Rule

#: 一条规则至少要有这么多"真值 + 命中"的对照样本，才允许对它下误报结论。
MIN_RULE_SAMPLES = 3
#: 低于该精确率就提示复核阈值/时间窗（不是自动改，是给人看的旗标）。
LOW_PRECISION = 0.5

VERDICT_MEASURED_OK = "measured_ok"
VERDICT_SUSPECT_PRECISION = "suspect_precision"
VERDICT_NEVER_FIRED = "never_fired"
VERDICT_NOT_MEASURED = "not_measured"


@dataclass(frozen=True, slots=True)
class RuleSample:
    """一条规则的对照数据。`true_positive`/`false_positive` 只有现场真值到位才可能有数。"""

    rule_id: str
    hits: int = 0
    true_positive: int = 0
    false_positive: int = 0
    cases: int = 0

    @property
    def truth_pairs(self) -> int:
        return self.true_positive + self.false_positive


@dataclass(frozen=True, slots=True)
class RuleAdvice:
    rule_id: str
    hazard_type: str
    version: int
    triggered_level: int
    calibration_basis: str
    reviewer: str
    hits: int
    cases: int
    true_positive: int
    false_positive: int
    precision: float | None
    verdict: str
    suggestion: str | None
    applied: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "hazard_type": self.hazard_type,
            "version": self.version,
            "triggered_level": self.triggered_level,
            "calibration_basis": self.calibration_basis,
            "reviewer": self.reviewer,
            "hits": self.hits,
            "cases": self.cases,
            "true_positive": self.true_positive,
            "false_positive": self.false_positive,
            "precision": self.precision,
            "verdict": self.verdict,
            "suggestion": self.suggestion,
            "applied": self.applied,
        }


@dataclass(frozen=True, slots=True)
class CalibrationReport:
    status: str
    dataset_kind: str
    dataset_cases: int
    min_samples: int
    rules: int
    fired_rules: int
    hazards_covered: tuple[str, ...]
    advice: tuple[RuleAdvice, ...]
    note: str
    auto_applied: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "auto_applied": self.auto_applied,
            "dataset": {"kind": self.dataset_kind, "cases": self.dataset_cases, "min_samples_per_rule": self.min_samples},
            "rules": self.rules,
            "fired_rules": self.fired_rules,
            "hazards_covered": list(self.hazards_covered),
            "note": self.note,
            "advice": [row.as_dict() for row in self.advice],
        }


def collect_rule_hits(chains: Iterable[Any]) -> dict[str, int]:
    """从链路台账统计每条规则的命中次数。

    既接受 `ChainResult`（读视图里的对象）也接受它的 `as_dict()` 结果：台账在内存与库侧
    是两种形状，判据只有一条——命中的 `rule_id` 是什么。
    """
    counts: dict[str, int] = {}
    for row in chains:
        raw = getattr(row, "hits", None)
        if raw is None and isinstance(row, Mapping):
            raw = row.get("hits")
        for hit in raw or []:
            rule_id = hit.get("rule_id") if isinstance(hit, Mapping) else getattr(hit, "rule_id", None)
            if rule_id:
                counts[str(rule_id)] = counts.get(str(rule_id), 0) + 1
    return counts


def _precision(sample: RuleSample, min_samples: int) -> float | None:
    if sample.truth_pairs < min_samples:
        return None
    return round(sample.true_positive / sample.truth_pairs, 4)


def _advice_for(rule: Rule, sample: RuleSample, min_samples: int, dataset_cases: int) -> RuleAdvice:
    precision = _precision(sample, min_samples)
    if precision is not None:
        if precision < LOW_PRECISION:
            verdict = VERDICT_SUSPECT_PRECISION
            window = ", ".join(f"{item.metric}{item.op}{item.threshold}（{item.window_seconds}s/{item.agg}）" for item in rule.conditions)
            suggestion = f"误报占比偏高（precision={precision}）：建议复核阈值与时间窗 {window}，形成新版本后由人工审核发布（本条未生效）"
        else:
            verdict = VERDICT_MEASURED_OK
            suggestion = None
    elif sample.hits == 0 and dataset_cases >= min_samples:
        verdict = VERDICT_NEVER_FIRED
        metrics = ", ".join(item.metric for item in rule.conditions)
        suggestion = f"在 {dataset_cases} 个标注案例窗口内从未命中：阈值可能偏高，或指标 {metrics} 在现场缺数（未测得，需人工复核）"
    else:
        verdict = VERDICT_NOT_MEASURED
        suggestion = None
    return RuleAdvice(
        rule_id=rule.rule_id,
        hazard_type=rule.hazard_type.value,
        version=rule.version,
        triggered_level=int(rule.triggered_level),
        calibration_basis=rule.calibration_basis,
        reviewer=rule.reviewer,
        hits=sample.hits,
        cases=sample.cases or dataset_cases,
        true_positive=sample.true_positive,
        false_positive=sample.false_positive,
        precision=precision,
        verdict=verdict,
        suggestion=suggestion,
    )


def calibration_report(
    rulebook: Iterable[Rule],
    samples: Mapping[str, RuleSample] | None = None,
    *,
    dataset_kind: str = "unspecified",
    dataset_cases: int = 0,
    min_samples: int = MIN_RULE_SAMPLES,
) -> CalibrationReport:
    """把规则集 + 对照样本算成标定报表。样本缺失就是 `not_measured`，不猜。"""
    rules = list(rulebook)
    collected = dict(samples or {})
    advice = tuple(
        _advice_for(rule, collected.get(rule.rule_id, RuleSample(rule_id=rule.rule_id)), min_samples, dataset_cases) for rule in rules
    )
    measured = [row for row in advice if row.precision is not None]
    if not measured:
        status = "not_measured"
        note = (
            f"分规则误报对照未测得：需要 {min_samples} 条以上带现场真值的配对样本（批次 E1）。"
            "本表的命中次数是平台台账实测，精度与修订建议留空。"
        )
    elif dataset_kind != "field":
        status = "partial"
        note = f"{len(measured)} 条规则有精度对照，但数据集为 {dataset_kind}（非现场标注），不构成标定完成结论"
    else:
        flagged = sum(1 for row in measured if row.verdict != VERDICT_MEASURED_OK)
        status = "measured"
        note = f"{len(measured)} 条规则给出精度，其中 {flagged} 条需要人工复核（建议均未自动生效）"
    return CalibrationReport(
        status=status,
        dataset_kind=dataset_kind,
        dataset_cases=dataset_cases,
        min_samples=min_samples,
        rules=len(rules),
        fired_rules=sum(1 for row in advice if row.hits > 0),
        hazards_covered=tuple(sorted({rule.hazard_type.value for rule in rules})),
        advice=advice,
        note=note,
    )


__all__ = [
    "LOW_PRECISION",
    "MIN_RULE_SAMPLES",
    "CalibrationReport",
    "RuleAdvice",
    "RuleSample",
    "calibration_report",
    "collect_rule_hits",
]
