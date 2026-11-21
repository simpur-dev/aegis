"""规则准确率回放的离线用例：可分性、阈值扫描、标定可行性双闸。

要点：
- AUC 用 Mann-Whitney（0.5 = 随机）；完全可分的合成数据必须给 1.0；
- 阈值扫描的 precision/recall 算术要能被手算核对；
- **标定可行性双闸**（AUC≥0.65 且 F1 优于朴素基线 1.25×）：雨量与事件日近乎独立时
  必须判 `not_supported_by_data`——这正是三县真实数据的实测结论（AUC 0.47–0.55），
  防止"挑一个最好看的阈值"冒充标定。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from scripts.replay_rule_accuracy import (
    MIN_AUC_FOR_CALIBRATION,
    attach_windows,
    auc,
    holdout,
    load_station_daily,
    main,
    sweep,
    verdict_for_sweep,
)


def _case(rain: float, truth: bool, hazard: str = "debris_flow") -> dict:
    return {
        "case_id": f"replay:540121:2021-07-0{(1 if truth else 2)}:{hazard}:{rain}",
        "region_code": "540121",
        "date": "2021-07-01" if truth else "2021-07-02",
        "hazard_type": hazard,
        "rain_mm": rain,
        "truth_warning": truth,
    }


class TestAuc:
    def test_完全可分时为1(self) -> None:
        assert auc([50.0, 60.0], [1.0, 2.0]) == pytest.approx(1.0)

    def test_完全反序时为0(self) -> None:
        assert auc([1.0, 2.0], [50.0, 60.0]) == pytest.approx(0.0)

    def test_无区分度时约等于半分(self) -> None:
        # 交叉分布：一半正样本高于负样本、一半低于
        assert auc([1.0, 10.0], [5.0, 20.0]) == pytest.approx(0.25)
        assert auc([5.0, 20.0], [1.0, 10.0]) == pytest.approx(0.75)

    def test_空样本按无信息处理(self) -> None:
        assert auc([], [1.0]) == 0.5


class TestSweep:
    def test_点指标可手算(self) -> None:
        cases = [_case(50.0, True), _case(5.0, True), _case(50.0, False), _case(1.0, False)]
        rows = {row["threshold_mm"]: row for row in sweep(cases, "debris_flow")}
        # 阈值 10：只有 50mm 那条正样本过线、同时一条负样本也过线
        at10 = rows[10.0]
        assert (at10["tp"], at10["fp"], at10["fn"]) == (1, 1, 1)
        assert at10["precision"] == pytest.approx(0.5)
        assert at10["recall"] == pytest.approx(0.5)
        assert at10["f1"] == pytest.approx(0.5)
        # 阈值 120：全部不过线
        at120 = rows[120.0]
        assert (at120["tp"], at120["fp"], at120["recall"]) == (0, 0, 0.0)


class TestCalibrationVerdict:
    def test_低AUC一律不可标定(self) -> None:
        """AUC 0.55 的类别无论 F1 怎么挑，都不该被判"支持标定"。"""
        rows = sweep([_case(10.0, True), _case(9.9, False)], "debris_flow")
        assert verdict_for_sweep(rows, positives=1, negatives=1, auc_value=MIN_AUC_FOR_CALIBRATION - 0.01) == "not_supported_by_data"

    def test_高AUC但F1无显著提升也不可标定(self) -> None:
        rows = [{"threshold_mm": 10.0, "f1": 0.07}]  # 朴素基线（1 正 30 负）约 0.063
        assert verdict_for_sweep(rows, positives=1, negatives=30, auc_value=0.9) == "not_supported_by_data"

    def test_高AUC且F1显著提升才可标定(self) -> None:
        rows = [{"threshold_mm": 10.0, "f1": 0.9}]
        assert verdict_for_sweep(rows, positives=10, negatives=10, auc_value=0.9) == "supported"

    def test_样本不足如实标注(self) -> None:
        assert verdict_for_sweep([], positives=0, negatives=5, auc_value=0.9) == "insufficient_samples"


def test_main产出报告且带双闸口径(tmp_path: Path) -> None:
    cases = tmp_path / "cases.jsonl"
    with cases.open("w", encoding="utf-8", newline="\n") as handle:
        for index in range(4):
            handle.write(json.dumps(_case(rain=float(index * 10), truth=index % 2 == 0), ensure_ascii=False) + "\n")
    out = tmp_path / "report.json"
    # 站点日值显式指到不存在路径：单测不做真实文件依赖，累计窗口跑"跳过"分支
    assert main(["--cases", str(cases), "--out", str(out), "--station-daily", str(tmp_path / "none.csv")]) == 0
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["cases"] == 4
    assert set(report["current_rules"]) == {"debris_flow"}
    assert report["calibration_gates"]["min_auc"] == MIN_AUC_FOR_CALIBRATION
    # 正样本（0/20mm）整体低于负样本（10/30mm）→ AUC 0.25（4 对里 1 对正>负），
    # 这同时说明"反相关"也会被如实量出来（不是只挑好看的数看）
    assert report["separability_auc"]["debris_flow"] == pytest.approx(0.25)
    assert report["calibration_verdict"]["debris_flow"] == "not_supported_by_data"
    assert "cumulative" not in report and "cumulative_note" in report


def _daily(path: Path, rows: list[tuple[str, str, float | None]]) -> Path:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("date,station_id,region_code,latitude,longitude,precip_mm\n")
        for region, day, value in rows:
            handle.write(f"{day},PR-{region}-01,{region},29.0,94.0,{'' if value is None else value}\n")
    return path


class TestCumulativeWindows:
    def test_窗口跨月求和且缺测不按零计(self, tmp_path: Path) -> None:
        daily = load_station_daily(
            _daily(
                tmp_path / "d.csv",
                [
                    ("540121", "2021-07-30", 10.0),
                    ("540121", "2021-07-31", 5.0),
                    ("540121", "2021-08-01", 2.0),
                    ("540121", "2021-08-02", None),  # 缺测：不计 0，只少一天
                ],
            )
        )
        [row] = attach_windows([{**_case(2.0, True), "date": "2021-08-01"}], daily, windows=(3,))
        # 3 日窗口 = 08-01 + 07-31 + 07-30 = 17.0（08-01 是当日）
        assert row["rain_3d_mm"] == pytest.approx(17.0)
        assert row["rain_3d_missing"] == 0

        [row2] = attach_windows([{**_case(2.0, True), "date": "2021-08-02"}], daily, windows=(2,))
        assert row2["rain_2d_mm"] == pytest.approx(2.0), "缺测日跳过，不把缺测当 0 也不整窗作废"
        assert row2["rain_2d_missing"] == 1

    def test_整窗缺测为None且扫描不把它当负样本(self) -> None:
        cases = [
            {
                "case_id": "a",
                "region_code": "540121",
                "date": "2021-08-03",
                "hazard_type": "debris_flow",
                "rain_7d_mm": None,
                "truth_warning": False,
            }
        ]
        rows = sweep(cases, "debris_flow", value_key="rain_7d_mm")
        assert all(row["fp"] == 0 for row in rows), "缺测样本不进任何一档"


class TestHoldout:
    @staticmethod
    def _hcase(case_id: str, day: str, rain: float, truth: bool, region: str = "540121") -> dict:
        return {
            "case_id": case_id,
            "region_code": region,
            "date": day,
            "hazard_type": "rockfall",
            "rain_7d_mm": rain,
            "truth_warning": truth,
        }

    def test_阈值在训练侧挑测试侧算(self) -> None:
        """2020 训练/2021 测试：训练侧能分开的阈值，测试侧必须照样过 F1 闸（合成数据）。"""
        cases = []
        # 2020：正样本 100mm、负样本 0mm（训练侧可分）
        for index in range(5):
            cases.append(self._hcase(f"t{index}", f"2020-08-{index + 1:02d}", 100.0, True))
            cases.append(self._hcase(f"n{index}", f"2020-09-{index + 1:02d}", 0.0, False))
        # 2021：同样的可分结构（测试侧应能复现）
        for index in range(5):
            cases.append(self._hcase(f"t2{index}", f"2021-08-{index + 1:02d}", 100.0, True))
            cases.append(self._hcase(f"n2{index}", f"2021-09-{index + 1:02d}", 0.0, False))
        result = holdout(cases, "rockfall", "rain_7d_mm")["year_2020_train"]
        assert result["threshold_mm"] > 0
        assert result["f1"] == pytest.approx(1.0)
        assert result["passes_f1_gate"] is True

    def test_测试侧无正样本时如实标样本不足(self) -> None:
        cases = [
            self._hcase("t", "2020-08-01", 100.0, True),
            self._hcase("n", "2020-09-01", 0.0, False),
        ]
        result = holdout(cases, "rockfall", "rain_7d_mm")["year_2020_train"]
        # 2020 全在训练侧 → 测试侧只剩 0 条
        assert result["status"] == "insufficient_samples"
