"""
ScenarioValidate 节点 —— 情景与参数校验

【本文件角色】
  情景配置合法性校验：
  - 主判据 = 确定性校验（validate_scenarios_tool：schema 唯一性 + 模型参数范围）
  - 辅助 = LLM 诚实性审查（不可用时跳过，仅保留确定性结论）

【对外契约】
  读取：scenario_configs / hazard_type / retry_count
  写入：validated_scenarios / validation_result / retry_count / next
  路由：route_after_scenario_validate 读 validation_result.recommendation
        （fail → 直送输出；retry 且预算未耗尽 → 回设计节点；其余 → 路由节点）

【重试语义（修正 Risk Agent 恒不自增的缺陷模式）】
  recommendation == "retry" 时，在本节点同一次 return 里自增 retry_count，
  route_after_scenario_validate 读自增后的 retry_count < max_retries 判断回环。
"""

import json

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from aegis.agents.hazards.prompts.simulation_prompt import (
    SCENARIO_VALIDATE_SYSTEM_PROMPT,
    build_scenario_validate_user_prompt,
)
from aegis.agents.hazards.simulation_agent.llm_setup import get_simulation_llm
from aegis.agents.hazards.simulation_agent.tools.scenario_validate_tool import validate_scenarios_tool


class ScenarioReview(BaseModel):
    """LLM 诚实性审查输出。"""

    is_valid: bool = Field(description="整体是否可用")
    issues: list[str] = Field(default_factory=list, description="发现的问题（重试时设计节点会看到）")
    recommendation: str = Field(description="proceed / retry / fail")


def scenario_validate_node(state: dict) -> dict:
    """情景校验节点。"""
    configs = state.get("scenario_configs", []) or []
    hazard_raw = state.get("hazard_type", "") or ""
    retry_count = state.get("retry_count", 0)

    # ---------- 1) 确定性校验（主判据，不依赖 LLM）----------
    report = validate_scenarios_tool(configs, hazard_raw)
    valid = report["valid_scenarios"]
    invalid = report["invalid_details"]
    print(f"[ScenarioValidate] 确定性校验：总数={report['total']} 通过={report['valid_count']} 不通过={len(invalid)}")

    issues: list[str] = []
    review_dict = None

    # ---------- 2) 判定 recommendation ----------
    if report.get("unsupported_reason"):
        # 灾种不支持（防御分支：design 前置检查通常已拦截）
        recommendation = "fail"
        issues = [report["unsupported_reason"]]
    elif not valid:
        recommendation = "fail"
        issues = [f"全部 {report['total']} 个情景未通过确定性校验"] + [f"{d['scenario_id']}: {'；'.join(d['issues'])}" for d in invalid]
    else:
        # 至少一个情景合法 → LLM 诚实性审查（不可用则跳过）
        recommendation = "proceed"
        try:
            llm = get_simulation_llm()
            structured = llm.with_structured_output(ScenarioReview)
            review: ScenarioReview = structured.invoke(
                [
                    SystemMessage(content=SCENARIO_VALIDATE_SYSTEM_PROMPT),
                    HumanMessage(
                        content=build_scenario_validate_user_prompt(
                            scenario_configs_str=json.dumps(configs, ensure_ascii=False, indent=1),
                            deterministic_summary=(
                                f"总数={report['total']}，通过={report['valid_count']}，"
                                f"未通过明细={json.dumps(invalid, ensure_ascii=False)}"
                            ),
                        )
                    ),
                ]
            )
            review_dict = review.model_dump()
            issues = list(review.issues or [])
            if review.recommendation in ("retry", "fail"):
                recommendation = review.recommendation
        except Exception as e:
            print(f"[ScenarioValidate] LLM 审查不可用，仅依据确定性校验: {e}")

    # ---------- 3) 重试预算：retry 判定与自增原子绑定 ----------
    new_retry_count = retry_count + 1 if recommendation == "retry" else retry_count

    print(f"[ScenarioValidate] 建议={recommendation} 问题数={len(issues)} retry_count={new_retry_count}")

    return {
        "validated_scenarios": valid,
        "validation_result": {
            "is_valid": recommendation == "proceed",
            "recommendation": recommendation,
            "issues": issues,
            "invalid_details": invalid,
            "deterministic_check": {
                "total": report["total"],
                "valid_count": report["valid_count"],
                "all_invalid": report["all_invalid"],
            },
            "llm_review": review_dict,
        },
        "retry_count": new_retry_count,  # >>> ★ 与 retry 判定原子绑定
        "next": "hazard_model_router" if recommendation in ("proceed", "retry") else "simulation_output",
    }
