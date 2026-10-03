"""解析能力评测（批次 C4 的算式侧）。

为什么单独一个模块而不是塞进脚本：评测口径要能被测试直接调用并复跑，
放在 `scripts/` 里就只能靠子进程输出对账（脚本一改版，报表口径就悄悄变了）。

三条口径纪律：
1. 数据集自己声明 `kind`；**合成集永远不算官方"识别 ≥5 类触发条件"的达成**，
   它只证明解析算术与回归不劣化（与 `persistence/replay.py` 的准确率口径同一套逻辑）；
2. 灾种、读数、等级三件事分开计：一条上报"报了但灾种错"与"根本没报"是两种故障；
3. 期望值缺省（`risk_level: null`）表示"够不上预警才是正确答案"，
   系统给了等级算错，给了 None 算对。
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aegis.services.semantic_parser import ParsedDisaster, detect_hazard, detect_level, extract_metrics, extract_region

METRIC_TOLERANCE = 0.01
MIN_CASES = 30
MIN_HAZARDS = 5


class EvalDatasetError(ValueError):
    def __init__(self, message: str, *, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.detail = detail or {}


@dataclass(frozen=True, slots=True)
class EvalCase:
    case_id: str
    text: str
    hazard_type: str
    region_code: str | None
    risk_level: int | None
    metrics: dict[str, float]
    form: str = ""

    @property
    def expects_warning(self) -> bool:
        return self.risk_level is not None


@dataclass(frozen=True, slots=True)
class EvalDataset:
    cases: tuple[EvalCase, ...]
    kind: str
    source: str
    note: str


@dataclass(frozen=True, slots=True)
class CaseOutcome:
    case_id: str
    form: str
    hazard_correct: bool
    region_correct: bool
    metrics_expected: int
    metrics_found: int
    metrics_spurious: int
    level_correct: bool
    decided_by: str
    needs_review: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "form": self.form,
            "hazard_correct": self.hazard_correct,
            "region_correct": self.region_correct,
            "metrics_expected": self.metrics_expected,
            "metrics_found": self.metrics_found,
            "metrics_spurious": self.metrics_spurious,
            "level_correct": self.level_correct,
            "decided_by": self.decided_by,
            "needs_review": self.needs_review,
        }


@dataclass(frozen=True, slots=True)
class EvalReport:
    dataset_kind: str
    dataset_source: str
    dataset_note: str
    cases: int
    hazards_covered: tuple[str, ...]
    hazard_accuracy: float
    region_accuracy: float
    metric_precision: float
    metric_recall: float
    level_accuracy: float
    by_hazard: Mapping[str, Mapping[str, float]]
    by_form: Sequence[Mapping[str, Any]]
    outcomes: Sequence[CaseOutcome]
    official_claim: bool
    note: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "dataset": {
                "kind": self.dataset_kind,
                "source": self.dataset_source,
                "note": self.dataset_note,
                "cases": self.cases,
                "min_cases": MIN_CASES,
            },
            "hazards_covered": list(self.hazards_covered),
            "hazard_accuracy": self.hazard_accuracy,
            "region_accuracy": self.region_accuracy,
            "metric_precision": self.metric_precision,
            "metric_recall": self.metric_recall,
            "level_accuracy": self.level_accuracy,
            "by_hazard": dict(self.by_hazard),
            "by_form": list(self.by_form),
            "official_claim": self.official_claim,
            "note": self.note,
            "outcomes": [outcome.as_dict() for outcome in self.outcomes],
        }


def parse_case(raw: Mapping[str, Any], *, line_no: int) -> EvalCase:
    missing = [key for key in ("id", "text", "hazard_type") if key not in raw]
    if missing:
        raise EvalDatasetError(f"第 {line_no} 行缺字段：{', '.join(missing)}", detail={"line": line_no})
    level = raw.get("risk_level")
    metrics = raw.get("metrics") or {}
    if not isinstance(metrics, dict):
        raise EvalDatasetError(f"第 {line_no} 行 metrics 必须是对象", detail={"line": line_no})
    return EvalCase(
        case_id=str(raw["id"]),
        text=str(raw["text"]),
        hazard_type=str(raw["hazard_type"]),
        region_code=None if raw.get("region_code") is None else str(raw["region_code"]),
        risk_level=None if level is None else int(level),
        metrics={str(key): float(value) for key, value in metrics.items()},
        form=str(raw.get("form") or ""),
    )


def load_dataset(path: str | Path) -> EvalDataset:
    file = Path(path)
    if not file.is_file():
        raise EvalDatasetError("评测集不存在", detail={"path": str(file)})
    cases: list[EvalCase] = []
    kind = "unspecified"
    source = str(file)
    note = "未声明数据来源"
    seen: set[str] = set()
    for line_no, line in enumerate((row for row in file.read_text(encoding="utf-8").splitlines() if row.strip()), start=1):
        if line.lstrip().startswith("#"):
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise EvalDatasetError(f"第 {line_no} 行不是合法 JSON", detail={"line": line_no}) from exc
        if not isinstance(raw, dict):
            raise EvalDatasetError(f"第 {line_no} 行不是对象", detail={"line": line_no})
        if "dataset" in raw:
            meta = raw["dataset"]
            kind = str(meta.get("kind", "unspecified"))
            source = str(meta.get("source") or file)
            note = str(meta.get("note") or "")
            continue
        case = parse_case(raw, line_no=line_no)
        if case.case_id in seen:
            raise EvalDatasetError(f"case_id 重复：{case.case_id}", detail={"line": line_no})
        seen.add(case.case_id)
        cases.append(case)
    return EvalDataset(cases=tuple(cases), kind=kind, source=source, note=note)


def score_case(case: EvalCase, parsed: ParsedDisaster) -> CaseOutcome:
    hazard_correct = parsed.hazard_type.value == case.hazard_type
    region_correct = (parsed.region_code or None) == case.region_code
    found = {metric: value for metric, value, _unit in parsed.metrics}
    metrics_found = sum(1 for metric, value in case.metrics.items() if abs(found.get(metric, float("nan")) - value) <= METRIC_TOLERANCE)
    spurious = len(found) - metrics_found
    if case.risk_level is None:
        level_correct = parsed.risk_level is None
    else:
        level_correct = parsed.risk_level is not None and int(parsed.risk_level) == case.risk_level
    return CaseOutcome(
        case_id=case.case_id,
        form=case.form,
        hazard_correct=hazard_correct,
        region_correct=region_correct,
        metrics_expected=len(case.metrics),
        metrics_found=metrics_found,
        metrics_spurious=max(spurious, 0),
        level_correct=level_correct,
        decided_by=parsed.decided_by,
        needs_review=parsed.needs_review,
    )


def score_dataset(cases: Iterable[EvalCase], outcomes: Iterable[CaseOutcome], *, dataset: EvalDataset, note_suffix: str = "") -> EvalReport:
    rows = list(outcomes)
    total = len(rows)
    hazards = sorted({case.hazard_type for case in cases})
    expected_metrics = sum(outcome.metrics_expected for outcome in rows)
    found_metrics = sum(outcome.metrics_found for outcome in rows)
    extracted = sum(outcome.metrics_found + outcome.metrics_spurious for outcome in rows)

    by_hazard: dict[str, dict[str, float]] = {}
    for hazard in hazards:
        scoped = [row for row, case in zip(rows, cases, strict=True) if case.hazard_type == hazard]
        by_hazard[hazard] = {
            "cases": float(len(scoped)),
            "hazard_accuracy": _rate(sum(1 for row in scoped if row.hazard_correct), len(scoped)),
            "level_accuracy": _rate(sum(1 for row in scoped if row.level_correct), len(scoped)),
        }
    by_form: list[dict[str, Any]] = []
    for form in dict.fromkeys(case.form for case in cases):
        scoped = [row for row, case in zip(rows, cases, strict=True) if case.form == form]
        by_form.append(
            {
                "form": form,
                "cases": len(scoped),
                "level_accuracy": _rate(sum(1 for row in scoped if row.level_correct), len(scoped)),
                "hazard_accuracy": _rate(sum(1 for row in scoped if row.hazard_correct), len(scoped)),
            }
        )

    official = dataset.kind == "field" and total >= MIN_CASES
    note = "本结果不构成官方口径：数据集为合成回归集" if not official else "现场标注数据集，可按指标口径引用"
    return EvalReport(
        dataset_kind=dataset.kind,
        dataset_source=dataset.source,
        dataset_note=dataset.note,
        cases=total,
        hazards_covered=tuple(hazards),
        hazard_accuracy=_rate(sum(1 for row in rows if row.hazard_correct), total),
        region_accuracy=_rate(sum(1 for row in rows if row.region_correct), total),
        metric_precision=_rate(found_metrics, extracted),
        metric_recall=_rate(found_metrics, expected_metrics),
        level_accuracy=_rate(sum(1 for row in rows if row.level_correct), total),
        by_hazard=by_hazard,
        by_form=by_form,
        official_claim=official,
        note=(note + note_suffix) if note_suffix else note,
        outcomes=rows,
    )


def _rate(numerator: int, denominator: int) -> float:
    return 0.0 if denominator <= 0 else round(numerator / denominator, 4)


async def evaluate(dataset: EvalDataset, *, with_llm: bool = False, llm: Any | None = None, evidence: Any | None = None) -> EvalReport:
    """跑一遍解析评测。默认只量确定性腿（规则 + 词表），带 `llm` 时才把裁决腿接上。"""
    from aegis.services.semantic_parser import DisasterTextParser

    parser = DisasterTextParser(llm=llm if with_llm else None, evidence=evidence if with_llm else None)
    outcomes: list[CaseOutcome] = []
    for case in dataset.cases:
        parsed = await parser.parse(case.text, region_code=case.region_code)
        outcomes.append(score_case(case, parsed))
    note = "（含 LLM 裁决腿）" if with_llm else "（仅规则/词腿，LLM 与佐证未参与）"
    return score_dataset(dataset.cases, outcomes, dataset=dataset, note_suffix=note)


def lexical_baseline(text: str) -> dict[str, Any]:
    """词表侧的三件抽取结果。给"去掉整条解析链、只看词表"的对照实验用。"""
    return {
        "hazard_type": None if (hazard := detect_hazard(text)) is None else hazard.value,
        "region_code": extract_region(text),
        "metrics": {metric: value for metric, value, _unit in extract_metrics(text)},
        "risk_level": None if (level := detect_level(text)) is None else int(level),
    }


__all__ = [
    "METRIC_TOLERANCE",
    "MIN_CASES",
    "MIN_HAZARDS",
    "CaseOutcome",
    "EvalCase",
    "EvalDataset",
    "EvalDatasetError",
    "EvalReport",
    "evaluate",
    "lexical_baseline",
    "load_dataset",
    "parse_case",
    "score_case",
    "score_dataset",
]
