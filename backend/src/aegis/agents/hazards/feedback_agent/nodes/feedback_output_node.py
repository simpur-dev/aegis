"""
FeedbackOutput 节点（统一结构化反馈输出，交给 Manager）

【本文件角色】
  组装 feedback_result：问题清单 + 分类评估 + 反思 + 建议 + 记忆存档结果 +
  给 Manager 的行动建议 + 安全声明。
  【不直接修改 Risk / Simulation / Warning 的任何原始输出】——本节点只读
  State 中 Feedback 自己产生的字段并组装。

【status 语义】（给 Manager 判断流程）
  no_issues              未发现可行动问题；
  issues_found           发现问题并完成反馈（含反思与建议）；
  needs_review           发现问题但证据不足/全部低置信，须人工复核后再定行动；
  insufficient_evidence  关键上游结果缺失，反馈链路无法完整核验；
  failed                 前提性失败（灾种不在五类标准键内被拒绝 / 内部异常）。

【对外契约】
  写入：feedback_result / status / next
"""

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from aegis.agents.hazards.feedback_agent.state import FeedbackAgentState

log = logging.getLogger("aegis.agents.hazards.feedback_agent.nodes.feedback_output_node")


def _manager_actions(evaluation: dict, items: list, suggestions: list) -> list:
    """按问题归属聚合给 Manager 的行动建议（决定权在 Manager，Feedback 只建议）。"""
    by_agent: dict[str, list] = {}
    for item in items:
        if item.get("severity") not in ("minor", "major", "critical"):
            continue
        agent = item.get("agent", "manager")
        by_agent.setdefault(agent, []).append(item)

    actions = []
    for agent, agent_items in sorted(by_agent.items()):
        worst = max(agent_items, key=lambda i: {"info": 0, "minor": 1, "major": 2, "critical": 3}.get(i.get("severity"), 0))
        has_confident = any(float(i.get("confidence", 0)) >= 0.7 for i in agent_items)
        if agent == "manager":
            action = "pipeline_fix_suggested"  # 编排层问题（传参/级联/版本同步）
        elif worst.get("severity") == "critical" and has_confident:
            action = "rerun_suggested"  # 高置信严重问题 → 建议重跑该 Agent
        elif has_confident:
            action = "rerun_investigation_suggested"
        else:
            action = "manual_review_first"  # 证据不足 → 先人工复核，不建议直接重跑
        actions.append(
            {
                "agent": agent,
                "recommended_action": action,
                "issue_count": len(agent_items),
                "max_severity": worst.get("severity", ""),
                "evidence_high_confidence": has_confident,
                "related_feedback_ids": [i.get("feedback_id") for i in agent_items],
                "note": "仅建议：是否重新执行该 Agent 由 Manager 决定，Feedback 无权也未修改任何上游结果",
            }
        )
    return actions


