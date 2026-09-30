"""预警准确率回放量测的边界测试。

这里只验证**算术与口径**：案例全部由测试自己构造，不代表任何真实现场测量结果。
关键不变式是诚实性：合成数据集永远不得被判为"指标达成"，未给数据集时不得凭空产出准确率。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from aegis.persistence.errors import ReplayDatasetError
from aegis.persistence.replay import (
    MIN_FIELD_CASES,
    Confusion,
    ReplayCase,
    ReplayDataset,
    load_dataset,
    measure,
    parse_case,
    parse_dataset,
    per_hazard_tally,
    replay,
    tally,
)


def case(
    case_id: str,
    *,
    truth: bool = True,
    predicted: bool = True,
    hazard: str = "debris_flow",
    predicted_hazard: str | None = "debris_flow",
    truth_level: int | None = 3,
    predicted_level: int | None = 3,
    lead: float | None = 300.0,
) -> ReplayCase:
    return ReplayCase(
        case_id=case_id,
        hazard_type=hazard,
        region_code="540102",
        truth_warning=truth,
        predicted_warning=predicted,
        predicted_hazard_type=predicted_hazard,
        truth_level=truth_level,
        predicted_level=predicted_level,
        lead_seconds=lead,
    )


def dataset_of(*cases: ReplayCase, kind: str = "synthetic", source: str = "unit-test") -> ReplayDataset:
    return ReplayDataset(cases=cases, kind=kind, source=source, note="测试构造，非现场测量")  # type: ignore[arg-type]


class TestConfusionArithmetic:
    def test_empty_confusion_reports_no_ratios_instead_of_zero(self) -> None:
        """分母为零时必须是 None：把"没测"渲染成 0.0 会被下游读成"准确率为零"。"""
        c = Confusion()
        assert c.total == 0
        assert c.precision is None
        assert c.recall is None
        assert c.accuracy is None
        assert c.f1 is None
        assert c.predicted_positive == 0
        assert c.actual_positive == 0

    def test_all_negative_still_has_accuracy_but_no_precision(self) -> None:
        c = Confusion(tn=10)
        assert c.accuracy == 1.0
        assert c.precision is None  # 没有任何预测为正
        assert c.recall is None  # 没有任何真实为正

    def test_all_positive_predictions_with_no_truth_gives_recall_none(self) -> None:
        c = Confusion(fp=4)
        assert c.precision == 0.0
        assert c.recall is None
        assert c.accuracy == 0.0

    def test_f1_is_harmonic_mean_of_precision_and_recall(self) -> None:
        c = Confusion(tp=3, fp=1, fn=1)
        assert c.precision == pytest.approx(0.75)
        assert c.recall == pytest.approx(0.75)
        assert c.f1 == pytest.approx(0.75)

    def test_f1_is_zero_when_either_leg_is_zero(self) -> None:
        assert Confusion(tp=0, fp=2, fn=3, tn=5).f1 == 0.0

    def test_as_dict_exposes_cells_and_ratios_together(self) -> None:
        payload = Confusion(tp=1, tn=1).as_dict()
        assert payload["tp"] == 1 and payload["tn"] == 1
        assert payload["total"] == 2
        assert payload["accuracy"] == 1.0
        assert "budget_ms" not in payload or payload["budget_ms"] is None


class TestCaseCell:
    @pytest.mark.parametrize(
        ("truth", "predicted", "cell"),
        [(True, True, "tp"), (False, True, "fp"), (True, False, "fn"), (False, False, "tn")],
    )
    def test_four_quadrants(self, truth: bool, predicted: bool, cell: str) -> None:
        assert case("c", truth=truth, predicted=predicted).cell == cell

    def test_type_correct_requires_both_levels_present_and_equal(self) -> None:
        assert case("a", predicted_hazard="debris_flow").type_correct
        assert not case("b", predicted_hazard="snow_avalanche").type_correct
        assert not case("c", predicted_hazard=None).type_correct

    def test_type_correct_is_false_when_truth_absent(self) -> None:
        c = ReplayCase(
            case_id="x",
            hazard_type="",
            region_code="540102",
            truth_warning=False,
            predicted_warning=False,
            predicted_hazard_type=None,
        )
        assert not c.type_correct


class TestTally:
    def test_tally_counts_every_case_exactly_once(self) -> None:
        cases = [
            case("tp", truth=True, predicted=True),
            case("fp", truth=False, predicted=True),
            case("fn", truth=True, predicted=False),
            case("tn", truth=False, predicted=False),
        ]
        c = tally(cases)
        assert (c.tp, c.fp, c.fn, c.tn) == (1, 1, 1, 1)
        assert c.total == 4

    def test_tally_of_empty_input_is_zero_cells(self) -> None:
        assert tally([]).total == 0

    def test_per_hazard_tally_groups_by_true_hazard_type(self) -> None:
        cases = [
            case("a", hazard="debris_flow"),
            case("b", hazard="debris_flow", truth=False, predicted=True),
            case("c", hazard="glacial_lake_outburst"),
        ]
        grouped = per_hazard_tally(cases)
        assert set(grouped) == {"debris_flow", "glacial_lake_outburst"}
        assert grouped["debris_flow"].tp == 1 and grouped["debris_flow"].fp == 1
        assert grouped["glacial_lake_outburst"].total == 1

    def test_per_hazard_tally_is_empty_for_no_cases(self) -> None:
        assert per_hazard_tally([]) == {}


class TestParsing:
    def test_minimal_case_line_parses_with_optional_fields_none(self) -> None:
        c = parse_case(
            {"case_id": "c1", "hazard_type": "debris_flow", "region_code": "540102", "truth_warning": True, "predicted_warning": False},
            line_no=3,
        )
        assert c.cell == "fn"
        assert c.lead_seconds is None
        assert c.predicted_level is None

    @pytest.mark.parametrize(
        "raw",
        [
            {"hazard_type": "debris_flow", "region_code": "540102", "truth_warning": True, "predicted_warning": False},
            {"case_id": "", "hazard_type": "debris_flow", "region_code": "540102", "truth_warning": True, "predicted_warning": False},
            {"case_id": "c", "region_code": "540102", "truth_warning": True, "predicted_warning": False},
            {"case_id": "c", "hazard_type": "debris_flow", "truth_warning": True, "predicted_warning": False},
            {"case_id": "c", "hazard_type": "debris_flow", "region_code": "540102", "truth_warning": "yes", "predicted_warning": False},
            {
                "case_id": "c",
                "hazard_type": "debris_flow",
                "region_code": "540102",
                "truth_warning": True,
                "predicted_warning": False,
                "truth_level": 9,
            },
        ],
    )
    def test_rejects_missing_or_out_of_range_fields_with_line_number(self, raw: dict[str, Any]) -> None:
        with pytest.raises(ReplayDatasetError) as excinfo:
            parse_case(raw, line_no=7)
        assert excinfo.value.detail.get("line") == 7

    def test_dataset_metadata_line_is_not_counted_as_a_case(self) -> None:
        text = "\n".join(
            [
                '{"dataset": {"kind": "field", "source": "林芝观测点", "note": "2026 汛期"}}',
                "# 注释行应被跳过",
                "",
                '{"case_id":"c1","hazard_type":"debris_flow","region_code":"540102","truth_warning":true,"predicted_warning":true}',
            ]
        )
        ds = parse_dataset(text)
        assert ds.kind == "field"
        assert ds.source == "林芝观测点"
        assert len(ds.cases) == 1

    def test_default_kind_is_unspecified_and_marked_not_measured(self) -> None:
        ds = parse_dataset(
            '{"case_id":"c1","hazard_type":"debris_flow","region_code":"540102","truth_warning":true,"predicted_warning":true}'
        )
        assert ds.kind == "unspecified"
        assert "未声明" in ds.note

    @pytest.mark.parametrize(
        "text",
        [
            "not json at all",
            "[1, 2]",
            '{"case_id":"c1","hazard_type":"debris_flow","region_code":"540102","truth_warning":true,"predicted_warning":true}\n{"case_id":"c1","hazard_type":"debris_flow","region_code":"540102","truth_warning":true,"predicted_warning":true}',
            '{"dataset": "kind=field"}',
            '{"dataset": {"kind": "guess"}}',
        ],
    )
    def test_malformed_datasets_are_rejected(self, text: str) -> None:
        with pytest.raises(ReplayDatasetError):
            parse_dataset(text)

    def test_empty_text_yields_empty_dataset_not_error(self) -> None:
        ds = parse_dataset("\n  \n")
        assert ds.empty

    def test_load_dataset_reports_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(ReplayDatasetError, match="不存在"):
            load_dataset(tmp_path / "nope.jsonl")

    def test_load_dataset_reads_utf8_file(self, tmp_path: Path) -> None:
        f = tmp_path / "cases.jsonl"
        f.write_text(
            '{"case_id":"c1","hazard_type":"泥石流","region_code":"540102","truth_warning":true,"predicted_warning":true}', encoding="utf-8"
        )
        assert len(load_dataset(f).cases) == 1


class TestMeasureHonesty:
    def test_no_dataset_is_not_measured_and_carries_no_ratio(self) -> None:
        """不给数据集时绝不凭空产出准确率 —— 这是报表可信度的底线。"""
        report = measure(None)
        assert report.status == "not_measured"
        assert report.indicator == "not_measured"
        assert report.confusion.accuracy is None
        assert report.confusion.total == 0

    def test_empty_dataset_is_not_measured(self) -> None:
        report = measure(ReplayDataset(cases=(), kind="field", source="s", note="n"))
        assert report.status == "not_measured"
        assert "无案例" in report.dataset_note

    def test_synthetic_dataset_can_never_report_indicator_met(self) -> None:
        """核心诚实性不变式：合成集算得再准也只能是 synthetic_only。

        否则一次算术演练就会被下游读成"≥80% 准确率已达成"。
        """
        perfect = dataset_of(*[case(f"c{i}", truth=i % 2 == 0, predicted=i % 2 == 0) for i in range(50)], kind="synthetic")
        report = measure(perfect, target=0.8)
        assert report.confusion.accuracy == 1.0
        assert report.status == "not_measured"
        assert report.indicator == "synthetic_only"

    def test_unspecified_kind_is_also_treated_as_unmeasured(self) -> None:
        report = measure(dataset_of(case("a"), kind="unspecified"), target=0.8)
        assert report.indicator == "synthetic_only"

    def test_field_dataset_below_min_cases_is_insufficient_sample(self) -> None:
        report = measure(dataset_of(*[case(f"c{i}") for i in range(MIN_FIELD_CASES - 1)], kind="field"), target=0.8)
        assert report.status == "insufficient_sample"
        assert report.indicator == "not_measured"
        assert report.min_field_cases == MIN_FIELD_CASES

    def test_field_dataset_at_boundary_is_measured_and_met(self) -> None:
        report = measure(dataset_of(*[case(f"c{i}") for i in range(MIN_FIELD_CASES)], kind="field"), target=0.8)
        assert report.status == "measured"
        assert report.indicator == "met"
        assert report.confusion.accuracy == 1.0

    def test_field_dataset_below_target_is_not_met(self) -> None:
        cases = [case(f"c{i}", truth=True, predicted=i % 5 == 0) for i in range(MIN_FIELD_CASES)]
        report = measure(dataset_of(*cases, kind="field"), target=0.8)
        assert report.status == "measured"
        assert report.indicator == "not_met"
        assert report.confusion.accuracy is not None and report.confusion.accuracy < 0.8

    def test_target_defaults_to_settings_and_is_validated(self) -> None:
        assert 0.0 < measure(None).target <= 1.0
        with pytest.raises(ReplayDatasetError, match="目标"):
            measure(None, target=0.0)
        with pytest.raises(ReplayDatasetError, match="目标"):
            measure(None, target=1.5)

    def test_per_hazard_breakdown_is_present_for_measured_report(self) -> None:
        cases = [case(f"d{i}", hazard="debris_flow") for i in range(20)] + [
            case(f"g{i}", hazard="glacial_lake_outburst") for i in range(20)
        ]
        report = measure(dataset_of(*cases, kind="field"), target=0.8)
        assert set(report.per_hazard) == {"debris_flow", "glacial_lake_outburst"}
        assert sum(c.total for c in report.per_hazard.values()) == 40

    def test_type_correct_counts_only_hazard_type_matches(self) -> None:
        cases = [
            case("a", predicted_hazard="debris_flow"),
            case("b", predicted_hazard="snow_avalanche"),
        ]
        report = measure(dataset_of(*cases, kind="field"), target=0.8)
        assert report.type_correct == 1


class TestReplayEntryPoint:
    def test_replay_without_path_is_not_measured(self) -> None:
        assert replay().status == "not_measured"

    def test_replay_with_path_measures_the_file(self, tmp_path: Path) -> None:
        lines = ['{"dataset": {"kind": "field", "source": "t", "note": "t"}}']
        lines += [
            json.dumps(
                {
                    "case_id": f"c{i}",
                    "hazard_type": "debris_flow",
                    "region_code": "540102",
                    "truth_warning": True,
                    "predicted_warning": True,
                    "predicted_hazard_type": "debris_flow",
                }
            )
            for i in range(MIN_FIELD_CASES)
        ]
        f = tmp_path / "field.jsonl"
        f.write_text("\n".join(lines), encoding="utf-8")
        report = replay(f, target=0.8)
        assert report.status == "measured"
        assert report.indicator == "met"

    def test_replay_propagates_dataset_errors(self, tmp_path: Path) -> None:
        f = tmp_path / "bad.jsonl"
        f.write_text("{oops}", encoding="utf-8")
        with pytest.raises(ReplayDatasetError):
            replay(f)


@settings(max_examples=80, deadline=None)
@given(
    tp=st.integers(0, 40),
    fp=st.integers(0, 40),
    fn=st.integers(0, 40),
    tn=st.integers(0, 40),
)
def test_confusion_ratios_are_always_bounded_or_none(tp: int, fp: int, fn: int, tn: int) -> None:
    c = Confusion(tp=tp, fp=fp, fn=fn, tn=tn)
    assert c.total == tp + fp + fn + tn
    for ratio in (c.precision, c.recall, c.accuracy, c.f1):
        assert ratio is None or 0.0 <= ratio <= 1.0
    if c.predicted_positive and c.actual_positive and c.total:
        assert c.accuracy == (tp + tn) / c.total
    assert c.as_dict()["total"] == c.total
