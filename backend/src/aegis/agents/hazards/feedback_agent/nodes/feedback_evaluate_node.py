"""
FeedbackEvaluate 节点（问题分类与影响评估）

【本文件角色】
  对 FeedbackAnalyze 产出的确定性 issue 做分类汇总与标注（纯 Python，无 LLM）：
    - 按八类问题类型 / 所属 Agent / 严重程度 / 置信度归类；
    - 生成反馈条目 feedback_items（带 feedback_id，供反思/建议/记忆环节引用）；
    - 生成评估汇总 evaluation_result（路由核心字段）。

【路由语义】evaluation_result.recommendation：
    "reflect"     存在 minor 及以上问题且证据链基本齐备 → 进入 Reflection 归因分析；
    "pass"        无问题或仅有 info 级提示 → 直接输出；
    "insufficient"关键上游结果缺失（insufficient_evidence/critical 灾种拒绝）→ 直接输出；
    "output"      灾种无效等前提性失败 → 直接输出（不做反思）。

【红线】不改动 issue 的原始证据；评估只做归类，不做专业判断。

【对外契约】
  读取：detected_issues / collected_context
  写入：feedback_items / evaluation_result / historical_feedback / next
"""

import logging
from typing import Any

from aegis.agents.hazards.feedback_agent.state import FeedbackAgentState
from aegis.agents.hazards.feedback_agent.tools.feedback_check_tool import (
    CATEGORY_INSUFFICIENT_EVIDENCE,
    CATEGORY_WORKFLOW_ERROR,
)
from aegis.agents.hazards.feedback_agent.tools.feedback_memory_tool import (
    load_feedback_records,
)

log = logging.getLogger("aegis.agents.hazards.feedback_agent.nodes.feedback_evaluate_node")

_SEVERITY_ORDER = {"info": 0, "minor": 1, "major": 2, "critical": 3}


def feedback_evaluate_node(state: FeedbackAgentState) -> dict[str, Any]:
    """反馈评估节点：问题归类 + 路由判定（确定性）。"""
    issues = state.get("detected_issues", []) or []
    hazard_key = (state.get("collected_context", {}) or {}).get("hazard_type_normalized", "")

    # ---------- 1) 生成反馈条目（附加 feedback_id 与可信度标注） ----------
    feedback_items = []
    for i, issue in enumerate(issues):
        item = dict(issue)  # 浅拷贝注：不回写 issue 原始对象
        item["feedback_id"] = f"FBI-{i + 1:03d}"
        item["needs_review"] = bool(issue.get("requires_human_review")) or float(issue.get("confidence", 0)) < 0.7
        feedback_items.append(item)

    # ---------- 2) 分类/严重度汇总 ----------
    category_summary: dict[str, int] = {}
    agent_summary: dict[str, int] = {}
    severity_summary: dict[str, int] = {}
    for item in feedback_items:
        category_summary[item["category"]] = category_summary.get(item["category"], 0) + 1
        agent_summary[item["agent"]] = agent_summary.get(item["agent"], 0) + 1
        severity_summary[item["severity"]] = severity_summary.get(item["severity"], 0) + 1

    actionable = [i for i in feedback_items if _SEVERITY_ORDER.get(i["severity"], 0) >= 1]  # minor 及以上
    confident = [i for i in actionable if float(i["confidence"]) >= 0.7]
    max_severity = max((i["severity"] for i in feedback_items), key=lambda s: _SEVERITY_ORDER.get(s, 0), default="")

    # ---------- 3) 前提性失败判定（灾种无效 / 证据整体不足） ----------
    hazard_rejected = any(
        i["category"] == CATEGORY_WORKFLOW_ERROR and i["severity"] == "critical" and "hazard_type" in i["fields"] and not hazard_key
        for i in feedback_items
    )
    insufficient = any(i["category"] == CATEGORY_INSUFFICIENT_EVIDENCE and i["severity"] in ("major", "critical") for i in feedback_items)

    # ---------- 4) 路由建议 ----------
    if hazard_rejected:
        recommendation = "output"  # 灾种都不合法，反思无意义
    elif not actionable:
        recommendation = "pass"  # 无问题 / 仅 info 提示
    elif all(i["category"] == CATEGORY_INSUFFICIENT_EVIDENCE for i in actionable):
        recommendation = "insufficient"  # 只缺上游证据 → 不反思，直接输出
    elif not confident:
        recommendation = "insufficient"  # 全部低置信 → 不下结论，直接输出待复核清单
    else:
        recommendation = "reflect"  # 有高置信可反思问题

    evaluation_result = {
        "has_issues": bool(feedback_items),
        "actionable_issue_count": len(actionable),
        "high_confidence_issue_count": len(confident),
        "human_review_count": sum(1 for i in feedback_items if i["needs_review"]),
        "category_summary": category_summary,
        "agent_summary": agent_summary,
        "severity_summary": severity_summary,
        "max_severity": max_severity,
        "hazard_rejected": hazard_rejected,
        "insufficient_evidence": recommendation == "insufficient" or (insufficient and recommendation != "reflect"),
        "recommendation": recommendation,
        "note": "评估仅基于确定性证据归类；本节点不修改任何上游结果，是否重跑上游 Agent 由 Manager 决定",
    }

    # ---------- 5) 检索同灾种历史反馈经验（只读参考） ----------
    historical = load_feedback_records(hazard_type=hazard_key, limit=5) if hazard_key else []

    log.info(
        f"[FeedbackEvaluate] 问题数={len(feedback_items)} "
        f"可行动={len(actionable)} 高置信={len(confident)} "
        f"路由建议={recommendation} 历史经验={len(historical)} 条"
    )

    return {
        "feedback_items": feedback_items,
        "evaluation_result": evaluation_result,
        "historical_feedback": historical,
        "next": "conditional_router",
    }
