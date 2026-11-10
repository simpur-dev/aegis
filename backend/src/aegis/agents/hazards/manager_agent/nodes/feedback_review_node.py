"""
feedback_review_node：解读 Feedback Agent 结构化反馈，制定最小返工计划（全确定性）

【输入契约（以 Feedback Agent 真实交付物为准）】
  feedback_result.status ∈ no_issues / issues_found / needs_review /
                           insufficient_evidence / failed
  feedback_result.manager_actions[]：{agent（短名 data/risk/simulation/
                           warning/manager）, recommended_action,
                           evidence_high_confidence, ...}
  recommended_action 词表：rerun_suggested / rerun_investigation_suggested /
                           manual_review_first / pipeline_fix_suggested

【处置规则（任务书第十节 情况 A~E）】
  no_issues                    → 通过，收尾（不返工）；
  insufficient_evidence /
  needs_review / failed        → 不返工，转人工复核（上游结果保留）；
  issues_found：
    rerun_suggested + 高置信 + 目标为生产环节 → 最小返工：
        最早目标环节 + 其下游依赖闭包（planner.rework_closure），
        返工范围内交付物失效（清空结果/完成登记/该轮重试计数），
        feedback_loop_count+1，未超 MANAGER_MAX_FEEDBACK_LOOPS 才放行；
        达上限 → 停止返工、记错误历史、转人工复核；
    rerun_investigation_suggested / pipeline_fix_suggested /
    manual_review_first        → 不自动返工（"先调查/先人工"），转人工复核；
    阈值/权重/参数类建议（情况 E）→ Manager 永不自动执行参数调整，
        只写入 feedback_decisions 待审记录，最终报告呈现。

【红线】返工 = 重新完整调用相应 Agent，由其重新获取/计算；
  Manager 不修改、不修补任何交付物内容，也不替 Feedback 判断业务对错。
"""

import logging

from aegis.agents.hazards.manager_agent import planner
from aegis.agents.hazards.manager_agent.state import (
    AGENT_DATA,
    AGENT_FEEDBACK,
    AGENT_ORDER,
    AGENT_RESULT_KEY,
    AGENT_RISK,
    AGENT_SIMULATION,
    AGENT_WARNING,
    ERROR_FEEDBACK_REVIEW,
)

log = logging.getLogger("aegis.agents.hazards.manager_agent.nodes.feedback_review_node")

# Feedback 交付物短名 → Manager 标准 Agent 名
_SHORT_TO_AGENT = {
    "data": AGENT_DATA,
    "risk": AGENT_RISK,
    "simulation": AGENT_SIMULATION,
    "warning": AGENT_WARNING,
    "feedback": AGENT_FEEDBACK,
}
# 可成为返工目标的生产环节（返工 feedback 自身无意义，归人工复核处置）
_RERUNNABLE = {AGENT_DATA, AGENT_RISK, AGENT_SIMULATION, AGENT_WARNING}
# "先调查 / 先人工 / 编排层修复"类建议：一律不自动重跑，转人工
_REVIEW_ACTIONS = {"rerun_investigation_suggested", "pipeline_fix_suggested", "manual_review_first"}


def _max_loops() -> int:
    from aegis.agents.hazards.config import MANAGER_MAX_FEEDBACK_LOOPS

    return int(MANAGER_MAX_FEEDBACK_LOOPS)


def _normalize_agent(raw: str) -> str:
    """短名或标准名 → 标准 Agent 名；无法识别（如 manager）返回 ''。"""
    raw = str(raw or "")
    return _SHORT_TO_AGENT.get(raw, raw if raw in AGENT_ORDER else "")


def _decision(state: dict, action: str, reason: str, **extra) -> dict:
    """一条可追溯的返工决策记录（写入 feedback_decisions，报告呈现）。"""
    rec = {
        "loop": state.get("feedback_loop_count", 0) + 1,
        "feedback_status": (state.get("feedback_result") or {}).get("status", ""),
        "action": action,
        "reason": reason,
    }
    rec.update(extra)
    return rec


