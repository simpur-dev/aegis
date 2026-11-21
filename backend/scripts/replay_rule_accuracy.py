"""规则准确率回放：历史真值 × 真实降雨 → precision/recall/F1（含阈值扫描）。

**为什么需要它**：平台有 `warning_truth_labels` / 准确率回放的**生产**口径（`persistence/replay.py`），
但那要求现场标注；外部公开数据目录里的公路普查（13,768 条，2012–2022）是**历史真值**——
不需要等现场标注，就能回答"现行规则在历史事件上抓得住吗"。这一步把"准确性"从口号变成数字。

**评测两档（都不改生产代码）**：
1. **现行规则库**：把回放样本的降雨折算成平台指标（`rain_cumulative_24h` = 当日降雨），
   灌进 `RuleEngine.evaluate()`——规则里凡要求传感器量的（`debris_level`/`displacement_mm`/
   `crack_aperture_mm`）在纯雨量回放下不成立，所以这一档量的是"仅凭雨量，现行规则能抓到多少"。
   这是**如实的下界**，不是规则的完整能力（完整能力要传感器数据）。
2. **候选降雨阈值扫描**：对每个灾种扫 `rain_mm >= t` 的阈值，报各点的 precision/recall/F1。
   扫描的目的不是挑一个"最好看"的阈值塞进规则，而是回答**"这个数据集是否支持降雨阈值标定"**：
   若最佳 F1 与"全报正样本"的朴素基线接近，则答案是**不支持**（先验不可估）。

**口径**：
- 正样本 = 普查已发生事件日；负样本 = 同县同月无该类事件的日期（见 `build_replay_dataset.py`）；
- AUC（Mann-Whitney）衡量可分性：0.5 = 与随机猜无异；
- 输出 `backend/data/replay/rule_accuracy_report.json`（运行态，不入 git；含 NC 数据派生统计）。

用法：
    uv run python scripts/replay_rule_accuracy.py
    uv run python scripts/replay_rule_accuracy.py --cases backend/data/replay/replay_cases.jsonl
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CASES = REPO_ROOT / "backend" / "data" / "replay" / "replay_cases.jsonl"
DEFAULT_OUT = REPO_ROOT / "backend" / "data" / "replay" / "rule_accuracy_report.json"
DEFAULT_STATION_DAILY = REPO_ROOT / "backend" / "data" / "precip" / "station_daily.csv"

#: 候选阈值扫描网格（mm/日）。粗网格即可——目标是判"可标定/不可标定"，不是挑小数点。
THRESHOLD_GRID = (1.0, 2.0, 5.0, 10.0, 20.0, 30.0, 50.0, 80.0, 120.0)

#: 累计窗口（日）。单日雨量与事件日近乎独立，但土壤含水量有记忆——水文上更合理的
#: 先验是"前几天下了多少"。实测（2026-11-21，三县 2020–2021）：7 日累计 AUC 明显
#: 高于单日，落石/滑坡在留出切分下 F1 提升仍过 1.25× 闸，泥石流过不了。
CUMULATIVE_WINDOWS = (3, 7, 15)
#: 累计量的候选阈值网格（mm/窗口）。比日雨网格更粗但上限更高（15 日累计可到 200mm+）。
CUMULATIVE_THRESHOLD_GRID = tuple(float(v) for v in range(5, 205, 5))


def load_cases(path: Path) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            cases.append(json.loads(line))
    return cases


def load_station_daily(path: Path) -> dict[tuple[str, str], float | None]:
    """(region, 'YYYY-MM-DD') → 日降雨 mm；空值/坏值读成 None（缺测不是 0）。"""
    daily: dict[tuple[str, str], float | None] = {}
    with path.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            raw = (row.get("precip_mm") or "").strip()
            try:
                value = float(raw) if raw else None
            except ValueError:
                value = None
            daily[(row["region_code"], row["date"])] = value
    return daily


def attach_windows(
    cases: list[dict[str, Any]],
    daily: dict[tuple[str, str], float | None],
    windows: tuple[int, ...] = CUMULATIVE_WINDOWS,
) -> list[dict[str, Any]]:
    """给每条样本加 `rain_{w}d_mm`：窗口内**已知日**求和，全缺测 → None。

    缺测日不按 0 计（0mm 是合法读数，缺测不是"没下雨"）；代价是部分窗口只有几天有值，
    所以报告里同时给每档的缺测样本数，读者能判断"这个 AUC 建在多少条上"。
    窗口包含当日（当日 + 前 w-1 日）。
    """
    enriched: list[dict[str, Any]] = []
    for case in cases:
        row = dict(case)
        day = date.fromisoformat(case["date"])
        region = case["region_code"]
        for window in windows:
            known = [daily.get((region, (day - timedelta(days=offset)).isoformat())) for offset in range(window)]
            values = [value for value in known if value is not None]
            row[f"rain_{window}d_mm"] = sum(values) if values else None
            row[f"rain_{window}d_missing"] = window - len(values)
        enriched.append(row)
    return enriched


def auc(positives: list[float], negatives: list[float]) -> float:
    """Mann-Whitney AUC：正样本值 > 负样本值的概率（0.5 = 无区分度）。"""
    if not positives or not negatives:
        return 0.5
    wins = 0.0
    for value in positives:
        for other in negatives:
            if value > other:
                wins += 1.0
            elif value == other:
                wins += 0.5
    return wins / (len(positives) * len(negatives))


def sweep(
    cases: list[dict[str, Any]],
    hazard: str,
    *,
    value_key: str = "rain_mm",
    grid: tuple[float, ...] = THRESHOLD_GRID,
) -> list[dict[str, Any]]:
    """单灾种的阈值扫描：`value_key >= t` 即报。返回每档的点指标。

    `value_key is None` 的样本（缺测）不参与任何一档——既不进分母也不进分子，
    否则"缺测"会被当成"没下雨"混进负样本。
    """
    subset = [c for c in cases if c["hazard_type"] == hazard and c.get(value_key) is not None]
    positives = sum(1 for c in subset if c["truth_warning"])
    rows: list[dict[str, Any]] = []
    for threshold in grid:
        tp = sum(1 for c in subset if c["truth_warning"] and c[value_key] >= threshold)
        fp = sum(1 for c in subset if not c["truth_warning"] and c[value_key] >= threshold)
        fn = positives - tp
        precision = tp / (tp + fp) if (tp + fp) else 0.0
        recall = tp / positives if positives else 0.0
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
        rows.append(
            {
                "threshold_mm": threshold,
                "tp": tp,
                "fp": fp,
                "fn": fn,
                "precision": round(precision, 4),
                "recall": round(recall, 4),
                "f1": round(f1, 4),
            }
        )
    return rows


def current_rule_hits(cases: list[dict[str, Any]]) -> dict[str, Any]:
    """现行规则库在纯雨量回放下的命中：把当日降雨折算成平台指标喂 RuleEngine。

    `rain_cumulative_24h` = 当日降雨（`precip` 腿的口径）；`rain_10min` 无法从日值反推，
    **不造假**——只喂日累计，规则里要 10 分钟雨强的必然不成立，这是如实的下界。
    """
    from aegis.domain.messages import TelemetryReading, now_iso
    from aegis.services.rules.trigger_rules import RuleEngine

    engine = RuleEngine()
    stats: dict[str, dict[str, int]] = defaultdict(lambda: {"positive": 0, "hit_on_positive": 0, "negative": 0, "hit_on_negative": 0})
    for case in cases:
        reading = TelemetryReading(
            station_id=f"PR-{case['region_code']}-01",
            metric="rain_cumulative_24h",
            value=float(case["rain_mm"]),
            unit="mm",
            region_code=case["region_code"],
            observed_at=f"{case['date']}T12:00:00Z",
            ingested_at=now_iso(),
            source="replay",
            quality_flag="ok",
        )
        result = engine.evaluate([reading], region_code=case["region_code"])
        fired = any(hit.hazard_type == case["hazard_type"] for hit in result.hits)
        bucket = stats[case["hazard_type"]]
        if case["truth_warning"]:
            bucket["positive"] += 1
            bucket["hit_on_positive"] += 1 if fired else 0
        else:
            bucket["negative"] += 1
            bucket["hit_on_negative"] += 1 if fired else 0
    out: dict[str, Any] = {}
    for hazard, bucket in sorted(stats.items()):
        tp = bucket["hit_on_positive"]
        fp = bucket["hit_on_negative"]
        positives = bucket["positive"]
        out[hazard] = {
            "positives": positives,
            "negatives": bucket["negative"],
            "tp": tp,
            "fp": fp,
            "precision": round(tp / (tp + fp), 4) if (tp + fp) else None,
            "recall": round(tp / positives, 4) if positives else None,
        }
    return out


#: 标定可行性的双闸：只看 F1 会被"正样本极稀少"的类别骗（F1 微高于朴素基线就误判可标定）。
#: AUC 是阈值无关的可分性度量——雨量若与事件日近乎独立（AUC≈0.5），任何阈值的 F1 提升
#: 都只是拟合噪声。两个条件都过才允许说"这份数据支持降雨阈值标定"。
MIN_AUC_FOR_CALIBRATION = 0.65
MIN_F1_IMPROVEMENT = 1.25


def verdict_for_sweep(rows: list[dict[str, Any]], positives: int, negatives: int, auc_value: float) -> str:
    """数据集是否支持降雨阈值标定：**AUC 与 F1 双闸**。

    朴素基线 = 永远报警：precision = 正样本占比，recall = 1.0。
    实测意义（2026-11-21 修正后）：三县**单日**降雨对事件日 AUC 0.58/0.64/0.64、
    最佳阈值全部落在扫描网格下沿 2mm（信号只是"下没下雨"）→ 单日口径不支持标定。
    **累计窗口是另一回事**：7 日累计 AUC 0.65–0.71，落石/滑坡过双闸、泥石流过不了
    （见 `holdout` 的留出检查）——单日不成不代表这份数据没用，口径要说清。
    """
    if not rows or positives == 0 or negatives == 0:
        return "insufficient_samples"
    if auc_value < MIN_AUC_FOR_CALIBRATION:
        return "not_supported_by_data"
    base_precision = positives / (positives + negatives)
    base_f1 = 2 * base_precision * 1.0 / (base_precision + 1.0)
    best_f1 = max(row["f1"] for row in rows)
    if best_f1 <= base_f1 * MIN_F1_IMPROVEMENT:
        return "not_supported_by_data"
    return "supported"


def _metric_block(cases: list[dict[str, Any]], hazard: str, value_key: str) -> dict[str, Any]:
    """单档（灾种 × 雨量口径）的 AUC/扫描/结论 + 有效样本数（缺测剔除后）。"""
    subset = [c for c in cases if c["hazard_type"] == hazard and c.get(value_key) is not None]
    positives = [c[value_key] for c in subset if c["truth_warning"]]
    negatives = [c[value_key] for c in subset if not c["truth_warning"]]
    grid = THRESHOLD_GRID if value_key == "rain_mm" else CUMULATIVE_THRESHOLD_GRID
    rows = sweep(cases, hazard, value_key=value_key, grid=grid)
    auc_value = round(auc(positives, negatives), 4)
    best = max(rows, key=lambda row: row["f1"]) if rows else None
    return {
        "positives": len(positives),
        "negatives": len(negatives),
        "auc": auc_value,
        "best": None
        if best is None
        else {
            "threshold_mm": best["threshold_mm"],
            "f1": best["f1"],
            "precision": best["precision"],
            "recall": best["recall"],
        },
        "verdict": verdict_for_sweep(rows, len(positives), len(negatives), auc_value),
        "sweep": rows,
    }


#: 留出切分：阈值在训练侧挑、指标在测试侧算——同一批样本上挑阈值再自评会把
#: "拟合噪声"读成"可标定"。两种切分各管一个混淆来源：年份（气候年际差异）与
#: 区域（540221 占正样本 65%，留出它检验不是只学会了一个县的雨）。
HOLDOUT_SPLITS: dict[str, Callable[[dict[str, Any]], bool]] = {
    "year_2020_train": lambda case: case["date"][:4] == "2020",
    "region_540221_holdout": lambda case: case["region_code"] != "540221",
}


def holdout(cases: list[dict[str, Any]], hazard: str, value_key: str) -> dict[str, Any]:
    """阈值迁移检查：训练侧选最佳 F1 阈值，测试侧算 F1 与朴素基线的比值。"""
    grid = THRESHOLD_GRID if value_key == "rain_mm" else CUMULATIVE_THRESHOLD_GRID
    subset = [c for c in cases if c["hazard_type"] == hazard and c.get(value_key) is not None]
    out: dict[str, Any] = {}
    for name, is_train in HOLDOUT_SPLITS.items():
        train = [c for c in subset if is_train(c)]
        test = [c for c in subset if not is_train(c)]
        train_rows = sweep(train, hazard, value_key=value_key, grid=grid)
        if not train_rows:
            out[name] = {"status": "insufficient_samples"}
            continue
        chosen = max(train_rows, key=lambda row: row["f1"])["threshold_mm"]
        tp = sum(1 for c in test if c["truth_warning"] and c[value_key] >= chosen)
        fp = sum(1 for c in test if not c["truth_warning"] and c[value_key] >= chosen)
        positives = sum(1 for c in test if c["truth_warning"])
        negatives = len(test) - positives
        if positives == 0 or negatives == 0:
            out[name] = {"status": "insufficient_samples", "threshold_mm": chosen}
            continue
        precision = tp / (tp + fp) if (tp + fp) else 0.0
        recall = tp / positives
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
        base_precision = positives / (positives + negatives)
        base_f1 = 2 * base_precision / (base_precision + 1.0)
        out[name] = {
            "threshold_mm": chosen,
            "test_positives": positives,
            "test_negatives": negatives,
            "precision": round(precision, 4),
            "recall": round(recall, 4),
            "f1": round(f1, 4),
            "base_f1": round(base_f1, 4),
            "f1_improvement": round(f1 / base_f1, 4) if base_f1 else None,
            "passes_f1_gate": bool(base_f1 and f1 > base_f1 * MIN_F1_IMPROVEMENT),
        }
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="规则准确率回放：历史真值 × 真实降雨")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument(
        "--station-daily",
        type=Path,
        default=DEFAULT_STATION_DAILY,
        help="站点日降雨 CSV（缺省 backend/data/precip/station_daily.csv；不存在则跳过累计窗口）",
    )
    args = parser.parse_args(argv)

    if not args.cases.exists():
        raise SystemExit(f"回放样本不存在：{args.cases}——先跑 scripts/build_replay_dataset.py")
    cases = load_cases(args.cases)
    by_hazard: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for case in cases:
        by_hazard[case["hazard_type"]].append(case)

    report: dict[str, Any] = {
        "schema_version": "1.1.0",
        "cases": len(cases),
        "note": "正样本=普查事件日；负样本=同县同月无该类事件日；仅日降雨可用（无传感器量）",
        "threshold_grid_mm": list(THRESHOLD_GRID),
        "calibration_gates": {"min_auc": MIN_AUC_FOR_CALIBRATION, "min_f1_improvement": MIN_F1_IMPROVEMENT},
        "current_rules": current_rule_hits(cases),
        "sweeps": {},
        "separability_auc": {},
        "calibration_verdict": {},
    }
    for hazard in sorted(by_hazard):
        block = _metric_block(cases, hazard, "rain_mm")
        report["sweeps"][hazard] = block["sweep"]
        report["separability_auc"][hazard] = block["auc"]
        report["calibration_verdict"][hazard] = block["verdict"]

    if args.station_daily.exists():
        enriched = attach_windows(cases, load_station_daily(args.station_daily))
        report["cumulative_windows"] = list(CUMULATIVE_WINDOWS)
        report["cumulative"] = {}
        for window in CUMULATIVE_WINDOWS:
            key = f"rain_{window}d_mm"
            report["cumulative"][f"{window}d"] = {hazard: _metric_block(enriched, hazard, key) for hazard in sorted(by_hazard)}
        report["holdout"] = {
            f"{window}d": {hazard: holdout(enriched, hazard, f"rain_{window}d_mm") for hazard in sorted(by_hazard)}
            for window in CUMULATIVE_WINDOWS
        }
    else:
        report["cumulative_note"] = f"站点日值不存在（{args.station_daily}），累计窗口评测跳过"

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")

    print(f"回放样本 {len(cases)} 条 → {args.out}")
    print("\n现行规则（仅日降雨可得）:")
    for hazard, stat in report["current_rules"].items():
        print(f"  {hazard}: 正样本 {stat['positives']} 命中 {stat['tp']} | recall={stat['recall']} precision={stat['precision']}")
    print("\n可分性与标定结论（单日）:")
    for hazard in sorted(by_hazard):
        best = max(report["sweeps"][hazard], key=lambda row: row["f1"])
        print(
            f"  {hazard}: AUC={report['separability_auc'][hazard]} | 最佳阈值 {best['threshold_mm']}mm "
            f"F1={best['f1']} | 结论={report['calibration_verdict'][hazard]}"
        )
    if "cumulative" in report:
        print("\n累计窗口与留出检查（训练侧挑阈值、测试侧算指标）:")
        for window, block in report["cumulative"].items():
            for hazard, stat in block.items():
                holds = report["holdout"][window][hazard]
                hold_txt = "；".join(
                    f"{name} 阈值 {item['threshold_mm']}mm F1={item['f1']} ({item.get('f1_improvement')}×)"
                    for name, item in holds.items()
                    if item.get("f1") is not None
                )
                print(f"  {window} {hazard}: AUC={stat['auc']} N={stat['negatives']} 结论={stat['verdict']} | 留出：{hold_txt}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