def feedback_output_node(state: FeedbackAgentState) -> dict[str, Any]:
    """反馈输出节点：组装交给 Manager 的统一结构化反馈。"""
    context = state.get("collected_context", {}) or {}
    evaluation = state.get("evaluation_result", {}) or {}
    items = state.get("feedback_items", []) or []
    suggestions = state.get("improvement_suggestions", []) or []
    error_message = state.get("error_message", "")

    actionable = [i for i in items if i.get("severity") != "info"]

    # ---------- status 判定 ----------
    if evaluation.get("hazard_rejected") or (not context.get("hazard_type_normalized") and items):
        status = "failed"  # 灾种被拒绝 / 前提性失败
    elif not actionable:
        status = "no_issues"
    elif evaluation.get("recommendation") == "insufficient":
        # 评估建议不反思：全部是"缺上游证据"或全部低置信 → 直接如实标注
        only_missing_evidence = all(i.get("category") == "insufficient_evidence" for i in actionable)
        status = "insufficient_evidence" if only_missing_evidence else "needs_review"
    elif evaluation.get("high_confidence_issue_count", 0) == 0:
        # 全部低置信：纯缺上游证据 → insufficient_evidence；
        # 存在其他类别低置信问题 → needs_review（须人工复核，不下结论）
        only_missing_evidence = all(i.get("category") == "insufficient_evidence" for i in actionable)
        status = "insufficient_evidence" if only_missing_evidence else "needs_review"
    else:
        status = "issues_found"
    if error_message and status == "no_issues":
        status = "failed"

    feedback_result = {
        # ---- 标识 ----
        "feedback_id": f"FB-{uuid.uuid4().hex[:10]}",
        "event_id": state.get("event_id", ""),
        "hazard_type": context.get("hazard_type_normalized", "") or state.get("hazard_type", ""),
        "generated_at": datetime.now(UTC).isoformat(),
        # ---- 结论 ----
        "status": status,
        "summary": {
            "issue_count": len(items),
            "actionable_issue_count": len(actionable),
            "category_summary": evaluation.get("category_summary", {}),
            "agent_summary": evaluation.get("agent_summary", {}),
            "severity_summary": evaluation.get("severity_summary", {}),
            "max_severity": evaluation.get("max_severity", ""),
            "needs_human_review_count": evaluation.get("human_review_count", 0),
        },
        # ---- 明细 ----
        "feedback_items": items,
        "evaluation_result": evaluation,
        "analysis_notes": state.get("analysis_notes", {}) or {},
        "reflection_result": state.get("reflection_result", {}) or {},
        "improvement_suggestions": suggestions,
        "historical_feedback_reference": (state.get("historical_feedback", []) or [])[:3],
        "memory": state.get("memory_result", {}) or {},
        # ---- 给 Manager 的行动建议（决定权在 Manager）----
        "manager_actions": _manager_actions(evaluation, items, suggestions),
        # ---- 外部反馈（透传存档结果，不做真伪判断）----
        "external_feedback_received": bool(state.get("external_feedback")),
        # ---- 安全声明（自证未越权）----
        "safety_declarations": {
            "modified_upstream_results": False,  # 未修改任何上游 Agent 输出
            "risk_scores_changed": False,  # 未修改 Risk 正式风险分数
            "warning_levels_changed": False,  # 未调整预警等级
            "thresholds_or_weights_changed": False,  # 未修改五类灾害权重/阈值
            "mock_never_treated_as_real": True,  # Mock 结果均按模拟数据核验
            "llm_reflection_not_professional_validation": True,  # 反思不构成专业验证
        },
        # ---- 运行元信息 ----
        "metadata": {
            "iterations_used": state.get("iteration_count", 0),
            "max_iterations": state.get("max_iterations", 2),
            "upstream_versions": {
                "data": {
                    "status": (context.get("agents", {}).get("data", {}) or {}).get("status"),
                    "is_mock": (context.get("agents", {}).get("data", {}) or {}).get("is_mock"),
                },
                "risk": {
                    "rule_version": (context.get("agents", {}).get("risk", {}) or {}).get("rule_version"),
                    "engine_version": (context.get("agents", {}).get("risk", {}) or {}).get("engine_version"),
                },
                "simulation": {"model_version": (context.get("agents", {}).get("simulation", {}) or {}).get("model_version")},
                "warning": {"mapping_version": (context.get("agents", {}).get("warning", {}) or {}).get("mapping_version")},
            },
            "error_message": error_message,
            "note": "Feedback 输出为质量检查与经验追溯结果；所有问题判定基于确定性"
            "证据，低置信条目已标记 needs_review，须人工审核后方可用于模型改进",
        },
    }

    log.info(
        f"[FeedbackOutput] status={status} 问题数={len(items)} "
        f"可行动={len(actionable)} 建议数={len(suggestions)} "
        f"反思轮次={state.get('iteration_count', 0)}"
    )

    return {
        "feedback_result": feedback_result,  # >>> 最终交付物，给 Manager
        "status": status,  # >>> 给 Manager 判断流程
        "next": "end",
    }
