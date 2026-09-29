"""端到端演练：注入模拟监测数据，跑通"感知—研判—决策—执行—反馈"全链路。

用法：
    uv run python -m scripts.drill --scenario surge --rounds 3 --agents
    uv run python -m scripts.drill --scenario normal --rounds 2      # 负样本：不应告警
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

from aegis.config import Settings
from aegis.connectors.simulator import HazardScenarioSimulator
from aegis.container import create_container
from aegis.domain.messages import utc_now


def _grouped(readings: list[Any]) -> dict[str, list[Any]]:
    grouped: dict[str, list[Any]] = {}
    for reading in readings:
        grouped.setdefault(reading.region_code, []).append(reading)
    return grouped


def _summarize(result: Any) -> dict[str, Any]:
    return {
        "trace_id": result.trace_id,
        "region": result.verdict.region_code if result.verdict else (result.hits[0].region_code if result.hits else "-"),
        "ok": result.ok,
        "acted": result.acted,
        "risk": int(result.verdict.risk_level) if result.verdict else None,
        "hazard": result.verdict.hazard_type.value if result.verdict else None,
        "stages": {s.name: s.mode for s in result.stages},
        "task_units": len(result.task_units),
        "warning_id": result.warning.warning_id if result.warning else None,
        "channels": [d.channel for d in (result.warning.deliveries if result.warning else [])],
        "degradations": result.degradations,
        "errors": result.errors,
    }


async def run_drill(*, scenario: str, rounds: int, with_agents: bool, seed: int) -> int:
    settings = Settings(env="dev", bus_backend="memory", delivery_mode="mock", simulator_seed=seed)
    container = create_container(settings, with_simulator=False)
    await container.start(with_mock_agents=with_agents)
    await asyncio.sleep(0.1)

    simulator = HazardScenarioSimulator(scenario=scenario, seed=seed)
    summaries: list[dict[str, Any]] = []
    try:
        for _round in range(rounds):
            readings = simulator.collect_at(utc_now())
            results = await container.chain.process_many(_grouped(readings))
            summaries.extend(_summarize(r) for r in results)
            await container.transport.idle(timeout=3.0)
            await asyncio.sleep(0.05)
    finally:
        latency = container.latency_report()
        await container.shutdown()

    print(
        json.dumps(
            {"drill": summaries, "metrics": latency["metrics"], "collaboration": latency["collaboration"]}, ensure_ascii=False, indent=2
        )
    )
    fatal = [s for s in summaries if s["errors"]]
    return 1 if fatal else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="AEGIS 端到端演练")
    parser.add_argument("--scenario", choices=["surge", "normal"], default="surge")
    parser.add_argument("--rounds", type=int, default=2, help="注入轮次（每轮覆盖全部模拟站点）")
    parser.add_argument("--agents", action="store_true", help="拉起 Mock 智能体（默认走平台降级路径）")
    parser.add_argument("--seed", type=int, default=202_609)
    args = parser.parse_args()
    if args.rounds < 1:
        parser.error("--rounds 必须 ≥ 1")
    return asyncio.run(run_drill(scenario=args.scenario, rounds=args.rounds, with_agents=args.agents, seed=args.seed))


if __name__ == "__main__":
    sys.exit(main())
