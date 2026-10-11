"""质量四表回归门禁脚本（§4.5 / D8）：CI 合入前跑一遍，任一回退即不过、退出码非零。

    uv run python -m scripts.quality_gate --baseline baselines/quality_baseline.json   # 门禁：回退→退出码 1
    uv run python -m scripts.quality_gate --write-baseline baselines/quality_baseline.json  # 重测后刷新基线
    uv run python -m scripts.quality_gate --json reports/ci-quality.json               # 只留完整结果档

诚实口径：只有【STU合法率】能确定性实算（走 plan 本地剧本，无 LLM/无外部服务）；触发F1 /
定级一致率 / 文案裁判分需金标集与裁判模型（数据/密钥门），未评测即 None，既不冒充通过也不冒充退步。
基线只比"两边都有数"的列——None 不参与判定（见 regression_gate）。
基线档必须由 `--write-baseline` 生成且四列齐全：把 `--json` 的报告档当基线读，四列会全读成 None、
门禁就此永绿——所以缺键一律直接报错，不出一个"看起来在把关"的空闸。
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from aegis.agents.plan import PlanAgent
from aegis.bus.gateway import ContractRegistry
from aegis.domain.enums import AgentType
from aegis.domain.messages import make_request
from aegis.services.quality_eval import QualityMetrics, regression_gate, stu_legality_rate

TRACE = "trc_" + "0" * 16


async def plan_playbook_stu_units(contracts: ContractRegistry) -> list[dict[str, Any]]:
    """无 LLM 的 plan 本地剧本路径产一批真实 STU（确定性），供量 STU合法率。"""
    agent = PlanAgent(
        transport=None, agent_type=AgentType.PLAN, agent_id="plan.gate", capabilities=["task_decompose"], llm=None, contracts=contracts
    )  # type: ignore[arg-type]
    request = make_request(
        source="platform.gate",
        target="plan.gate",
        action="plan.stu",
        payload={
            "event_id": "evt_" + "0" * 12,
            "risk": {
                "hazard_type": "debris_flow",
                "region_code": "540121",
                "risk_level": 1,
                "confidence": 0.9,
                "rationale": "强降雨",
                "evidence_refs": ["R-1"],
            },
            "objective": "处置",
            "constraints": {},
            "reference_cases": [],
            "retrieved_context": [],
        },
        trace_id=TRACE,
        reply_to="reply.gate.inbox",
        deadline_ms=3000,
    )
    reply = await agent.handle(request)
    return list(reply.payload.get("task_units") or [])


async def current_quality(contracts: ContractRegistry) -> QualityMetrics:
    rate, _bad = stu_legality_rate(await plan_playbook_stu_units(contracts), contracts)
    return QualityMetrics(stu_legality=rate)  # 其余三列未评测 → None（不冒充）


_COLUMNS = ("trigger_f1", "level_agreement", "stu_legality", "copy_judge_score")


def _load_baseline(path: str) -> QualityMetrics:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    missing = [col for col in _COLUMNS if col not in raw]
    if missing:
        raise ValueError(f"基线文件 {path} 缺列 {missing}——基线须由 --write-baseline 生成，缺列会被读成未评测、闸门锁不上")
    return QualityMetrics(**{col: raw[col] for col in _COLUMNS})


async def _run(args: argparse.Namespace) -> int:
    contracts = ContractRegistry()
    current = await current_quality(contracts)
    if args.write_baseline:
        Path(args.write_baseline).write_text(json.dumps(current.as_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    baseline = _load_baseline(args.baseline) if args.baseline else QualityMetrics()
    result = regression_gate(current, baseline)
    report = {
        "quality_table": current.as_dict(),
        "baseline_present": args.baseline is not None,
        "gate": result.as_dict(),
        "note": "仅 STU合法率确定性实算；触发F1/定级一致率/文案裁判分未评测=None，不冒充达标或退步。",
    }
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.json:
        Path(args.json).write_text(text, encoding="utf-8")
    print(text)
    return 0 if result.passed else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="质量四表回归门禁")
    parser.add_argument("--baseline", default=None, help="基线四表 JSON；给出则任一回退判不过")
    parser.add_argument("--write-baseline", dest="write_baseline", default=None, help="把本次实测四表写成基线 JSON")
    parser.add_argument("--json", dest="json", default=None, help="把结果写到该路径")
    args = parser.parse_args(argv)
    if args.baseline and args.write_baseline and Path(args.baseline) == Path(args.write_baseline):
        # 同一文件：先写后读会让当前值变成自己的基线，门禁永远绿——必须拒绝而不是静默自基线。
        parser.error("--baseline 与 --write-baseline 不能是同一个文件")
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
