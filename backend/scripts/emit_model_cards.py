"""模型卡片 + 质量表取证入口（§4.5/§5）。

    uv run python -m scripts.emit_model_cards                # 打印
    uv run python -m scripts.emit_model_cards --json out.json # 落盘给 REPORT 引用

诚实口径：
- 模型卡片只陈述真实配置（路由模型 / 已迁入的 prompt 版本），训练集与评测分未做即 None，不编数。
- 质量表里**只有 STU合法率**能在此确定性实算（走 plan 本地剧本路径，复用契约校验）；
  触发F1 / 定级一致率 / 文案裁判分需金标集 + 裁判模型（数据/密钥门），此处如实标 None=未评测，
  绝不放进基线比较冒充达标。
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from aegis.agents.plan import PlanAgent
from aegis.bus.gateway import ContractRegistry
from aegis.config import get_settings
from aegis.domain.enums import AgentType
from aegis.domain.messages import make_request
from aegis.services.model_cards import build_model_cards
from aegis.services.prompt_registry import PromptRegistry
from aegis.services.quality_eval import QualityMetrics, stu_legality_rate

TRACE = "trc_" + "0" * 16


async def _plan_playbook_units() -> list[dict[str, Any]]:
    """用无 LLM 的 plan 本地剧本路径产一批真实 STU，量其合法率（确定性、可复算）。"""
    agent = PlanAgent(transport=None, agent_type=AgentType.PLAN, agent_id="plan.card", capabilities=["task_decompose"], llm=None)  # type: ignore[arg-type]
    request = make_request(
        source="platform.script",
        target="plan.card",
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
        reply_to="reply.gwcards.inbox",
        deadline_ms=3000,
    )
    reply = await agent.handle(request)
    return list(reply.payload.get("task_units") or [])


async def _build_report() -> dict[str, Any]:
    cards = build_model_cards(get_settings(), PromptRegistry.builtin())
    units = await _plan_playbook_units()
    rate, bad = stu_legality_rate(units, ContractRegistry())
    metrics = QualityMetrics(stu_legality=rate)
    return {
        "model_cards": cards,
        "quality_table": metrics.as_dict(),
        "measured_columns": ["stu_legality"],
        "stu_legality_detail": {"rate": rate, "illegal_count": bad, "units": len(units)},
        "note": "触发F1/定级一致率/文案裁判分 = None：待金标集+裁判模型评测后回填，未跑即不冒充达标。",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="模型卡片与质量表取证")
    parser.add_argument("--json", dest="json_out", default=None, help="把结果写到该路径")
    args = parser.parse_args(argv)
    report = asyncio.run(_build_report())
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.json_out:
        Path(args.json_out).write_text(text, encoding="utf-8", newline="\n")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
