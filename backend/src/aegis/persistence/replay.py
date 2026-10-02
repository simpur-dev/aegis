"""预警准确率回放（离线）：读 JSONL 案例集，算混淆矩阵与派生指标。

口径在此唯一确定，报表侧不重算：
- truth_warning 为真值（该案例是否应当预警），predicted_warning 为系统实际产出；
- TP/FP/FN/TN 按"该不该报"判定；灾种是否报对单列为 `type_correct`（只统计 TP），
  因为"报了"和"报对了灾种"是两件事，合成一个数字就会掩盖其中一件；
- 分母为零的比率一律返回 None，不返回 0.0 —— "无从判断"与"判定为差"必须可区分。

完整性约束（本模块的设计前提）：官方"多灾种预警准确率 ≥80%"只能在
`dataset_kind == "field"`（真实现场标注）时给出结论；合成样例只能证明**算术**，
`indicator` 因此被固定为 `synthetic_only`，`official_accuracy` 为 None。
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from aegis.config import get_settings
from aegis.persistence.errors import ReplayDatasetError

DatasetKind = Literal["field", "synthetic", "unspecified"]
Status = Literal["not_measured", "measured", "insufficient_sample"]
Indicator = Literal["not_measured", "synthetic_only", "met", "not_met"]

MIN_FIELD_CASES = 30
_ALLOWED_KINDS = frozenset({"field", "synthetic", "unspecified"})


@dataclass(frozen=True, slots=True)
class ReplayCase:
    """一个回放案例：某区域某灾种在一个观测窗口的真值与系统产出。"""

    case_id: str
    hazard_type: str
    region_code: str
    truth_warning: bool
    predicted_warning: bool
    predicted_hazard_type: str | None = None
    truth_level: int | None = None
    predicted_level: int | None = None
    lead_seconds: float | None = None

    @property
    def cell(self) -> Literal["tp", "fp", "fn", "tn"]:
        if self.truth_warning:
            return "tp" if self.predicted_warning else "fn"
        return "fp" if self.predicted_warning else "tn"

    @property
    def type_correct(self) -> bool:
        """灾种是否报对：只对 TP 有定义（未报警无所谓对错）。"""
        return self.cell == "tp" and self.predicted_hazard_type == self.hazard_type


@dataclass(frozen=True, slots=True)
class Confusion:
    tp: int = 0
    fp: int = 0
    fn: int = 0
    tn: int = 0

    @property
    def total(self) -> int:
        return self.tp + self.fp + self.fn + self.tn

    @property
    def predicted_positive(self) -> int:
        return self.tp + self.fp

    @property
    def actual_positive(self) -> int:
        return self.tp + self.fn

    @property
    def precision(self) -> float | None:
        return None if self.predicted_positive == 0 else self.tp / self.predicted_positive

    @property
    def recall(self) -> float | None:
        return None if self.actual_positive == 0 else self.tp / self.actual_positive

    @property
    def accuracy(self) -> float | None:
        return None if self.total == 0 else (self.tp + self.tn) / self.total

    @property
    def f1(self) -> float | None:
        precision, recall = self.precision, self.recall
        if precision is None or recall is None:
            return None
        total = precision + recall
        # 两侧都已定义为 0（预测全错）时 F1 是 0.0 而非"未量测"：
        # 返回 None 会把"很差"误标成"没测"，报表就分不清这两种结论了。
        return 0.0 if total == 0 else 2 * precision * recall / total

    def as_dict(self) -> dict[str, Any]:
        return {
            "tp": self.tp,
            "fp": self.fp,
            "fn": self.fn,
            "tn": self.tn,
            "total": self.total,
            "precision": self.precision,
            "recall": self.recall,
            "accuracy": self.accuracy,
            "f1": self.f1,
        }


@dataclass(frozen=True, slots=True)
class ReplayDataset:
    cases: tuple[ReplayCase, ...]
    kind: DatasetKind
    source: str
    note: str

    @property
    def empty(self) -> bool:
        return not self.cases


@dataclass(frozen=True, slots=True)
class ReplayReport:
    status: Status
    indicator: Indicator
    dataset_kind: DatasetKind
    dataset_source: str
    dataset_note: str
    confusion: Confusion
    per_hazard: Mapping[str, Confusion]
    type_correct: int
    target: float
    min_field_cases: int

    @property
    def official_accuracy(self) -> float | None:
        """官方口径准确率：非现场标注数据集一律返回 None。"""
        if self.status != "measured":
            return None
        return self.confusion.accuracy

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "indicator": self.indicator,
            "target": self.target,
            "official_accuracy": self.official_accuracy,
            "measured_accuracy": self.confusion.accuracy,
            "dataset": {
                "kind": self.dataset_kind,
                "source": self.dataset_source,
                "note": self.dataset_note,
                "cases": self.confusion.total,
                "min_field_cases": self.min_field_cases,
            },
            "overall": self.confusion.as_dict(),
            "per_hazard": {hazard: cell.as_dict() for hazard, cell in sorted(self.per_hazard.items())},
            "type_correct_of_tp": self.type_correct,
            "provenance_warning": None
            if self.status == "measured"
            else "本结果不构成官方'预警准确率 ≥80%'的证据：数据集非现场标注（或未提供数据集）",
        }


def _as_bool(raw: object, *, field: str, line_no: int) -> bool:
    if isinstance(raw, bool):
        return raw
    raise ReplayDatasetError(f"第 {line_no} 行的 {field} 必须是布尔真值", detail={"field": field, "line": line_no})


def _as_optional_float(raw: object, *, field: str, line_no: int) -> float | None:
    if raw is None:
        return None
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ReplayDatasetError(f"第 {line_no} 行的 {field} 不是数值", detail={"field": field, "line": line_no}) from exc
    if value != value:
        raise ReplayDatasetError(f"第 {line_no} 行的 {field} 为 NaN", detail={"field": field, "line": line_no})
    return value


def _as_optional_level(raw: object, *, field: str, line_no: int) -> int | None:
    if raw is None:
        return None
    value = int(_as_optional_float(raw, field=field, line_no=line_no) or 0)
    if not 1 <= value <= 5:
        raise ReplayDatasetError(f"第 {line_no} 行的 {field} 越界（1..5）", detail={"field": field, "line": line_no})
    return value


def parse_case(raw: Mapping[str, Any], *, line_no: int) -> ReplayCase:
    """解析一行案例。字段名即契约口径：truth_warning / predicted_warning。"""
    missing = [key for key in ("case_id", "hazard_type", "region_code", "truth_warning", "predicted_warning") if key not in raw]
    if missing:
        raise ReplayDatasetError(f"第 {line_no} 行缺字段：{', '.join(missing)}", detail={"line": line_no, "missing": missing})
    case_id = str(raw["case_id"]).strip()
    if not case_id:
        raise ReplayDatasetError(f"第 {line_no} 行 case_id 为空", detail={"line": line_no})
    return ReplayCase(
        case_id=case_id,
        hazard_type=str(raw["hazard_type"]).strip(),
        region_code=str(raw["region_code"]).strip(),
        truth_warning=_as_bool(raw["truth_warning"], field="truth_warning", line_no=line_no),
        predicted_warning=_as_bool(raw["predicted_warning"], field="predicted_warning", line_no=line_no),
        predicted_hazard_type=None if raw.get("predicted_hazard_type") is None else str(raw["predicted_hazard_type"]),
        truth_level=_as_optional_level(raw.get("truth_level"), field="truth_level", line_no=line_no),
        predicted_level=_as_optional_level(raw.get("predicted_level"), field="predicted_level", line_no=line_no),
        lead_seconds=_as_optional_float(raw.get("lead_seconds"), field="lead_seconds", line_no=line_no),
    )


def case_from_row(row: Mapping[str, Any], *, line_no: int) -> ReplayCase:
    """库里"真值 + 窗口内产出"的配对行 -> 案例：判定字段一个都不自己算，全部走 `parse_case`。

    存在的理由只有一个：让 Postgres 侧读出来的东西与 JSONL 侧走**同一套**口径。
    在 SQL 里再算一遍 TP/FP 就会有两个"预警准确率"，两边不一致时没人能看出谁在骗人。

    `predicted_generated_at` 为空即窗口内没有任何预警，也就是漏报（False）而不是未知：
    把漏报写成 None 会让分母悄悄变小，准确率反而显得更高。
    """
    generated_at = row.get("predicted_generated_at")
    observed_at = row.get("observed_at")
    lead_seconds = None
    if isinstance(generated_at, datetime) and isinstance(observed_at, datetime):
        lead_seconds = (generated_at - observed_at).total_seconds()
    return parse_case(
        {
            "case_id": row["case_id"],
            "hazard_type": row["hazard_type"],
            "region_code": row["region_code"],
            "truth_warning": row["truth_warning"],
            "predicted_warning": generated_at is not None,
            "predicted_hazard_type": row.get("predicted_hazard_type"),
            "truth_level": row.get("truth_level"),
            "predicted_level": row.get("predicted_level"),
            "lead_seconds": lead_seconds,
        },
        line_no=line_no,
    )


def parse_dataset(text: str) -> ReplayDataset:
    """解析 JSONL：以 `{"dataset": {...}}` 行为数据集元信息，`{"case_id": ...}` 行为案例。"""
    cases: list[ReplayCase] = []
    kind: DatasetKind = "unspecified"
    source = ""
    note = "未声明数据来源"
    seen: set[str] = set()
    for line_no, line in enumerate((row for row in text.splitlines() if row.strip()), start=1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        try:
            raw = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ReplayDatasetError(f"第 {line_no} 行不是合法 JSON", detail={"line": line_no}) from exc
        if not isinstance(raw, dict):
            raise ReplayDatasetError(f"第 {line_no} 行不是对象", detail={"line": line_no})
        if "dataset" in raw:
            meta = raw["dataset"]
            if not isinstance(meta, dict):
                raise ReplayDatasetError("dataset 元信息必须是对象", detail={"line": line_no})
            declared = str(meta.get("kind", "unspecified"))
            if declared not in _ALLOWED_KINDS:
                raise ReplayDatasetError(f"dataset.kind 只允许 {sorted(_ALLOWED_KINDS)}", detail={"line": line_no, "actual": declared})
            kind = declared  # type: ignore[assignment]
            source = str(meta.get("source", ""))
            note = str(meta.get("note", ""))
            continue
        case = parse_case(raw, line_no=line_no)
        if case.case_id in seen:
            raise ReplayDatasetError(f"case_id 重复：{case.case_id}", detail={"line": line_no})
        seen.add(case.case_id)
        cases.append(case)
    return ReplayDataset(cases=tuple(cases), kind=kind, source=source, note=note)


def load_dataset(path: str | Path) -> ReplayDataset:
    file = Path(path)
    if not file.is_file():
        raise ReplayDatasetError("回放数据集不存在", detail={"path": str(file)})
    try:
        return parse_dataset(file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ReplayDatasetError("回放数据集不是合法 JSONL", detail={"path": str(file)}) from exc


def tally(cases: Iterable[ReplayCase]) -> Confusion:
    values = {"tp": 0, "fp": 0, "fn": 0, "tn": 0}
    for case in cases:
        values[case.cell] += 1
    return Confusion(**values)


def per_hazard_tally(cases: Iterable[ReplayCase]) -> dict[str, Confusion]:
    grouped: dict[str, list[ReplayCase]] = {}
    for case in cases:
        grouped.setdefault(case.hazard_type, []).append(case)
    return {hazard: tally(items) for hazard, items in grouped.items()}


def measure(
    dataset: ReplayDataset | None,
    *,
    target: float | None = None,
    min_field_cases: int = MIN_FIELD_CASES,
) -> ReplayReport:
    """把数据集算成报表。`dataset` 为 None 或无案例 -> status=not_measured，全部比率为 None。"""
    threshold = get_settings().sla_warning_accuracy if target is None else float(target)
    if not 0.0 < threshold <= 1.0:
        raise ReplayDatasetError("准确率目标取值区间为 (0, 1]", detail={"target": threshold})
    if dataset is None or dataset.empty:
        return ReplayReport(
            status="not_measured",
            indicator="not_measured",
            dataset_kind="unspecified" if dataset is None else dataset.kind,
            dataset_source="" if dataset is None else dataset.source,
            dataset_note="未提供数据集：指标量测未执行" if dataset is None else "数据集无案例",
            confusion=Confusion(),
            per_hazard={},
            type_correct=0,
            target=threshold,
            min_field_cases=min_field_cases,
        )
    cases = dataset.cases
    overall = tally(cases)
    accuracy = overall.accuracy
    # status 只表达"是否用真实现场标注量测过"：合成集永远不是 measured，
    # 否则下游报表会把一次算术演练读成指标达成。
    if dataset.kind != "field":
        status: Status = "not_measured"
        indicator: Indicator = "synthetic_only"
    elif overall.total < min_field_cases:
        status = "insufficient_sample"
        indicator = "not_measured"
    else:
        status = "measured"
        indicator = "met" if accuracy is not None and accuracy >= threshold else "not_met"
    return ReplayReport(
        status=status,
        indicator=indicator,
        dataset_kind=dataset.kind,
        dataset_source=dataset.source,
        dataset_note=dataset.note,
        confusion=overall,
        per_hazard=per_hazard_tally(cases),
        type_correct=sum(1 for case in cases if case.type_correct),
        target=threshold,
        min_field_cases=min_field_cases,
    )


def replay(
    path: str | Path | None = None,
    *,
    target: float | None = None,
    min_field_cases: int = MIN_FIELD_CASES,
) -> ReplayReport:
    """回放入口：不给路径就是"未量测"，绝不返回一个凭空算出的准确率。"""
    if path is None:
        return measure(None, target=target, min_field_cases=min_field_cases)
    return measure(load_dataset(path), target=target, min_field_cases=min_field_cases)
