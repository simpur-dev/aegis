"""
RiskAssess 节点（面向西藏五类山地地质灾害，因子化口径）

【本文件角色】评估主节点：校验灾种 → 归一化 Data 输出 → 按灾种路由因子评估工具
              （P/T/R 框架）→ LLM 仅做结果解释（不计算、不定级）。
【设计思想】
  - 数值计算全部在 factor_engine（确定性、可测试、可审计）；
  - risk_level 由引擎按 MOCK 临时阈值给出（provisional_level），
    LLM 不得自行编造风险等级或数值；
  - 灾种 → 工具的映射由 hazard_risk_registry 决定，hazard_type 必须显式传入。
【对外契约】
  读取：task / hazard_type（必填）/ data_result / retry_count
  写入：hazard_type / risk_factors / calculation_result(=因子评估信封) / evidence /
        risk_result / uncertainties / next
  提供给：risk_validate_node、risk_output_node
"""

import json
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from aegis.agents_ext.hazard_registry import (
    UnsupportedHazardError,
    require_hazard,
    supported_hazards_text,
)
from aegis.agents_ext.prompts.risk_prompt import (
    RISK_EXPLANATION_SYSTEM_PROMPT,
    build_risk_explain_user_prompt,
)
from aegis.agents_ext.risk_agent.llm_setup import get_risk_llm
from aegis.agents_ext.risk_agent.state import RiskAgentState
from aegis.agents_ext.risk_agent.tools.hazard_risk_registry import get_hazard_risk_profile
from aegis.agents_ext.risk_agent.tools.risk_evidence_tool import risk_evidence_tool

# ============================================================
# Pydantic：LLM 仅做解释（★ 不含 risk_level / 任何数值决策字段 ★）
# ============================================================


class RiskExplanation(BaseModel):
    """LLM 对因子引擎结果的解释（数字必须逐字引用引擎输出）。"""

    reasoning: str = Field(description="判断依据：必须逐字引用因子引擎输出的 P/T/R 与关键因子值")
    risk_factors: list[str] = Field(description="本次数据体现的风险因素（从该灾种典型因素中选取并说明）")
    uncertainties: list[str] = Field(description="不确定性/数据局限说明")
    needs_more_data: bool = Field(description="是否因数据不足无法可靠解释")
    missing_info: list[str] = Field(description="缺失的关键信息")


# ============================================================
# ★ 关键适配层：把 Data 的 data_result 归一化（灾种无关，保留复用）★
# ============================================================


def _normalize_data_result(data_result: dict) -> dict:
    """兼容 Data Agent 两种可能的输出结构，统一提取 events/status/quality/source。"""
    if not isinstance(data_result, dict):
        return {"events": [], "status": "failed", "data_quality": {}, "missing_data": [], "source": "unknown"}

    inner = data_result.get("processed_data", {})

    if isinstance(inner, dict) and "events" in inner:
        events = inner.get("events", [])
        source = inner.get("data_source") or inner.get("source", "unknown")
    elif isinstance(inner, dict) and isinstance(inner.get("processed_data"), dict):
        deep = inner["processed_data"]
        events = deep.get("events", [])
        source = deep.get("data_source") or deep.get("source", "unknown")
    else:
        events = []
        source = "unknown"

    return {
        "events": events,
        "status": data_result.get("status", "unknown"),
        "data_quality": data_result.get("data_quality", {}),
        "missing_data": data_result.get("missing_data", []),
        "source": source,
    }


def _summarize_data(norm: dict) -> str:
    return (
        f"Data Agent 状态={norm['status']}，观测记录数={len(norm['events'])}，"
        f"数据源={norm['source']}（Mock 数据），"
        f"样例={json.dumps(norm['events'][:3], ensure_ascii=False)}"
    )


