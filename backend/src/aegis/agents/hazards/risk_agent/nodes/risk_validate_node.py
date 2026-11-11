"""
RiskValidate 节点

【本文件角色】审查评估结果的证据充分性/一致性/诚实性，写 validation_result。
【对齐你 Data】你 Data 的 validate 写 data_quality、路由读 data_quality；
              这里 validate 写 validation_result、路由读 validation_result。对称。
【不重新评估风险】只判断"这份评估能不能用"。
【对外契约】
  读取：risk_result / evidence / uncertainties（assess 写的）
  写入：validation_result（>>> 给 graph 路由函数读，决定 proceed/retry/output）
"""

import json
import logging
from typing import Any

from pydantic import BaseModel, Field

from aegis.agents.core.meta import llm_call_meta
from aegis.agents.core.prompts import build_prompt
from aegis.agents.hazards.risk_agent.llm_setup import get_risk_llm
from aegis.agents.hazards.risk_agent.state import RiskAgentState

log = logging.getLogger(__name__)


class RiskValidationOutput(BaseModel):
    is_valid: bool = Field(description="整体是否可信")
    evidence_sufficient: bool = Field(description="证据是否充分")
    consistency_ok: bool = Field(description="结论与证据是否一致")
    issues: list[str] = Field(description="发现的问题")
    recommendation: str = Field(description="proceed / retry / fail")


def risk_validate_node(state: RiskAgentState) -> dict[str, Any]:
    """风险验证节点。"""

    risk_result = state.get("risk_result", {})
    evidence = state.get("evidence", [])
    uncertainties = state.get("uncertainties", [])

    # 数据不足分支（assess 已标记）：不验证，放行 output
    if risk_result.get("needs_more_data") and not evidence:
        log.info("[RiskValidate] 数据不足，跳过验证")
        return {
            "validation_result": {
                "is_valid": False,
                "recommendation": "proceed",  # 放行，由 output 标 insufficient_data
                "issues": ["无数据"],
            },
            "next": "output",
        }

    llm = get_risk_llm()
    structured_llm = llm.with_structured_output(RiskValidationOutput)
    try:
        messages, prompt_version = build_prompt(
            "hazard.risk_validate",
            risk_result=json.dumps(risk_result, ensure_ascii=False),
            evidence=json.dumps(evidence, ensure_ascii=False),
            uncertainties=json.dumps(uncertainties, ensure_ascii=False),
        )
        with llm_call_meta(task="hazard.risk_validate", prompt_version=prompt_version, structured=True):
            r = structured_llm.invoke(messages)
        validation = {
            "is_valid": r.is_valid,
            "evidence_sufficient": r.evidence_sufficient,
            "consistency_ok": r.consistency_ok,
            "issues": r.issues,
            "recommendation": r.recommendation,
        }
    except Exception as e:
        # 兜底：验证 LLM 不可用时用规则粗判，避免卡死
        log.warning("[RiskValidate] LLM 验证失败，规则兜底: %s", e)
        ok = len(evidence) > 0
        validation = {
            "is_valid": ok,
            "evidence_sufficient": ok,
            "consistency_ok": True,
            "issues": [] if ok else ["无证据"],
            "recommendation": "proceed" if ok else "retry",
        }

    log.info("[RiskValidate] 建议=%s 问题=%s", validation["recommendation"], validation["issues"])

    # ---------- ★ 缺陷修复：retry 判定时自增 retry_count ----------
    # 此前 assess / validate / output 三个节点都不写 retry_count，而 graph.py 的
    # route_after_validate 用 retry_count < max_retries 判断是否回环评估，
    # retry_count 恒为 0 → LLM 持续给 "retry" 时必然死循环，
    # 直到 LangGraph 默认 recursion_limit(25) 抛 GraphRecursionError。
    # 修复方式与 data_validate_node 的写法保持一致：判 retry 时 +1。
    retry_count = state.get("retry_count", 0)
    if validation.get("recommendation") == "retry":
        retry_count = retry_count + 1

    return {
        "validation_result": validation,  # >>> 给 graph 路由读
        "retry_count": retry_count,  # >>> ★ 新增：供路由判断是否超过重试上限
        "next": "validate_done",  # 记录用
    }
