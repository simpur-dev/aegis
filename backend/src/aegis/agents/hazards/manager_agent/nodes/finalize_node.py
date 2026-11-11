"""
finalize_node：汇总已有结果，输出最终任务报告（全确定性；报告润色 LLM 为可选增强）

【task_status 判定（第十二节受控词表）】
  已在前置节点定终态（need_clarification / unsupported_hazard / insufficient_data）
  → 原样保留；
  计划全部完成且无失败无跳过 → completed；
  有完成也有失败/跳过 → partially_completed；
  无任何完成 → failed；
  人工复核标记 → needs_human_review（覆盖前两类"完成"态，问题仍如实呈现）。

【诚实要求】Mock 标识原样透传并显式声明；region/analysis_time 未按参数过滤
  的能力限制必须写入报告；报告只汇总，不新增任何专业结论。
"""

import logging
from datetime import UTC, datetime

from aegis.agents.hazards.manager_agent.state import (
    AGENT_CN_NAME,
    AGENT_ORDER,
    TASK_COMPLETED,
    TASK_FAILED,
    TASK_INSUFFICIENT_DATA,
    TASK_NEED_CLARIFICATION,
    TASK_NEEDS_HUMAN_REVIEW,
    TASK_PARTIALLY_COMPLETED,
    TASK_UNSUPPORTED_HAZARD,
)

log = logging.getLogger("aegis.agents.hazards.manager_agent.nodes.finalize_node")

_TERMINAL_SET = {TASK_NEED_CLARIFICATION, TASK_UNSUPPORTED_HAZARD, TASK_INSUFFICIENT_DATA}


def _max_feedback_loops() -> int:
    from aegis.agents.hazards.config import MANAGER_MAX_FEEDBACK_LOOPS

    return int(MANAGER_MAX_FEEDBACK_LOOPS)


def _decide_task_status(state: dict) -> str:
    prev = state.get("task_status", "")
    if prev in _TERMINAL_SET:  # 前置节点的终态优先保留
        return prev
    plan = state.get("execution_plan") or []
    completed = state.get("completed_agents") or []
    failed = state.get("failed_agents") or []
    skipped = state.get("skipped_agents") or []
    if plan and all(a in completed for a in plan):
        status = TASK_COMPLETED if not (failed or skipped) else TASK_PARTIALLY_COMPLETED
    elif completed:
        status = TASK_PARTIALLY_COMPLETED
    else:
        status = TASK_FAILED
    if status in (TASK_COMPLETED, TASK_PARTIALLY_COMPLETED) and state.get("human_review_required"):
        status = TASK_NEEDS_HUMAN_REVIEW
    return status


def _mock_disclosure(state: dict) -> dict:
    """Mock / Real 标识透传核验结果（原样引用各交付物自报字段，不加戏）。"""
    data = state.get("data_result") or {}
    proc = data.get("processed_data") or {}
    risk = state.get("risk_result") or {}
    sim = state.get("simulation_result") or {}
    warn = state.get("warning_result") or {}
    return {
        "data_is_mock": proc.get("is_mock"),
        "data_source": proc.get("data_source"),
        "risk_is_mock": (risk.get("model_info") or {}).get("is_mock"),
        "risk_rule_version": (risk.get("model_info") or {}).get("rule_version"),
        "simulation_is_real_prediction": (sim.get("model_info") or {}).get("is_real_prediction"),
        "warning_mock_only": (warn.get("alert") or {}).get("mock_only"),
        "note": "以上标识均透传自各专业 Agent 交付物原值；Mock 结果不是真实地质灾害预测，禁止伪装为真实。",
    }