def _insufficient_return(
    hazard_key: str, hazard_raw: str, profile: dict, reasoning: str, missing_info: list, error_message: str, assessment: dict | None = None
) -> dict:
    """数据不足/灾种无效的统一出图结构（不产证据，needs_more_data=True）。"""
    if assessment is None:
        # 引擎未运行的场景（灾种无效/无数据）：声明引擎身份与未运行状态
        from aegis.agents_ext.risk_agent.tools.factor_engine import (
            FACTOR_ENGINE_VERSION,
            R_FORMULA_EXPRESSION,
            R_FORMULA_NOTE,
            R_FORMULA_STATUS,
        )

        assessment = {
            "engine_version": FACTOR_ENGINE_VERSION,
            "assessment_status": "not_run",
            "formula": {"expression": R_FORMULA_EXPRESSION, "status": R_FORMULA_STATUS, "note": R_FORMULA_NOTE},
            "P": None,
            "T": None,
            "R": None,
            "factor_scores": {},
            "completeness": {},
            "missing_factors": [],
            "rule_hits": [],
            "insufficient_reasons": [error_message],
        }
    return {
        "hazard_type": hazard_key or hazard_raw,
        "risk_factors": list(profile.get("risk_factors", [])) if profile else [],
        "calculation_result": assessment,
        "evidence": [],
        "risk_result": {
            "hazard_type": hazard_key or hazard_raw,
            "risk_level": "unknown",
            "reasoning": reasoning,
            "needs_more_data": True,
            "missing_info": missing_info,
            "trigger_conditions": [],
            "rule_version": profile.get("rule_version", "") if profile else "",
            "implementation_status": profile.get("implementation_status", "") if profile else "",
            "assessment": assessment,
        },
        "uncertainties": ["数据不足或灾种无效，未进行数值评估（诚实原则：不强制给出等级）"],
        "next": "output",
        "error_message": error_message,
    }


