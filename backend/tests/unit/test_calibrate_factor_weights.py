"""因子权重校准骨架的离线用例：诚实出口（not_measured）与样本闸。

要钉住的：
- 没有标注/没有观测 → 整份报告 `not_measured`，理由写明缺哪份输入；
- 配对不足 `MIN_PAIRS` → 该灾种 `not_measured`（少样本的区分度是噪声）；
- 样本足够 → 每组 `measured`：候选权重组内和为 1、低出现率因子被排除、
  `applied=false`（没有任何自动改权重的路径）、版本号只给 measured 的灾种发。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from scripts.calibrate_factor_weights import (
    MIN_FACTOR_PRESENCE,
    MIN_PAIRS,
    calibrate,
    main,
)

from aegis.agents.hazards.risk_agent.tools.landslide_risk_tool import FACTOR_CONFIG as LANDSLIDE_CONFIG


def _labels(path: Path, hazard: str, count: int, *, start: int = 0) -> Path:
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        for index in range(start, start + count):
            handle.write(
                json.dumps(
                    {
                        "case_id": f"{hazard}-{index:04d}",
                        "region_code": "540121",
                        "hazard_type": hazard,
                        "observed_at": "2026-11-01T08:00:00+00:00",
                        "truth_warning": index % 2 == 0,
                        "truth_level": None,
                        "source": "pytest",
                        "labelled_by": "pytest",
                        "note": "",
                    }
                )
                + "\n"
            )
    return path


def _observations(path: Path, hazard: str, count: int, *, discriminative: bool = True, start: int = 0) -> Path:
    """正组雨量大、负组雨量小（discriminative=True 时区分度非零）。"""
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        for index in range(start, start + count):
            warning = index % 2 == 0
            rain = 60.0 if (warning and discriminative) else (10.0 if discriminative else 30.0)
            handle.write(
                json.dumps(
                    {
                        "case_id": f"{hazard}-{index:04d}",
                        "hazard_type": hazard,
                        "observations": [{"factor": "rainfall_mm_h", "mean_value": rain, "unit": "mm/h"}],
                    }
                )
                + "\n"
            )
    return path


def test_无输入_整份报告如实not_measured() -> None:
    report = calibrate(None, None)
    assert report["applied"] is False
    assert report["weight_set_version"] is None
    assert all(item["status"] == "not_measured" for item in report["hazards"].values())
    assert "缺因子观测导出" in report["hazards"]["landslide"]["reason"]
    assert report["inputs"]["labels"]["note"] == "未提供 --labels"


def test_有标注缺观测_理由指向观测导出(tmp_path: Path) -> None:
    labels = _labels(tmp_path / "labels.jsonl", "landslide", 5)
    report = calibrate(labels, None)
    assert report["inputs"]["labels"]["note"].endswith("5 行标注")
    assert report["hazards"]["landslide"]["reason"] == "缺因子观测导出（--observations）：证据包尚未落库，无从配对"


def test_样本不足_该灾种not_measured且不带组(tmp_path: Path) -> None:
    labels = _labels(tmp_path / "labels.jsonl", "landslide", MIN_PAIRS - 1)
    observations = _observations(tmp_path / "obs.jsonl", "landslide", MIN_PAIRS - 1)
    report = calibrate(labels, observations)
    landslide = report["hazards"]["landslide"]
    assert landslide["status"] == "not_measured"
    assert f"{MIN_PAIRS - 1}" in landslide["reason"]
    assert landslide["groups"] == {}, "样本不足时不得给出任何组候选"
    assert report["weight_set_version"] is None


def test_样本足够_组内权重归一且低出现率因子被排除(tmp_path: Path) -> None:
    hazard = "landslide"
    labels = _labels(tmp_path / "labels.jsonl", hazard, MIN_PAIRS)
    observations = _observations(tmp_path / "obs.jsonl", hazard, MIN_PAIRS)
    report = calibrate(labels, observations)
    landslide = report["hazards"][hazard]
    assert landslide["status"] == "measured" and landslide["pairs"] == MIN_PAIRS
    for group in ("P", "T", "I"):
        item = landslide["groups"][group]
        if item["status"] != "measured":
            continue
        weights = item["proposed_weights"]
        assert sum(weights.values()) == pytest.approx(1.0, abs=1e-6)
        assert set(weights) <= {declaration["key"] for declaration in LANDSLIDE_CONFIG["groups"][group]}
    # rainfall_mm_h 出现率 100% ≥ 闸，区分度非零 → 必须在 T 组候选里且权重最大
    t_item = landslide["groups"]["T"]
    assert t_item["status"] == "measured"
    assert t_item["proposed_weights"]["rainfall_mm_h"] == max(t_item["proposed_weights"].values())
    assert report["applied"] is False
    assert report["weight_set_version"] and report["weight_set_version"].startswith("calib-")


def test_无区分度_组如实not_measured(tmp_path: Path) -> None:
    hazard = "landslide"
    labels = _labels(tmp_path / "labels.jsonl", hazard, MIN_PAIRS)
    observations = _observations(tmp_path / "obs.jsonl", hazard, MIN_PAIRS, discriminative=False)
    report = calibrate(labels, observations)
    landslide = report["hazards"][hazard]
    assert all(item["status"] == "not_measured" for item in landslide["groups"].values()), "区分度全零不得产候选权重"


def test_main落盘报告_文件带换行结尾(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "reports" / "weight_calibration"
    code = main(["--out", str(out)])
    assert code == 0
    files = list(out.glob("weight_calibration_*.json"))
    assert len(files) == 1
    text = files[0].read_text(encoding="utf-8")
    assert text.endswith("\n")
    report = json.loads(text)
    assert report["method"] == "truth_separation_v0"
    assert "not_measured" in capsys.readouterr().out


def test_其他灾种的案例不串台(tmp_path: Path) -> None:
    labels_path = tmp_path / "labels.jsonl"
    observations_path = tmp_path / "obs.jsonl"
    _labels(labels_path, "landslide", MIN_PAIRS)
    _observations(observations_path, "landslide", MIN_PAIRS)
    _labels(labels_path, "debris_flow", 3, start=100)
    _observations(observations_path, "debris_flow", 3, start=100)
    report = calibrate(labels_path, observations_path)
    assert report["hazards"]["landslide"]["pairs"] == MIN_PAIRS
    assert report["hazards"]["debris_flow"]["pairs"] == 3
    assert report["hazards"]["debris_flow"]["status"] == "not_measured"
    assert MIN_FACTOR_PRESENCE == 0.6
