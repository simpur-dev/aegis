"""
LLMExplanation 节点（预警文本解释）

【本文件角色】
  把【已经确定】的结构化预警对象转化为可读的预警说明文本。
  LLM 只做文字组织：【不得】改变等级、编造时间/坐标/灾情数值/提前量、
  把模拟假设描述成已发生事实、把不确定结果描述成确定预测。
  LLM 失败时走模板兜底，流程不崩。
"""

import json
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from aegis.agents.hazards.prompts.warning_prompt import (
    LEVEL_KEYWORDS,
    WARNING_EXPLANATION_SYSTEM_PROMPT,
    build_explain_user_prompt,
)
from aegis.agents.hazards.warning_agent.llm_setup import get_warning_llm
from aegis.agents.hazards.warning_agent.state import WarningAgentState


class AlertExplanation(BaseModel):
    """LLM 输出的预警说明（仅文字组织，无决策字段）。"""

    summary: str = Field(description="预警说明摘要（等级/灾种/依据必须与结构化对象一致）")
    bullet_points: list[str] = Field(description="分条说明（依据、数据来源、时效等）")
    cautions: list[str] = Field(description="必须包含的不确定性与 Mock 声明")


def llm_explanation_node(state: WarningAgentState) -> dict[str, Any]:
    """预警文本解释节点（LLM 失败走模板兜底）。"""

    alert = state.get("alert_object", {}) or {}
    retry_count = state.get("retry_count", 0)
    last_issues = (state.get("validation_result", {}) or {}).get("issues", []) or []

    level = alert.get("warning_level", "none")
    level_keyword = LEVEL_KEYWORDS.get(level, "")
    try:
        llm = get_warning_llm()
        structured = llm.with_structured_output(AlertExplanation)
        r = structured.invoke(
            [
                SystemMessage(content=WARNING_EXPLANATION_SYSTEM_PROMPT),
                HumanMessage(
                    content=build_explain_user_prompt(
                        alert_json=json.dumps(alert, ensure_ascii=False, indent=1),
                        required_keyword=level_keyword,
                        retry_count=retry_count,
                        previous_issues="；".join(last_issues),
                    )
                ),
            ]
        )
        text = r.summary + ("\n" + "\n".join(f"- {b}" for b in r.bullet_points) if r.bullet_points else "")
        # 确定性保障：LLM 文本缺等级关键词时，追加一句结构化事实复述（不编造内容）
        if level_keyword and level_keyword not in text:
            text += f"\n- 内部预警等级：{level_keyword}（{level}）。（该句为系统根据结构化字段补充的事实复述）"
        explanation = {
            "text": text,
            "summary": r.summary,
            "bullet_points": list(r.bullet_points or []),
            "cautions": list(r.cautions or []),
            "used_llm": True,
        }
    except Exception as e:
        print(f"[LLMExplanation] LLM 解释失败，模板兜底: {e}")
        cn = alert.get("hazard_type_cn", "")
        explanation = {
            "text": (
                f"【Mock 模拟预警说明】{cn}（{alert.get('hazard_type')}）当前内部预警等级为 "
                f"{level_keyword}（{level}，映射规则 {alert.get('mapping_version')}，Mock 测试规则）。"
                f"依据：Risk Agent 因子引擎结果 P={alert.get('based_on', {}).get('P')}、"
                f"T={alert.get('based_on', {}).get('T')}、R={alert.get('based_on', {}).get('R')}。"
                f"本说明为模板兜底生成（LLM 不可用），内容以结构化字段为准。"
            ),
            "summary": f"{cn} Mock 模拟预警（等级 {level_keyword}）",
            "bullet_points": [],
            "cautions": ["LLM 不可用，本段为模板兜底说明"],
            "used_llm": False,
        }

    print(f"[LLMExplanation] used_llm={explanation['used_llm']}")

    return {
        "explanation": explanation,
        "next": "warning_validator",
    }
