"""预警准确率回放的报表出口：`scripts.metrics_report --dataset`。

`persistence/replay.py` 早就把口径定死了（分母为零返回 None、合成集永远不是 measured），
但它没有任何生产入口——准确率因此只能停在"未测得"，现场拿到标注集也没有跑的地方。
这里补的就是那段路，并钉住三件容易被报表美化掉的事：
1. 没给数据集 → 结论是"未量测"，不是 0.0，也不是任何看起来像达标的话；
2. 合成数据集 → 只证明算术，`official_accuracy` 必须是 None；
3. 现场标注样本不足 → 判"样本不足"，不拿小样本冒充达成率。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from scripts.metrics_report import (
    ACCURACY_HINT,
    ACCURACY_INDICATOR,
    NOT_MEASURED,
    TYPE_ACCURACY_INDICATOR,
    accuracy_report,
    accuracy_rows,
)

from aegis.persistence.errors import ReplayDatasetError


def write_dataset(
    path: Path,
    *,
    kind: str,
    cases: list[dict[str, Any]],
    source: str = "pytest 夹具",
    note: str = "仅用于验证算式",
) -> Path:
    meta = {"dataset": {"kind": kind, "source": source, "note": note}}
    lines = [json.dumps(meta, ensure_ascii=False)]
    lines.extend(json.dumps(case, ensure_ascii=False) for case in cases)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def case(index: int, *, truth: bool, predicted: bool, hazard: str = "debris_flow", type_ok: bool = True) -> dict[str, Any]:
    return {
        "case_id": f"c{index:03d}",
        "hazard_type": hazard,
        "region_code": "540121",
        "truth_warning": truth,
        "predicted_warning": predicted,
        "predicted_hazard_type": hazard if type_ok else "landslide",
    }


def mixed(n_tp: int, n_fp: int, n_fn: int, n_tn: int, *, type_ok: bool = True) -> list[dict[str, Any]]:
    index = 0
    rows: list[dict[str, Any]] = []
    for truth, predicted, count in ((True, True, n_tp), (False, True, n_fp), (True, False, n_fn), (False, False, n_tn)):
        for _ in range(count):
            index += 1
            rows.append(case(index, truth=truth, predicted=predicted, type_ok=type_ok))
    return rows


class TestNoDatasetStaysUnmeasured:
    def test_missing_dataset_yields_no_number_at_all(self) -> None:
        report = accuracy_report(None)
        assert (report.status, report.indicator) == ("not_measured", "not_measured")
        assert report.official_accuracy is None
        assert report.confusion.accuracy is None
        assert report.confusion.total == 0

    def test_unmeasured_row_is_a_text_judgement_not_a_zero(self) -> None:
        row = accuracy_rows(accuracy_report(None))[0]
        assert row["指标"] == ACCURACY_INDICATOR
        assert row["准确率"] is None
        assert "未测得" in str(row["判定"])

    def test_hint_points_at_the_flag_that_enables_it(self) -> None:
        assert ACCURACY_INDICATOR in NOT_MEASURED
        assert "--dataset" in ACCURACY_HINT


class TestSyntheticDatasetCannotPass:
    def test_synthetic_kind_never_reaches_official_accuracy(self, tmp_path: Path) -> None:
        dataset = write_dataset(tmp_path / "s.jsonl", kind="synthetic", cases=mixed(40, 0, 0, 10))
        report = accuracy_report(dataset)
        assert (report.status, report.indicator) == ("not_measured", "synthetic_only")
        assert report.official_accuracy is None
        # 算术仍然算得出来：合成集证明的是实现，不是指标
        assert report.confusion.accuracy == pytest.approx(50 / 50)

    def test_row_labels_the_run_as_arithmetic_only(self, tmp_path: Path) -> None:
        dataset = write_dataset(tmp_path / "s.jsonl", kind="synthetic", cases=mixed(20, 5, 5, 20))
        row = accuracy_rows(accuracy_report(dataset))[0]
        assert "合成" in str(row["判定"]) and "不构成官方口径证据" in str(row["判定"])
        assert row["数据集"]["kind"] == "synthetic"
        assert row["官方口径准确率"] is None


class TestFieldDataset:
    def test_enough_field_cases_and_good_accuracy_pass(self, tmp_path: Path) -> None:
        dataset = write_dataset(tmp_path / "f.jsonl", kind="field", cases=mixed(28, 2, 5, 30), source="某流域 2025 复盘")
        report = accuracy_report(dataset)
        assert (report.status, report.indicator) == ("measured", "met")
        assert report.official_accuracy == pytest.approx((28 + 30) / 65)
        row = accuracy_rows(report)[0]
        assert row["判定"] == "达标"
        assert row["数据集"]["source"] == "某流域 2025 复盘"

    def test_poor_accuracy_is_reported_as_a_miss(self, tmp_path: Path) -> None:
        dataset = write_dataset(tmp_path / "f.jsonl", kind="field", cases=mixed(10, 30, 5, 5))
        report = accuracy_report(dataset)
        assert (report.status, report.indicator) == ("measured", "not_met")
        assert accuracy_rows(report)[0]["判定"] == "未达标"
        # 判"未达标"时 F1 必须是数字而不是 None：预测全错不等于没测
        assert report.confusion.f1 is not None

    def test_small_field_sample_is_insufficient_not_a_rate(self, tmp_path: Path) -> None:
        dataset = write_dataset(tmp_path / "f.jsonl", kind="field", cases=mixed(8, 0, 2, 5))
        report = accuracy_report(dataset)
        assert (report.status, report.indicator) == ("insufficient_sample", "not_measured")
        assert "样本不足" in str(accuracy_rows(report)[0]["判定"])
        assert report.official_accuracy is None

    def test_target_override_only_changes_the_verdict(self, tmp_path: Path) -> None:
        dataset = write_dataset(tmp_path / "f.jsonl", kind="field", cases=mixed(20, 5, 5, 20))
        assert accuracy_report(dataset).indicator == "met"
        assert accuracy_report(dataset, target=0.99).indicator == "not_met"

    def test_illegal_target_is_rejected(self, tmp_path: Path) -> None:
        dataset = write_dataset(tmp_path / "f.jsonl", kind="field", cases=mixed(1, 0, 0, 1))
        with pytest.raises(ReplayDatasetError):
            accuracy_report(dataset, target=0.0)
        with pytest.raises(ReplayDatasetError):
            accuracy_report(dataset, target=1.5)


class TestTypeAccuracyIsSeparate:
    def test_type_row_only_uses_true_positives(self, tmp_path: Path) -> None:
        dataset = write_dataset(tmp_path / "f.jsonl", kind="field", cases=mixed(32, 4, 4, 20, type_ok=False))
        report = accuracy_report(dataset)
        rows = {str(row["指标"]): row for row in accuracy_rows(report)}
        type_row = rows[TYPE_ACCURACY_INDICATOR]
        assert type_row["样本"] == 32 and type_row["灾种报对"] == 0
        assert float(type_row["判定"]) == 0.0

    def test_no_true_positive_means_unmeasured_type_accuracy(self, tmp_path: Path) -> None:
        dataset = write_dataset(tmp_path / "f.jsonl", kind="field", cases=mixed(0, 6, 24, 10))
        rows = {str(row["指标"]): row for row in accuracy_rows(accuracy_report(dataset))}
        assert "未测得" in str(rows[TYPE_ACCURACY_INDICATOR]["判定"])


class TestDatasetIntegrity:
    def test_broken_case_line_fails_loudly(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.jsonl"
        path.write_text('{"dataset": {"kind": "field"}}\n{"case_id": "c1", "truth_warning": true}\n', encoding="utf-8")
        with pytest.raises(ReplayDatasetError) as excinfo:
            accuracy_report(path)
        assert "缺字段" in str(excinfo.value)

    def test_missing_file_raises_instead_of_becoming_not_measured(self, tmp_path: Path) -> None:
        """路径写错不能悄悄退化成"没测"：那会让一次不存在的量测看起来像一次诚实的空结论。"""
        with pytest.raises(ReplayDatasetError) as excinfo:
            accuracy_report(tmp_path / "nope.jsonl")
        assert "不存在" in str(excinfo.value)

    def test_dataset_without_cases_is_not_measured(self, tmp_path: Path) -> None:
        dataset = write_dataset(tmp_path / "e.jsonl", kind="field", cases=[])
        report = accuracy_report(dataset)
        assert (report.status, report.indicator) == ("not_measured", "not_measured")

    def test_provenance_warning_travels_with_the_dict(self, tmp_path: Path) -> None:
        synthetic = accuracy_report(write_dataset(tmp_path / "s.jsonl", kind="synthetic", cases=mixed(30, 0, 0, 10))).as_dict()
        assert synthetic["provenance_warning"]
        assert "不构成" in str(synthetic["provenance_warning"])
