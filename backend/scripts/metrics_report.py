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
from aegis.observability.tracer import unit_of_metric
from aegis.persistence.replay import ReplayReport, load_dataset, measure

ACCURACY_INDICATOR = "多灾种灾害预警准确率 ≥80%"
TYPE_ACCURACY_INDICATOR = "5 类灾种触发条件识别准确率"

#: 指标名 → 对应考核口径。阈值不在这里抄第二份：账本出口自带 `budget` 与 `unit`，
#: 唯一的登记处是 `instrumentation.register_sla_budgets`。此前这份副本按"毫秒"写死，
#: 秒制那两条只能另开一支循环处理（`INGEST_INDICATOR` / `REPORT_INTAKE_INDICATOR`），
#: 而"另开一支"正是当年把秒值拿去比毫秒阈值、永远绿的源头。
INDICATORS: dict[str, str] = {
    "sync_agent_to_gateway_ms": "多节点数据共享同步时延 ≤3s",
    # 语义交互与数据共享同属考核指标 3 的 ≤3s 一档（`services/assistant.py` 唯一的记账点）
    "assistant_reply_ms": "语义交互一轮响应 ≤3s",
    "collab_txn": "异常工况识别与重调度响应 ≤10s（事务口径）",
    "stage_assess_ms": "常规任务调度响应时延 ≤2s",
    "stage_plan_ms": "常规任务调度响应时延 ≤2s",
    "warning_generation_ms": "预警信息生成时间 ≤3min",
    "warning_reach_ms": "预警信息靶向触达 ≤20min",
    "ingest_end_to_end_seconds": "多源数据接入时延 ≤5分钟",
    # 人工上报是"接入 ≤5min"的第四条腿（批次 B4）
    "report_intake_seconds": "人工上报接入时延 ≤5分钟（第四条腿）",
}

#: 固定两条上报文本：一条够得上阈值（该报警），一条只描述现象（不该报警）。
#: 用固定文本而不是随机句子，是为了让"报表里的 20 例"每次都能被逐条复跑对上。
REPORT_SAMPLES: tuple[tuple[str, str], ...] = (
    ("24小时累计降雨95毫米，沟道泥位抬升1.2米，下游约300人受威胁", "540121"),
    ("坡面出现裂缝并有石块滚落，暂无其它数据", "540200"),
)

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


def _first_stat(metrics: dict[str, Any], name: str, key: str) -> float | None:
    """取某指标的一个分位数；埋点缺失或无样本一律 None。

    存在理由：报表里"0"与"没测到"必须是两件事。写成 `…get(key, 0.0)` 会把没跑过的腿
    报成"耗时 0 毫秒"，读报表的人会当成达标。
    """
    stats = metrics.get(name)
    if not isinstance(stats, dict) or not stats.get("count"):
        return None
    value = stats.get(key)
    return None if not isinstance(value, (int, float)) else float(value)


def _table(latency: dict[str, Any], collaboration: dict[str, Any]) -> list[dict[str, Any]]:
    """考核指标表：数值与阈值一律取自账本出口，判定只比较同单位的两个数。

    列名跟着单位走（`P95_ms` / `P95_s`），因为这张表里既有毫秒埋点也有秒制埋点——
    把两者塞进同一个 `_ms` 列名就是 1000 倍误读，硬凑一个不带单位的列头又让读者自己猜。
    出口自带的 `unit` 与指标名后缀若不一致，这里直接抛错：一份悄悄错 1000 倍的报表
    比一份没有报表更坏。
    """
    rows: list[dict[str, Any]] = []
    metrics = latency["metrics"]
    for name, description in INDICATORS.items():
        unit = unit_of_metric(name)
        stats = metrics.get(name)
        row: dict[str, Any] = {"指标": description, "样本": 0, "单位": unit}
        for key in ("P50", "P95", "最大"):
            row[f"{key}_{unit}"] = None
        if stats is not None and stats.get("unit", unit) != unit:
            raise ValueError(f"{name}: 出口单位 {stats.get('unit')} 与指标名后缀 {unit} 不一致")
        budget = None if stats is None else stats.get("budget")
        row[f"阈值_{unit}"] = budget
        count = 0 if stats is None else stats["count"]
        row["样本"] = count
        if not count:
            row["判定"] = "未测得（账本里没有这条指标的样本）"
            rows.append(row)
            continue
        for source, label in (("p50", "P50"), ("p95", "P95"), ("max", "最大")):
            row[f"{label}_{unit}"] = stats[source]
        if budget is None:
            row["判定"] = "未设阈值"
        else:
            row["判定"] = "达标" if stats["p95"] <= budget else "超标"
            row["越限样本"] = stats.get("breaches", 0)
        rows.append(row)

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
    reports: int = 2,
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
        # 上报腿：两条固定文本各跑一遍（一条该报警、一条不该报），
        # 让"接入 ≤5min"这项在报表里有一条属于人工上报的独立样本，而不是只蹭遥测那条腿
        submitted = reviewed = with_warning = 0
        for index in range(reports):
            text, region = REPORT_SAMPLES[index % len(REPORT_SAMPLES)]
            outcome = await container.submit_report(note=text, region_code=region, reporter=f"metrics-report-{index + 1}")
            submitted += 1
            reviewed += 1 if outcome["human_review_required"] else 0
            with_warning += 1 if outcome["chain"]["warning_id"] else 0
            fatal += 1 if outcome["chain"]["errors"] else 0
            await drain_pending(container.transport, timeout=5.0)
        # 语义交互这条腿：只读查询走完整轮（意图解构→动作→认知镜像），让 ≤3s 这项在报表里
        # 有自己的样本。只发查询、不发执行类任务，因此不需要 /confirm，也不会有任何动作被执行。
        assistant_rounds = 0
        assistant = getattr(container, "assistant", None)
        if assistant is not None:
            for index in range(max(reports, 1)):
                async for _ in assistant.respond("查一下 540121 的预警", session_id=f"metrics-report-{index}"):
                    pass
                assistant_rounds += 1
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
        "人工上报腿": {
            "受理": submitted,
            "产出预警": with_warning,
            "转人工核签": reviewed,
            "台账计数": latency.get("reports"),
            "阈值版本": latency.get("rulebook"),
        },
        "语义交互腿": {
            "轮次": assistant_rounds,
            # 没有样本就是 None（报表里显示"未测得"），不能拿 0.0 顶上去——那是一行"一轮耗时 0 ms"的假话
            "一轮耗时_ms": _first_stat(latency["metrics"], "assistant_reply_ms", "p50"),
            "说明": "只读查询，未做执行类确认；本机无 LLM 凭据时为「纯规则词表」口径" if assistant_rounds else "助手腿未装配",
        },
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
    parser.add_argument("--reports", type=int, default=2, help="人工上报腿的样本条数（0 即不测这条腿，报表会写「未测得」）")
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
            reports=max(0, args.reports),
            with_agents=args.agents,
            seed=args.seed,
            out=args.out,
            dataset=args.dataset,
            accuracy_target=args.accuracy_target,
        )
    )


if __name__ == "__main__":
    sys.exit(main())
