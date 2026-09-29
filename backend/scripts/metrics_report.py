"""考核指标实测报告：把运行时埋点分位数与课题 SLA 阈值并列输出。

原则：报告里的每个数字都来自本次运行的真实样本（计数为 0 即如实标注未测得），
不写"应达到"的期望值。未由本平台数据集覆盖的指标（如预警准确率）显式标记 not_measured。

用法：
    uv run python -m scripts.metrics_report --rounds 20 --agents
    uv run python -m scripts.metrics_report --rounds 50 --out reports/metrics.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from aegis.config import Settings
from aegis.connectors.simulator import HazardScenarioSimulator
from aegis.container import create_container
from aegis.domain.messages import utc_now

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

NOT_MEASURED = {
    "多灾种灾害预警准确率 ≥80%": "需 5 灾种历史灾害案例回放数据集（M4 批次交付），本次运行不覆盖",
    "5 类灾种触发条件识别准确率": "需案例标注测试集，本次运行仅覆盖规则引擎可判定的触发组合",
}


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


async def run(*, rounds: int, with_agents: bool, seed: int, out: Path | None) -> int:
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
            await container.transport.idle(timeout=5.0)
        latency = container.latency_report()
    finally:
        await container.shutdown()

    report = {
        "运行参数": {"rounds": rounds, "with_mock_agents": with_agents, "seed": seed, "事件产出": acted, "致命错误": fatal},
        "指标判定": _table(latency, latency["collaboration"]),
        "未覆盖指标": NOT_MEASURED,
        "原始分位": latency["metrics"],
        "网关计数": latency["gateway_counters"],
        "运行态": latency["store"],
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n[已写入] {out}", file=sys.stderr)
    return 1 if fatal else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="AEGIS 考核指标实测报告")
    parser.add_argument("--rounds", type=int, default=10)
    parser.add_argument("--agents", action="store_true", help="使用 Mock 智能体（否则测平台降级路径）")
    parser.add_argument("--seed", type=int, default=202_609)
    parser.add_argument("--out", type=Path, default=None, help="同时把报告写入 JSON 文件")
    args = parser.parse_args()
    if args.rounds < 1:
        parser.error("--rounds 必须 ≥ 1")
    return asyncio.run(run(rounds=args.rounds, with_agents=args.agents, seed=args.seed, out=args.out))


if __name__ == "__main__":
    sys.exit(main())
