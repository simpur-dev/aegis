"""考核指标实测报告：把运行时埋点分位数与课题 SLA 阈值并列输出。

原则：报告里的每个数字都来自本次运行的真实样本（计数为 0 即如实标注未测得），
不写"应达到"的期望值。未由本平台数据集覆盖的指标（如预警准确率）显式标记 not_measured。

预警准确率只能从标注案例回放里算，不来自本次在线运行：`--dataset` 给出 JSONL 时
由 `aegis.persistence.replay` 口径唯一地算出（报表侧不重算），不给就如实写"未测得"。
数据集必须是现场标注（`{"dataset":{"kind":"field"}}`）才可能被判"达标"——
合成样例的结论永远是 `synthetic_only`，它只证明算术，不证明指标。

用法：
    uv run python -m scripts.metrics_report --rounds 20 --agents
    uv run python -m scripts.metrics_report --rounds 50 --out reports/metrics.json
    uv run python -m scripts.metrics_report --dataset /data/aegis/labels.jsonl
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from aegis.bus.transport import drain_pending
from aegis.config import Settings
from aegis.connectors.simulator import HazardScenarioSimulator
from aegis.container import create_container
from aegis.domain.messages import utc_now
from aegis.persistence.replay import ReplayReport, load_dataset, measure

ACCURACY_INDICATOR = "多灾种灾害预警准确率 ≥80%"
TYPE_ACCURACY_INDICATOR = "5 类灾种触发条件识别准确率"

# 指标名 → (阈值毫秒, 对应考核口径)
INDICATORS: dict[str, tuple[float, str]] = {
    "sync_agent_to_gateway_ms": (3_000.0, "多节点数据共享同步时延 ≤3s"),
    "collab_txn": (10_000.0, "异常工况识别与重调度响应 ≤10s（事务口径）"),
    "stage_assess_ms": (2_000.0, "常规任务调度响应时延 ≤2s"),
    "stage_plan_ms": (2_000.0, "常规任务调度响应时延 ≤2s"),
    "warning_generation_ms": (180_000.0, "预警信息生成时间 ≤3min"),
    "warning_reach_ms": (1_200_000.0, "预警信息靶向触达 ≤20min"),
}

INGEST_INDICATOR = ("ingest_end_to_end_seconds", 300_000.0, "多源数据接入时延 ≤5分钟")

ACCURACY_HINT = "需 5 灾种标注案例回放：`--dataset <labels.jsonl>`（现场标注口径见 persistence/replay.py）"

NOT_MEASURED = {
    ACCURACY_INDICATOR: ACCURACY_HINT,
    TYPE_ACCURACY_INDICATOR: "需案例标注测试集，本次运行仅覆盖规则引擎可判定的触发组合",
}


def _judgement(report: ReplayReport) -> str:
    """判定词按 (status, indicator) 两轴给：样本不足与没给数据是两种结论，不能共用一句话。"""
    if report.status == "insufficient_sample":
        return f"现场标注样本不足（<{report.min_field_cases} 例，本次 {report.confusion.total} 例）"
    if report.indicator == "not_measured":
        return "未测得（未提供数据集或数据集为空）"
    if report.indicator == "synthetic_only":
        return "仅合成数据集：算术已验证，不构成官方口径证据"
    return "达标" if report.indicator == "met" else "未达标"


def accuracy_rows(report: ReplayReport) -> list[dict[str, Any]]:
    """准确率两行：判定词与官方口径分开给，"报了"与"报对灾种"也分开给。"""
    overall = report.confusion
    judgement = _judgement(report)
    rows: list[dict[str, Any]] = [
        {
            "指标": ACCURACY_INDICATOR,
            "样本": overall.total,
            "准确率": overall.accuracy,
            "精确率": overall.precision,
            "召回率": overall.recall,
            "F1": overall.f1,
            "计数": {"tp": overall.tp, "fp": overall.fp, "fn": overall.fn, "tn": overall.tn},
            "阈值": report.target,
            "官方口径准确率": report.official_accuracy,
            "判定": judgement,
            "数据集": {"kind": report.dataset_kind, "source": report.dataset_source, "note": report.dataset_note},
        }
    ]
    if overall.tp:
        rows.append(
            {
                "指标": TYPE_ACCURACY_INDICATOR,
                "样本": overall.tp,
                "灾种报对": report.type_correct,
                "判定": f"{report.type_correct / overall.tp:.3f}",
            }
        )
    else:
        # 灾种是否报对只对真阳性有定义：一条都没报出来时无从判断，不是 0%
        rows.append({"指标": TYPE_ACCURACY_INDICATOR, "样本": 0, "判定": "未测得（无真阳性案例）"})
    return rows


def accuracy_report(dataset: Path | None, *, target: float | None = None) -> ReplayReport:
    """`--dataset` 缺省时也走同一份口径：空数据集 -> not_measured，而不是让报表自己造结论。"""
    loaded = None if dataset is None else load_dataset(dataset)
    return measure(loaded, target=target)


def _table(latency: dict[str, Any], collaboration: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    metrics = latency["metrics"]
    for name, (budget_ms, description) in INDICATORS.items():
        stats = metrics.get(name)
        if not stats or stats["count"] == 0:
            rows.append({"指标": description, "样本": 0, "P95_ms": None, "阈值_ms": budget_ms, "判定": "未测得"})
            continue
        p95 = stats["p95_ms"]
        rows.append(
            {
                "指标": description,
                "样本": stats["count"],
                "P50_ms": stats["p50_ms"],
                "P95_ms": p95,
                "最大_ms": stats["max_ms"],
                "阈值_ms": budget_ms,
                "判定": "达标" if p95 <= budget_ms else "超标",
                "越限样本": stats.get("breaches", 0),
            }
        )

    ingest_name, ingest_budget, ingest_desc = INGEST_INDICATOR
    ingest_stats = metrics.get(ingest_name)
    if ingest_stats and ingest_stats["count"]:
        # 该指标以"秒"记录，转为毫秒与阈值比较
        p95_ms = ingest_stats["p95_ms"]
        rows.append(
            {
                "指标": ingest_desc,
                "样本": ingest_stats["count"],
                "P95_ms": p95_ms,
                "阈值_ms": ingest_budget,
                "判定": "达标" if p95_ms <= ingest_budget else "超标",
            }
        )

    rate = collaboration["success_rate"]
    rows.append(
        {
            "指标": "多智能体协同联动成功率 ≥90%",
            "样本": collaboration["transactions"],
            "成功率": rate,
            "判定": ("达标" if rate is not None and rate >= collaboration["target"] else "未达标")
            if rate is not None
            else "未测得（无协同事务）",
        }
    )
    return rows


async def run(
    *,
    rounds: int,
    with_agents: bool,
    seed: int,
    out: Path | None,
    dataset: Path | None = None,
    accuracy_target: float | None = None,
) -> int:
    settings = Settings(env="dev", bus_backend="memory", delivery_mode="mock", simulator_seed=seed)
    container = create_container(settings, with_simulator=False)
    await container.start(with_mock_agents=with_agents)
    await asyncio.sleep(0.1)

    simulator = HazardScenarioSimulator(scenario="surge", seed=seed)
    acted = 0
    fatal = 0
    try:
        for _round in range(rounds):
            readings = simulator.collect_at(utc_now())
            grouped: dict[str, list[Any]] = {}
            for reading in readings:
                grouped.setdefault(reading.region_code, []).append(reading)
            results = await container.chain.process_many(grouped)
            acted += sum(1 for r in results if r.acted)
            fatal += sum(1 for r in results if r.errors)
            await drain_pending(container.transport, timeout=5.0)
        latency = container.latency_report()
    finally:
        await container.shutdown()

    # 准确率不来自这次在线运行：没给数据集就只报"未量测"，不给它任何看起来像结论的形状
    accuracy = accuracy_report(dataset, target=accuracy_target)
    rows = _table(latency, latency["collaboration"])
    uncovered = dict(NOT_MEASURED)
    if dataset is not None:
        rows += accuracy_rows(accuracy)
        covered = {str(row["指标"]) for row in rows}
        uncovered = {name: hint for name, hint in NOT_MEASURED.items() if name not in covered}

    report = {
        "运行参数": {"rounds": rounds, "with_mock_agents": with_agents, "seed": seed, "事件产出": acted, "致命错误": fatal},
        "指标判定": rows,
        "准确率回放": accuracy.as_dict(),
        "未覆盖指标": uncovered,
        "原始分位": latency["metrics"],
        "网关计数": latency["gateway_counters"],
        "运行态": latency["store"],
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n[已写入] {out}", file=sys.stderr)
    # 只有"现场标注数据集判出不达标"才算这次报表失败；未量测不是失败，是如实的空结论
    return 1 if fatal or accuracy.indicator in {"not_met", "insufficient_sample"} else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="AEGIS 考核指标实测报告")
    parser.add_argument("--rounds", type=int, default=10)
    parser.add_argument("--agents", action="store_true", help="使用 Mock 智能体（否则测平台降级路径）")
    parser.add_argument("--seed", type=int, default=202_609)
    parser.add_argument("--out", type=Path, default=None, help="同时把报告写入 JSON 文件")
    parser.add_argument("--dataset", type=Path, default=None, help="预警准确率回放数据集（JSONL，口径见 persistence/replay.py）")
    parser.add_argument("--accuracy-target", type=float, default=None, help="覆盖配置里的准确率阈值（0—1]，仅影响判定行")
    args = parser.parse_args()
    if args.rounds < 1:
        parser.error("--rounds 必须 ≥ 1")
    if args.dataset is not None and not args.dataset.is_file():
        parser.error(f"--dataset 文件不存在：{args.dataset}")
    return asyncio.run(
        run(
            rounds=args.rounds,
            with_agents=args.agents,
            seed=args.seed,
            out=args.out,
            dataset=args.dataset,
            accuracy_target=args.accuracy_target,
        )
    )


if __name__ == "__main__":
    sys.exit(main())