def feedback_review_node(state: dict) -> dict:
    fb = state.get("feedback_result") or {}
    status = str(fb.get("status", ""))
    loop = int(state.get("feedback_loop_count", 0))
    decisions = list(state.get("feedback_decisions") or [])
    plan = state.get("execution_plan") or []

    # ---------- 通过：无问题 ----------
    if status == "no_issues":
        decisions.append(_decision(state, "pass", "Feedback 复核未发现问题，正常收尾"))
        log.info("[Manager/FeedbackReview] no_issues → 通过，进入收尾")
        return {"rerun_plan": [], "feedback_decisions": decisions}

    # ---------- 证据不足 / 须人工 / 自身失败：不返工，转人工（上游保留）----------
    if status in ("insufficient_evidence", "needs_review", "failed") or not status:
        decisions.append(
            _decision(state, "human_review", f"Feedback status={status}：证据不足或复核未完成，不自动返工，已有结果保留并转人工复核")
        )
        log.info(f"[Manager/FeedbackReview] {status} → 不返工，转人工复核")
        return {"rerun_plan": [], "feedback_decisions": decisions, "human_review_required": True}

    # ---------- issues_found：按 manager_actions 决定返工 / 人工 ----------
    actions = fb.get("manager_actions") or []
    rerun_targets = sorted(
        {
            _normalize_agent(a.get("agent", ""))
            for a in actions
            if a.get("recommended_action") == "rerun_suggested" and a.get("evidence_high_confidence")
        }
        & _RERUNNABLE
    )
    review_only = [a for a in actions if a.get("recommended_action") in _REVIEW_ACTIONS]

    if not rerun_targets:
        reason = (
            "高置信重跑建议缺失（仅调查/人工/编排修复类建议或低置信），按第十节情况 D/E 不自动返工，转人工复核"
            if review_only
            else "issues_found 但无可识别的重跑目标，转人工复核"
        )
        decisions.append(_decision(state, "human_review", reason, review_actions=[a.get("recommended_action") for a in review_only]))
        log.info(f"[Manager/FeedbackReview] issues_found → 不返工：{reason}")
        return {"rerun_plan": [], "feedback_decisions": decisions, "human_review_required": True}

    # ---------- 达最大反馈循环上限：停止返工 ----------
    if loop >= _max_loops():
        msg = f"Feedback 建议返工 {rerun_targets}，但反馈循环已达上限（{loop}/{_max_loops()}），停止自动返工并转人工复核"
        decisions.append(_decision(state, "loop_limit_stop", msg, rerun_targets=rerun_targets))
        log.info(f"[Manager/FeedbackReview] {msg}")
        return {
            "rerun_plan": [],
            "feedback_decisions": decisions,
            "human_review_required": True,
            "error_history": [
                *list(state.get("error_history") or []),
                {"agent": AGENT_FEEDBACK, "error_type": ERROR_FEEDBACK_REVIEW, "message": msg, "fatal": False, "attempt": loop},
            ],
        }

    # ---------- 最小返工：最早目标 + 依赖闭包，返工范围内结果失效 ----------
    earliest = min(rerun_targets, key=AGENT_ORDER.index)
    rerun = planner.rework_closure(earliest, plan)
    completed = [a for a in (state.get("completed_agents") or []) if a not in rerun]
    agent_status = {k: v for k, v in (state.get("agent_status") or {}).items() if k not in rerun}
    retry_counts = dict(state.get("retry_counts") or {})
    retry_counts.update(dict.fromkeys(rerun, 0))  # 返工范围外环节的重试记录保留
    updates: dict = {
        "rerun_plan": rerun,
        "feedback_decisions": [
            *decisions,
            _decision(
                state,
                "rerun",
                f"高置信重跑建议：目标 {rerun_targets}，最早环节 {earliest}，最小返工计划 {rerun}（第 {loop + 1}/{_max_loops()} 轮）",
                rerun_targets=rerun_targets,
                rerun_plan=rerun,
            ),
        ],
        "feedback_loop_count": loop + 1,
        "pending_agents": rerun,
        "completed_agents": completed,
        "agent_status": agent_status,
        "retry_counts": retry_counts,
    }
    # 失效管理：返工范围内的旧交付物清空（重新执行后由 dispatch 重新落地），
    # 不在返工范围的结果原样保留 —— 不整体重跑，也不静默沿用旧结果
    for a in rerun:
        updates[AGENT_RESULT_KEY[a]] = {}
    log.info(f"[Manager/FeedbackReview] issues_found → 最小返工 {rerun}（第 {loop + 1}/{_max_loops()} 轮，目标 {rerun_targets}）")
    return updates