def finalize_node(state: dict) -> dict:
    task_status = _decide_task_status(state)
    retry_counts = state.get("retry_counts") or {}

    agent_status = {}
    for a in AGENT_ORDER:
        agent_status[a] = (state.get("agent_status") or {}).get(a, "not_run")

    report = {
        "event_id": state.get("event_id", ""),
        "hazard_type": state.get("hazard_type", ""),
        "task_type": state.get("task_type", ""),
        "region": state.get("region", ""),
        "analysis_time": state.get("analysis_time", ""),
        "task_status": task_status,
        "finished_at": datetime.now(UTC).isoformat(),
        "agent_status": agent_status,
        # "结果可用"按是否成功完成判定（失败/跳过的原始交付物仍在顶层键中，
        # 但状态为 failed —— 不把失败或跳过的环节呈现为已获得结果）
        "results_present": {a: a in (state.get("completed_agents") or []) for a in AGENT_ORDER},
        "execution_summary": {
            "execution_plan": state.get("execution_plan", []),
            "completed_agents": state.get("completed_agents", []),
            "failed_agents": state.get("failed_agents", []),
            "skipped_agents": state.get("skipped_agents", []),
            "retry_counts": retry_counts,
            "total_retries": sum(retry_counts.values()),
            "feedback_loop_count": state.get("feedback_loop_count", 0),
            "agent_call_log": state.get("agent_call_log", []),
        },
        "error_history": state.get("error_history", []),
        "human_review_required": bool(state.get("human_review_required")),
        # Feedback 返工闭环呈现（第十节）：每轮决策可追溯，含未执行的待人工建议
        "feedback_review": {
            "feedback_loop_count": state.get("feedback_loop_count", 0),
            "max_feedback_loops": _max_feedback_loops(),
            "rerun_plan": state.get("rerun_plan", []),
            "decisions": state.get("feedback_decisions", []),
            "note": "阈值/权重/参数类建议不会被自动执行，仅作为待人工审核记录呈现；返工=重新完整调用相应 Agent，不修改任何历史结论。",
        },
        "mock_disclosure": _mock_disclosure(state),
        "capability_limits": [
            "region/analysis_time 仅记入任务上下文：Data Agent 当前不按这两个参数过滤数据，该能力尚未实现。",
            "五灾种规则/模型均为 MOCK-v0.1 玩具实现，流程通过不代表地质灾害预测能力已经过专业验证。",
        ],
        "summary_text": _summary_text(state, task_status, agent_status),
    }

    # 可选 LLM 润色（llm_polish=True 时启用；失败自动走确定性兜底，
    # 只生成叙述文字，不改变任何结构化事实）
    if state.get("llm_polish"):
        report["llm_summary"], report["llm_summary_used_llm"] = _llm_polish(report)

    log.info(
        f"[Manager/Finalize] task_status={task_status} | "
        f"完成={report['execution_summary']['completed_agents']} | "
        f"失败={report['execution_summary']['failed_agents']} | "
        f"跳过={report['execution_summary']['skipped_agents']}"
    )
    return {"task_status": task_status, "final_report": report}


#: 最终报告里要复述给使用者看的结构化事实（其余字段不进提示词）。
_REPORT_OVERVIEW_KEYS = (
    "event_id",
    "hazard_type",
    "task_type",
    "task_status",
    "agent_status",
    "execution_summary",
    "mock_disclosure",
    "capability_limits",
    "human_review_required",
)


def _llm_polish(report: dict) -> tuple:
    """LLM 报告润色（可选增强）：失败/不可用时返回确定性摘要兜底。"""
    fallback = (report.get("summary_text", ""), False)
    try:
        from aegis.agents.core.meta import llm_call_meta
        from aegis.agents.core.prompts import build_prompt
        from aegis.agents.hazards.manager_agent.llm_setup import get_manager_llm

        llm = get_manager_llm()
        # 提示词正文与版本号来自唯一注册表；这一腿是**纯文本**润色（非结构化输出），
        # 因此 meta 不标 structured。
        messages, prompt_version = build_prompt(
            "hazard.manager_report",
            overview=str({k: report.get(k) for k in _REPORT_OVERVIEW_KEYS}),
        )
        with llm_call_meta(task="hazard.manager_report", prompt_version=prompt_version):
            resp = llm.invoke(messages)
        text = (getattr(resp, "content", "") or "").strip()
        return (text or fallback[0], bool(text))
    except Exception as e:
        log.info(f"[Manager/Finalize] LLM 润色不可用（{type(e).__name__}），使用确定性摘要")
        return fallback


def _summary_text(state: dict, task_status: str, agent_status: dict) -> str:
    """确定性摘要文本（不引入任何新结论，只复述结构化事实）。"""
    cn = {a: AGENT_CN_NAME[a] for a in AGENT_ORDER}
    lines = [f"任务 {state.get('event_id', '')}（灾种 {state.get('hazard_type', '')}）最终状态：{task_status}。"]
    for a in AGENT_ORDER:
        if a in (state.get("execution_plan") or []):
            lines.append(f"  - {cn[a]}（{a}）：{agent_status[a]}")
    if state.get("failed_agents"):
        lines.append(f"  失败环节：{'、'.join(state['failed_agents'])}")
    if state.get("skipped_agents"):
        lines.append(f"  跳过环节：{'、'.join(state['skipped_agents'])}（最终结果未获得该环节输出）")
    loops = state.get("feedback_loop_count", 0)
    if loops:
        lines.append(f"  Feedback 复核触发最小返工 {loops} 轮（返工计划：{'、'.join(state.get('rerun_plan') or []) or '（末轮已收尾）'}）")
    if state.get("human_review_required"):
        lines.append("  需要人工复核。")
    return "\n".join(lines)
