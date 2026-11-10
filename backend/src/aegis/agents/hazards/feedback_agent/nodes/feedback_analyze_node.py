"""
FeedbackAnalyze 节点（确定性检查 + LLM 解释）

【本文件角色】
  1. 调用 feedback_check_tool.run_deterministic_checks 完成全部确定性检查
     （字段完整性 / 灾种一致性 / 状态级联 / 版本匹配 / 数据时效 /
      Mock 声明一致性 / Warning 与上游一致性 / 灾种差异化关注项）；
  2. （可选）LLM 仅对已发现的问题做通俗解释，不新增"问题判定"；
     LLM 不可用时跳过解释，流程不崩。

【红线】问题是否存在只由确定性证据决定；LLM 不得认定专业计算错误。

【对外契约】
  读取：collected_context / data/risk/simulation/warning_result / external_feedback
  写入：detected_issues / analysis_notes / next
"""

import json
from typing import Any

from aegis.agents.hazards.feedback_agent.state import FeedbackAgentState
from aegis.agents.hazards.feedback_agent.tools.feedback_check_tool import (
    run_deterministic_checks,
)


def _llm_commentary(state, issues) -> dict:
    """LLM 对检查结果做解释（仅解释，不判定；失败走空说明兜底）。"""
    if not issues:
        return {"commentary": "", "used_llm": False, "note": "确定性检查未发现问题，无需解释"}
    try:
        from langchain_core.messages import HumanMessage, SystemMessage
        from pydantic import BaseModel, Field

        from aegis.agents.hazards.feedback_agent.llm_setup import get_feedback_llm
        from aegis.agents.hazards.prompts.feedback_prompt import FEEDBACK_ANALYZE_PROMPT, build_analyze_user_prompt

        class AnalyzeCommentary(BaseModel):
            commentary: str = Field(description="对已发现问题的通俗解释与影响说明")
            manager_attention: list = Field(default_factory=list, description="值得 Manager 注意的流程性提示（非新问题判定）")

        llm = get_feedback_llm()
        structured = llm.with_structured_output(AnalyzeCommentary)
        r = structured.invoke(
            [
                SystemMessage(content=FEEDBACK_ANALYZE_PROMPT),
                HumanMessage(
                    content=build_analyze_user_prompt(
                        context_json=json.dumps(state.get("collected_context", {}), ensure_ascii=False),
                        issues_json=json.dumps(issues, ensure_ascii=False),
                    )
                ),
            ]
        )
        return {"commentary": r.commentary, "manager_attention": list(r.manager_attention or []), "used_llm": True}
    except Exception as e:
        print(f"[FeedbackAnalyze] LLM 解释不可用，跳过（确定性结果不受影响）: {e}")
        return {"commentary": "", "used_llm": False, "llm_error": str(e)}


def feedback_analyze_node(state: FeedbackAgentState) -> dict[str, Any]:
    """反馈分析节点：确定性检查为主，LLM 解释为辅。"""
    issues = run_deterministic_checks(
        event_id=state.get("event_id", ""),
        hazard_type=state.get("hazard_type", ""),
        data_result=state.get("data_result", {}) or {},
        risk_result=state.get("risk_result", {}) or {},
        simulation_result=state.get("simulation_result", {}) or {},
        warning_result=state.get("warning_result", {}) or {},
        external_feedback=state.get("external_feedback") or None,
    )

    analysis_notes = _llm_commentary(state, issues)

    print(f"[FeedbackAnalyze] 确定性检查发现问题数={len(issues)} 严重度分布={{" + ", ".join(sorted({i["severity"] for i in issues})) + "}")

    return {
        "detected_issues": issues,
        "analysis_notes": analysis_notes,
        "next": "feedback_evaluate",
    }
