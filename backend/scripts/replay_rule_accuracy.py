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
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CASES = REPO_ROOT / "backend" / "data" / "replay" / "replay_cases.jsonl"
DEFAULT_OUT = REPO_ROOT / "backend" / "data" / "replay" / "rule_accuracy_report.json"

#: 候选阈值扫描网格（mm/日）。粗网格即可——目标是判"可标定/不可标定"，不是挑小数点。
THRESHOLD_GRID = (1.0, 2.0, 5.0, 10.0, 20.0, 30.0, 50.0, 80.0, 120.0)


def load_cases(path: Path) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            cases.append(json.loads(line))
    return cases


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


def sweep(cases: list[dict[str, Any]], hazard: str) -> list[dict[str, Any]]:
    """单灾种的阈值扫描：`rain_mm >= t` 即报。返回每档的点指标。"""
    subset = [c for c in cases if c["hazard_type"] == hazard]
    positives = sum(1 for c in subset if c["truth_warning"])
    rows: list[dict[str, Any]] = []
    for threshold in THRESHOLD_GRID:
        tp = sum(1 for c in subset if c["truth_warning"] and c["rain_mm"] >= threshold)
        fp = sum(1 for c in subset if not c["truth_warning"] and c["rain_mm"] >= threshold)
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
    实测意义（2026-11-21）：三县日降雨对事件日 AUC 0.47–0.55、最佳 F1 仅略高于基线——
    结论是**这份数据不支持降雨阈值标定**（县域代理点日值与局地暴雨之间有尺度错配），
    如实记录而不是把噪声阈值塞进规则库。
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="规则准确率回放：历史真值 × 真实降雨")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)

    if not args.cases.exists():
        raise SystemExit(f"回放样本不存在：{args.cases}——先跑 scripts/build_replay_dataset.py")
    cases = load_cases(args.cases)
    by_hazard: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for case in cases:
        by_hazard[case["hazard_type"]].append(case)

    report: dict[str, Any] = {
        "schema_version": "1.0.0",
        "cases": len(cases),
        "note": "正样本=普查事件日；负样本=同县同月无该类事件日；仅日降雨可用（无传感器量）",
        "threshold_grid_mm": list(THRESHOLD_GRID),
        "calibration_gates": {"min_auc": MIN_AUC_FOR_CALIBRATION, "min_f1_improvement": MIN_F1_IMPROVEMENT},
        "current_rules": current_rule_hits(cases),
        "sweeps": {},
        "separability_auc": {},
        "calibration_verdict": {},
    }
    for hazard, subset in sorted(by_hazard.items()):
        positives = [c["rain_mm"] for c in subset if c["truth_warning"]]
        negatives = [c["rain_mm"] for c in subset if not c["truth_warning"]]
        rows = sweep(cases, hazard)
        auc_value = round(auc(positives, negatives), 4)
        report["sweeps"][hazard] = rows
        report["separability_auc"][hazard] = auc_value
        report["calibration_verdict"][hazard] = verdict_for_sweep(rows, len(positives), len(negatives), auc_value)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")

    print(f"回放样本 {len(cases)} 条 → {args.out}")
    print("\n现行规则（仅日降雨可得）:")
    for hazard, stat in report["current_rules"].items():
        print(f"  {hazard}: 正样本 {stat['positives']} 命中 {stat['tp']} | recall={stat['recall']} precision={stat['precision']}")
    print("\n可分性与标定结论:")
    for hazard in sorted(by_hazard):
        best = max(report["sweeps"][hazard], key=lambda row: row["f1"])
        print(
            f"  {hazard}: AUC={report['separability_auc'][hazard]} | 最佳阈值 {best['threshold_mm']}mm "
            f"F1={best['f1']} | 结论={report['calibration_verdict'][hazard]}"
        )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
