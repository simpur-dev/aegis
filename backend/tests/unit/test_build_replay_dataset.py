"""回放数据集构建的离线用例：真值标签正确性（复检抓到的 HIGH bug 的守卫）。

**钉住什么**：负样本查找必须用 `(region, day)` 元组键、去重必须正样本优先。
2026-11-21 复检实测：字符串键查元组字典永远落空 + 先到先留的去重，使 123/170 个
真实事件日被标成 `truth_warning=false`，REPORT 的 AUC/正样本数随之失真。
"""

from __future__ import annotations

import json
from pathlib import Path

from scripts.build_replay_dataset import build_cases, load_census, load_rain


def _census(path: Path, rows: list[tuple[str, str, str, bool]]) -> Path:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for index, (region, day, hazard, occurred) in enumerate(rows):
            handle.write(
                json.dumps(
                    {
                        "record_id": f"census:{index}",
                        "event": {"date": day},
                        "geo": {"admin_division": region},
                        "hazard": {"type": hazard, "attributes": {"occurred": occurred}},
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    return path


def _rain(path: Path, rows: list[tuple[str, str, float]]) -> Path:
    path.write_text(
        "date,station_id,region_code,latitude,longitude,precip_mm\n"
        + "".join(f"{day},PR-{region}-01,{region},29.0,94.0,{value}\n" for region, day, value in rows),
        encoding="utf-8",
    )
    return path


def _month_days(region: str, month: str, value: float = 1.0) -> list[tuple[str, str, float]]:
    return [(region, f"{month}-{day:02d}", value) for day in range(1, 29)]


class TestCensusLoading:
    def test_元组键且过滤窗口与已发生(self, tmp_path: Path) -> None:
        census = _census(
            tmp_path / "c.jsonl",
            [
                ("540121", "2021-08-01", "debris_flow", True),
                ("540121", "2021-08-01", "landslide", True),
                ("540121", "2021-08-02", "debris_flow", False),  # 未发生：隐患点
                ("540121", "2010-01-01", "debris_flow", True),  # 窗口外
                ("999999", "2021-08-01", "debris_flow", True),  # 非三县
            ],
        )
        events = load_census(census)
        assert events == {("540121", "2021-08-01"): ["debris_flow", "landslide"]}, "键必须是 (region, day) 元组"


class TestBuildCases:
    def test_事件日不会被误标为负样本(self, tmp_path: Path) -> None:
        """核心守卫：同月里已发生事件的日期，绝不能出现在负样本里。"""
        census = _census(tmp_path / "c.jsonl", [("540121", "2021-08-10", "debris_flow", True)])
        rain = _rain(tmp_path / "r.csv", _month_days("540121", "2021-08"))
        report = build_cases(census, rain)
        positives = {(c["date"], c["hazard_type"]) for c in report["cases"] if c["truth_warning"]}
        negatives = {(c["date"], c["hazard_type"]) for c in report["cases"] if not c["truth_warning"]}
        assert positives == {("2021-08-10", "debris_flow")}
        assert ("2021-08-10", "debris_flow") not in negatives, "事件日被标成了负样本"

    def test_正样本优先于同id负样本(self, tmp_path: Path) -> None:
        """同月同灾种两个事件日：两天都必须保留为正样本（不得被先生成的负样本占位）。"""
        census = _census(
            tmp_path / "c.jsonl",
            [("540121", "2021-08-05", "landslide", True), ("540121", "2021-08-20", "landslide", True)],
        )
        rain = _rain(tmp_path / "r.csv", _month_days("540121", "2021-08"))
        report = build_cases(census, rain)
        positives = sorted(c["date"] for c in report["cases"] if c["truth_warning"])
        assert positives == ["2021-08-05", "2021-08-20"]

    def test_其他灾种的事件日仍是本灾种负样本(self, tmp_path: Path) -> None:
        """灾种独立：8-10 只发生泥石流（无滑坡）→ 该日对滑坡口径是负样本。

        注意设计边界：负样本只在该区该月**至少有一次该灾种事件**时才生成
        （迭代事件日时展开同月负样本）——"整月无该灾种"的月份不产负样本，
        精度是"有事件月份内"的条件估计。这条边界写在模块 docstring 里。
        """
        census = _census(
            tmp_path / "c.jsonl",
            [("540121", "2021-08-10", "debris_flow", True), ("540121", "2021-08-20", "landslide", True)],
        )
        rain = _rain(tmp_path / "r.csv", _month_days("540121", "2021-08"))
        report = build_cases(census, rain)
        landslide_negatives = {c["date"] for c in report["cases"] if not c["truth_warning"] and c["hazard_type"] == "landslide"}
        assert "2021-08-10" in landslide_negatives, "同月无滑坡的事件日对滑坡口径是负样本"

    def test_缺测日不进样本(self, tmp_path: Path) -> None:
        census = _census(tmp_path / "c.jsonl", [("540121", "2021-08-10", "debris_flow", True)])
        rows = [(region, day, value) for region, day, value in _month_days("540121", "2021-08") if day != "2021-08-11"]
        rain = _rain(tmp_path / "r.csv", rows)
        report = build_cases(census, rain)
        assert all(c["date"] != "2021-08-11" for c in report["cases"]), "缺测日（无降雨值）不得进样本"


class TestRainLoading:
    def test_缺测读成None而不是0(self, tmp_path: Path) -> None:
        path = tmp_path / "r.csv"
        path.write_text(
            "date,station_id,region_code,latitude,longitude,precip_mm\n"
            "2021-08-01,PR-1,540121,29.0,94.0,0.0\n"
            "2021-08-02,PR-1,540121,29.0,94.0,\n",
            encoding="utf-8",
        )
        rain = load_rain(path)
        assert rain[("540121", "2021-08-01")] == 0.0, "0mm 是合法读数"
        assert rain[("540121", "2021-08-02")] is None, "空值是缺测，不是 0"
