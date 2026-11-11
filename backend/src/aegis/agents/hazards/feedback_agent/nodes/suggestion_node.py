"""
Suggestion 节点（改进建议生成）

【本文件角色】
  为每个可行动的反馈条目生成【具体到 Agent/组件】的改进建议：
    target_agent / target_component / problem_description / suggested_action /
    supporting_evidence / expected_effect / requires_human_review
  LLM 通道按 FEEDBACK_SUGGESTION_PROMPT 生成并做字段合法性收口；
  确定性兜底用"类别 → 注册表组件"模板映射，同样具体到工具名，不输出空泛建议。

【红线】建议不改变任何上游结果；涉及专业规则变更必须 requires_human_review=True。

【对外契约】
  读取：feedback_items / reflection_result / collected_context / test_hooks
  写入：improvement_suggestions / needs_another_round / next
"""

import json
import logging
from typing import Any

from aegis.agents.core.meta import llm_call_meta
from aegis.agents.core.prompts import build_prompt
from aegis.agents.hazards.feedback_agent.state import FeedbackAgentState
from aegis.agents.hazards.hazard_registry import normalize_hazard_type

log = logging.getLogger("aegis.agents.hazards.feedback_agent.nodes.suggestion_node")

VALID_TARGET_AGENTS = {"data", "risk", "simulation", "warning", "manager"}

# 类别 → 建议动作模板（确定性兜底；措辞具体到环节动作，避免"优化模型"式空泛表述）
_ACTION_TEMPLATES = {
    "data_quality_issue": "核对 Data Agent 数据获取与清洗环节：确认该字段在数据源声明、"
    "清洗/标准化与质量校验三处口径一致；如为真实数据源接入，检查观测"
    "时效与缺测处理策略",
    "risk_calculation_issue": "核对 Risk Agent 因子引擎入参：确认所评灾种的必需因子在factor_scores 中齐备、证据链组装未被跳过",
    "simulation_issue": "核对 Simulation Agent 情景设计与执行：确认情景参数覆盖模型必需输入，且路由判定(inputs_ok)与最终状态一致",
    "warning_consistency_issue": "核对 Warning Agent 预警对象组装：based_on 依据链须引用最新"
    "Risk/Simulation 交付物字段值，解释文本声明与 mock_only 标记一致",
    "hazard_type_mismatch": "核对 Manager 串联各 Agent 时的 hazard_type 传参：五类标准键显式传入，任一 Agent 归一化失败应中断流程而非继续",
    "model_version_issue": "核对注册表与 Agent 结果中的版本字段：重新运行相应 Agent 以使用当前注册表声明的模型/规则版本，或更新缓存结果",
    "insufficient_evidence": "补齐反馈输入：由 Manager 将缺失的上游 Agent 交付物一并传入 Feedback Agent 后重新执行反馈检查",
    "workflow_error": "核对管线状态级联约定：上游 failed/insufficient 时应阻断或显式降级，而不是由下游继续产出成功结果",
}


def _resolve_component(hazard_key: str, agent: str, item: dict) -> str:
    """把建议定位到具体组件（从注册表取真实工具/类名，取不到则回退到模块名）。"""
    try:
        if agent == "data" and hazard_key:
            from aegis.agents.hazards.data_agent.tools.hazard_data_registry import get_data_source_tool

            return f"agents/data_agent/tools/（{get_data_source_tool(hazard_key).__name__} + data_validate_tool）"
        if agent == "risk" and hazard_key:
            from aegis.agents.hazards.risk_agent.tools.hazard_risk_registry import get_hazard_risk_profile

            tool = get_hazard_risk_profile(hazard_key).get("assessment_tool")
            return f"agents/risk_agent/tools/（{getattr(tool, '__name__', 'factor_engine')}）"
        if agent == "simulation" and hazard_key:
            from aegis.agents.hazards.simulation_agent.tools.hazard_model_router import get_model

            return f"agents/simulation_agent/models/（{type(get_model(hazard_key)).__name__} + scenario_validate_tool）"
        if agent == "warning":
            return "agents/warning_agent/nodes/（hazard_warning_rule_node + alert_builder_node）"
    except Exception:
        pass
    return {
        "data": "agents/data_agent",
        "risk": "agents/risk_agent",
        "simulation": "agents/simulation_agent",
        "warning": "agents/warning_agent",
        "manager": "workflow（Manager 编排层）",
    }.get(agent, agent)