def risk_assess_node(state: RiskAgentState) -> dict[str, Any]:
    """风险评估节点（按灾种路由因子评估；LLM 仅解释）。"""

    task = state.get("task", "")
    data_result = state.get("data_result", {})
    hazard_raw = state.get("hazard_type", "") or ""
    retry_count = state.get("retry_count", 0)

    # ---------- 步骤0：灾种校验（显式必填）----------
    try:
        hazard_key = require_hazard(hazard_raw)
    except UnsupportedHazardError as e:
        print(f"[RiskAssess] 灾种无效: {e}")
        return _insufficient_return(
            "",
            hazard_raw,
            {},
            reasoning=f"灾种未提供或无法识别，无法选择评估规则。{e}",
            missing_info=[f"hazard_type: 未提供或无法识别（当前支持：{supported_hazards_text()}）"],
            error_message=str(e),
        )

    profile = get_hazard_risk_profile(hazard_key)

    # ---------- 步骤1：归一化 Data 输出 ----------
    norm = _normalize_data_result(data_result)
    events = norm["events"]
    region = events[0].get("region", "unknown") if events else "unknown"

    # ---------- 情况A：无有效数据 ----------
    if not events:
        print("[RiskAssess] 数据不足，跳过因子引擎，标记 insufficient_data")
        return _insufficient_return(
            hazard_key,
            hazard_raw,
            profile,
            reasoning="Data Agent 未提供有效灾种观测数据",
            missing_info=[f"{profile['hazard_type_cn']}观测数据"],
            error_message="数据不足：无有效观测记录",
        )

    # ---------- 情况B：按灾种调用因子评估工具（P/T/R 框架）----------
    assessment = profile["assessment_tool"](events, region)
    print(
        f"[RiskAssess] 灾种={hazard_key} 引擎={assessment['engine_version']} "
        f"状态={assessment['assessment_status']} P={assessment['P']} "
        f"T={assessment['T']} R={assessment['R']} 覆盖率={assessment['completeness']}"
    )

    # ---------- 情况C：引擎判定数据不足（必需因子缺失/覆盖率低于门控）----------
    if assessment["assessment_status"] == "insufficient_data":
        missing_info = [
            f"{k}: {profile['hazard_type_cn']}评估必需因子"
            for k in assessment["missing_factors"]
            if k in assessment.get("required_factors", [])
        ] or [f"{k}: 因子无观测值" for k in assessment["missing_factors"]] + list(assessment.get("insufficient_reasons", []))
        return _insufficient_return(
            hazard_key,
            hazard_raw,
            profile,
            reasoning=("数据覆盖不足，因子引擎拒绝评分（诚实原则：缺失数据不填 0、不输出貌似精确的分数）"),
            missing_info=missing_info,
            error_message="；".join(assessment["insufficient_reasons"]),
            assessment=assessment,  # >>> 引擎信封随 insufficient 输出，供诊断
        )

    # ---------- 情况D：LLM 解释（不计算、不定级；失败走模板兜底）----------
    last_issues = state.get("validation_result", {}).get("issues", [])
    rule_hits = assessment.get("rule_hits", [])
    # 触发条件以"保护规则命中"为主（docx 的扳机要素，独立于综合指数）
    trigger_conditions = [f"{h['rule_id']}: {h['description']}（建议等级 {h.get('level_suggest', '')}）" for h in rule_hits]

    llm = get_risk_llm()
    structured_llm = llm.with_structured_output(RiskExplanation)
    try:
        r: RiskExplanation = structured_llm.invoke(
            [
                SystemMessage(content=RISK_EXPLANATION_SYSTEM_PROMPT),
                HumanMessage(
                    content=build_risk_explain_user_prompt(
                        task=task,
                        hazard_type=hazard_key,
                        hazard_type_cn=profile["hazard_type_cn"],
                        risk_factors=profile["risk_factors"],
                        assessment_json=json.dumps(assessment, ensure_ascii=False),
                        retry_count=retry_count,
                        previous_issues="; ".join(last_issues) if last_issues else "",
                    )
                ),
            ]
        )
        reasoning = r.reasoning
        risk_factors = r.risk_factors or profile["risk_factors"]
        uncertainties = r.uncertainties
    except Exception as e:
        print(f"[RiskAssess] LLM 解释失败，模板兜底: {e}")
        reasoning = (
            f"因子引擎输出：P={assessment['P']}，T={assessment['T']}，"
            f"R={assessment['R']}（原型候选公式）；保护规则命中 "
            f"{len(rule_hits)} 项。LLM 不可用，本段为模板兜底说明。"
        )
        risk_factors = profile["risk_factors"]
        uncertainties = ["LLM 调用失败，本段为模板兜底说明，可靠性有限"]

    risk_result = {
        "hazard_type": hazard_key,
        "risk_level": assessment["provisional_level"],  # ★ 引擎 MOCK 阈值给出，非 LLM
        "reasoning": reasoning,
        "needs_more_data": False,
        "missing_info": assessment["missing_factors"],
        "trigger_conditions": trigger_conditions,
        "rule_version": assessment["rule_version"],
        "implementation_status": profile["implementation_status"],
        "assessment": assessment,  # ★ 完整因子信封（含公式身份声明）
    }

    uncertainties_out = list(uncertainties) + list(assessment.get("warnings", []))
    uncertainties_out.append(
        f"R 公式为项目原型自拟候选组合公式（{assessment['formula']['expression']}），未经历史数据验证，不构成真实预警能力"
    )

    print(f"[RiskAssess] 临时等级={risk_result['risk_level']}（MOCK 阈值）")

    return {
        "hazard_type": hazard_key,
        "risk_factors": risk_factors,
        "calculation_result": assessment,  # >>> 因子评估信封（对齐旧字段名，便于下游）
        "risk_result": risk_result,
        "evidence": risk_evidence_tool(assessment, hazard_limitations=profile["current_limitations"]),
        "uncertainties": uncertainties_out,
        "next": "validate",
    }
