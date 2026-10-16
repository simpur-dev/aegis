"""质量表回归门禁脚本（§4.5 / D8）：CI 合入前跑一遍，任一回退即不过、退出码非零。

    uv run python -m scripts.quality_gate --baseline baselines/quality_baseline.json   # 门禁：回退→退出码 1
    uv run python -m scripts.quality_gate --write-baseline baselines/quality_baseline.json  # 重测后刷新基线
    uv run python -m scripts.quality_gate --json reports/ci-quality.json               # 只留完整结果档
    uv run python -m scripts.quality_gate --usage reports/llm_usage.json              # 用真跑出来的用量台账算首过率

诚实口径：只有【STU合法率】能离线确定性实算（走 plan 本地剧本，无 LLM/无外部服务）；
【结构化输出首过率】要有真调用落出的用量台账（`--usage`，由智能体真跑导出），CI 无密钥即 None；
触发F1 / 定级一致率 / 文案裁判分需金标集与裁判模型（数据/密钥门）。未评测的列既不冒充通过也不冒充退步。
基线只比"两边都有数"的列——None 不参与判定（见 regression_gate）。
基线档必须由 `--write-baseline` 生成且各列齐全：把 `--json` 的报告档当基线读，各列会全读成 None、
门禁就此永绿——所以缺键一律直接报错，不出一个"看起来在把关"的空闸。
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from aegis.agents.plan import PlanAgent
from aegis.contracts import ContractRegistry
from aegis.domain.enums import AgentType
from aegis.domain.messages import make_request
from aegis.services.quality_eval import QualityMetrics, regression_gate, structured_first_pass_rate, stu_legality_rate

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


async def current_quality(contracts: ContractRegistry, *, usage_rows: list[dict[str, Any]] | None = None) -> QualityMetrics:
    rate, _bad = stu_legality_rate(await plan_playbook_stu_units(contracts), contracts)
    first_pass, _breakdown = structured_first_pass_rate(usage_rows or [])
    # 其余列（触发F1/定级一致率/文案裁判分）需金标集与裁判模型 → None（不冒充）。
    return QualityMetrics(stu_legality=rate, structured_first_pass=first_pass)


_COLUMNS = ("trigger_f1", "level_agreement", "stu_legality", "copy_judge_score", "structured_first_pass")


def _load_usage(path: str) -> list[dict[str, Any]]:
    """读网关用量台账档（`LlmCallRecord.as_dict()` 的数组，或 `{"usage": [...]}` 的导出）。

    三道形状闸，缺一即响亮报错而不是返回空表：① 空档/非数组；② 行不是对象；
    ③ 行缺 `structured`/`attempt`/`validation` 任一键。第三道是给"旧版网关导的档"设的——
    那种档每行都没有这两格，函数会把整份台账读成"没有结构化调用"→ None → 这一列不参与判定，
    于是拿一个填对的文件名就能把闸门悄悄关掉，而它看起来完全正常。
    """
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = raw.get("usage") if isinstance(raw, dict) else raw
    if not isinstance(rows, list) or not rows:
        raise ValueError(f"用量台账 {path} 为空或不是调用行数组（要的是网关 usage_records 的导出）——空档会把首过率这一列静默关掉")
    required = ("structured", "attempt", "validation")
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or any(key not in row for key in required):
            raise ValueError(
                f"用量台账 {path} 第 {index} 行不是带 {'/'.join(required)} 三键的调用行——"
                "旧版网关导的档算不出首过率，读成 None 等于悄悄关掉这一列的闸门"
            )
    return [dict(row) for row in rows]


def _load_baseline(path: str) -> QualityMetrics:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    missing = [col for col in _COLUMNS if col not in raw]
    if missing:
        raise ValueError(f"基线文件 {path} 缺列 {missing}——基线须由 --write-baseline 生成，缺列会被读成未评测、闸门锁不上")
    return QualityMetrics(**{col: raw[col] for col in _COLUMNS})


def _blanking_columns(target: Path, current: dict[str, Any]) -> list[str]:
    """刷新基线前先算"这次会把哪几列从有数写成 None"。

    None 不参与退步判定，所以一次没带 `--usage` 的 `--write-baseline` 就能把已实测的列悄悄关掉——
    从此那一列永远绿。要抹数得显式承认（`--allow-blank`），不给静默通道。
    """
    if not target.exists():
        return []
    previous = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(previous, dict):
        raise ValueError(f"基线文件 {target} 不是对象档——拒绝覆盖一个读不出列的文件")
    return [column for column in _COLUMNS if previous.get(column) is not None and current.get(column) is None]


async def _run(args: argparse.Namespace) -> int:
    contracts = ContractRegistry()
    usage_rows = _load_usage(args.usage) if args.usage else None
    current = await current_quality(contracts, usage_rows=usage_rows)
    if args.write_baseline:
        target = Path(args.write_baseline)
        lost = _blanking_columns(target, current.as_dict())
        if lost and not args.allow_blank:
            print(
                json.dumps(
                    {
                        "refused": True,
                        "would_blank_columns": lost,
                        "hint": "这些列在现有基线里是有数的实测值；本次运行没跑出对应输入（如首过率缺 --usage 台账）。"
                        "带 --usage 重测，或确要清空时加 --allow-blank 并说明理由。",
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 2
        target.write_text(json.dumps(current.as_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    baseline = _load_baseline(args.baseline) if args.baseline else QualityMetrics()
    result = regression_gate(current, baseline)
    report = {
        "quality_table": current.as_dict(),
        "baseline_present": args.baseline is not None,
        "gate": result.as_dict(),
        "note": "STU合法率离线实算；首过率来自 --usage 的真调用台账（无台账=None，未评测）；"
        "触发F1/定级一致率/文案裁判分未评测=None，不冒充达标或退步。",
    }
    if usage_rows is not None:
        _rate, breakdown = structured_first_pass_rate(usage_rows)
        report["first_pass_ledger"] = {"source": args.usage, **breakdown}
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.json:
        Path(args.json).write_text(text, encoding="utf-8", newline="\n")
    print(text)
    return 0 if result.passed else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="质量表回归门禁")
    parser.add_argument("--baseline", default=None, help="基线质量表 JSON；给出则任一回退判不过")
    parser.add_argument("--write-baseline", dest="write_baseline", default=None, help="把本次实测各列写成基线 JSON")
    parser.add_argument("--usage", default=None, help="真跑导出的网关用量台账 JSON；算结构化输出首过率")
    parser.add_argument(
        "--allow-blank",
        dest="allow_blank",
        action="store_true",
        help="确认要把基线里已有数的列写成 None（未重测即清空会悄悄关掉那一列的闸门）",
    )
    parser.add_argument("--json", dest="json", default=None, help="把结果写到该路径")
    args = parser.parse_args(argv)
    if args.baseline and args.write_baseline and Path(args.baseline) == Path(args.write_baseline):
        # 同一文件：先写后读会让当前值变成自己的基线，门禁永远绿——必须拒绝而不是静默自基线。
        parser.error("--baseline 与 --write-baseline 不能是同一个文件")
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