def _template_suggestions(items: list, hazard_key: str) -> list:
    """确定性模板建议：每个可行动条目一条，组件定位到注册表真实工具名。"""
    out = []
    for item in items:
        agent = item.get("agent", "manager")
        category = item.get("category", "")
        out.append(
            {
                "feedback_id": item.get("feedback_id", ""),
                "issue_id": item.get("issue_id", ""),
                "target_agent": agent if agent in VALID_TARGET_AGENTS else "manager",
                "target_component": _resolve_component(hazard_key, agent, item),
                "problem_description": item.get("description", ""),
                "suggested_action": _ACTION_TEMPLATES.get(category, "交由 Manager 结合证据清单核查对应环节"),
                "supporting_evidence": item.get("evidence", []),
                "expected_effect": "消除该确定性检查项报告的不一致/缺失，使管线交付物重新满足系统约定",
                "requires_human_review": bool(item.get("requires_human_review")) or float(item.get("confidence", 0)) < 0.7,
                "source": "deterministic_template",
            }
        )
    return out


def suggestion_node(state: FeedbackAgentState) -> dict[str, Any]:
    """建议节点（LLM 优先 + 确定性模板兜底；输出结构收口为统一 Schema）。"""
    hazard_key = normalize_hazard_type(state.get("hazard_type", ""))
    items = [i for i in (state.get("feedback_items", []) or []) if i.get("severity") in ("minor", "major", "critical")]

    template = _template_suggestions(items, hazard_key)
    suggestions, used_llm = template, False

    if items:
        try:
            from pydantic import BaseModel, Field

            from aegis.agents.hazards.feedback_agent.llm_setup import get_feedback_llm

            class SuggestionOut(BaseModel):
                suggestions: list = Field(
                    description="建议列表，每项为对象："
                    "{feedback_id, target_agent, target_component, "
                    "problem_description, suggested_action, "
                    "supporting_evidence, expected_effect, "
                    "requires_human_review}"
                )

            llm = get_feedback_llm()
            structured = llm.with_structured_output(SuggestionOut)
            messages, prompt_version = build_prompt(
                "hazard.feedback_suggestion",
                reflection_json=json.dumps(state.get("reflection_result", {}), ensure_ascii=False),
                items_json=json.dumps(items, ensure_ascii=False),
            )
            with llm_call_meta(task="hazard.feedback_suggestion", prompt_version=prompt_version, structured=True):
                r = structured.invoke(messages)
            by_id = {s.get("feedback_id"): s for s in (r.suggestions or []) if isinstance(s, dict)}
            merged = []
            for t in template:
                s = dict(t)
                llm_s = by_id.get(t["feedback_id"], {})
                # 收口规则：LLM 只能改写文本字段；target_agent 必须合法，
                # requires_human_review 只允许从模板升级到 True，不允许降为 False
                for field in ("problem_description", "suggested_action", "expected_effect", "target_component"):
                    v = llm_s.get(field)
                    if isinstance(v, str) and v.strip():
                        s[field] = v.strip()
                if llm_s.get("target_agent") in VALID_TARGET_AGENTS:
                    s["target_agent"] = llm_s["target_agent"]
                ev = llm_s.get("supporting_evidence")
                if isinstance(ev, list) and ev:
                    s["supporting_evidence"] = list(dict.fromkeys([str(x) for x in ev] + s["supporting_evidence"]))
                s["requires_human_review"] = bool(s["requires_human_review"]) or bool(llm_s.get("requires_human_review"))
                s["source"] = "llm+guardrail" if llm_s else "deterministic_template"
                merged.append(s)
            suggestions, used_llm = merged, True
        except Exception as e:
            log.info(f"[Suggestion] LLM 不可用，使用确定性模板建议: {e}")

    # 反思回环请求：正常流程不请求额外轮次；测试钩子可强制请求，
    # 以验证 graph 的有界回环（受 max_iterations 限界）
    hooks = state.get("test_hooks", {}) or {}
    iteration = state.get("iteration_count", 0)
    max_iter = state.get("max_iterations", 2)
    needs_another_round = bool(hooks.get("force_needs_another_round")) and iteration < max_iter

    log.info(f"[Suggestion] 建议数={len(suggestions)} used_llm={used_llm} needs_another_round={needs_another_round}")

    return {
        "improvement_suggestions": suggestions,
        "needs_another_round": needs_another_round,
        "suggestion_used_llm": used_llm,
        "next": "memory_store",
    }
