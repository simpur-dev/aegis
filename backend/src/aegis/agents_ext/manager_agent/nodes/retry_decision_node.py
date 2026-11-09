"""
retry_decision_node：异常分类与有限重试决策（全确定性处置矩阵）

【处置矩阵（第九节）】
  可重试错误（执行异常/超时/结构非法/数据缺失/过期/模拟失败/预警校验失败）：
    未达该 Agent 的 RETRY_CONFIG 上限 → retry_current_agent
    达上限 → 记 MAX_RETRIES_EXCEEDED，按 Agent 特判：
      · Simulation 且 SIMULATION_OPTIONAL 且未强制 → skip_optional_agent
        （任务终态 partial，报告明示未获得模拟结果）
      · Data 的数据缺失/过期 → stop_with_insufficient_data（终态受控）
      · 其他 → stop_with_error
  致命错误（灾种串线 / Mock 冒充真实）：不重试（重试不改变错误性质），
    stop_with_error + 转人工复核。
  Feedback 自身失败：不推翻已有上游结果，stop + require_human_review。

【红线】重试 = 重新完整调用该 Agent（由其内部机制重新获取/计算），
  Manager 不伪造/修补交付物；每次决策都保留错误记录，不静默覆盖。
"""

from aegis.agents_ext.manager_agent.state import (
    AGENT_DATA,
    AGENT_FEEDBACK,
    AGENT_SIMULATION,
    ERROR_MAX_RETRIES,
    ERROR_MISSING_DATA,
    ERROR_SIMULATION_FAILURE,
    TASK_INSUFFICIENT_DATA,
)

# 允许重试的错误类型（重新调用有可能得到不同结果）
RETRYABLE_ERRORS = {
    "AGENT_EXECUTION_ERROR",
    "AGENT_TIMEOUT",
    "INVALID_AGENT_OUTPUT",
    ERROR_MISSING_DATA,
    "STALE_DATA",
    ERROR_SIMULATION_FAILURE,
    "WARNING_VALIDATION_FAILURE",
}


def _retry_limit(agent: str) -> int:
    from aegis.agents_ext.config import MANAGER_RETRY_CONFIG

    return int(MANAGER_RETRY_CONFIG.get(agent, 0))


def _sim_optional(state: dict) -> bool:
    from aegis.agents_ext.config import MANAGER_SIMULATION_OPTIONAL

    if state.get("require_simulation"):  # 用户明确要求必须完成模拟
        return False
    return bool(MANAGER_SIMULATION_OPTIONAL)


def retry_decision_node(state: dict) -> dict:
    check = state.get("check_result") or {}
    agent = check.get("agent", state.get("current_agent", ""))
    error_type = check.get("error_type", "")
    fatal = bool(check.get("fatal"))

    retry_counts = dict(state.get("retry_counts") or {})
    used = retry_counts.get(agent, 0)
    limit = _retry_limit(agent)
    updates: dict = {
        "retry_counts": retry_counts,
        "error_history": list(state.get("error_history") or []),
        "pending_agents": list(state.get("pending_agents") or []),
        "skipped_agents": list(state.get("skipped_agents") or []),
        "failed_agents": list(state.get("failed_agents") or []),
        "agent_status": dict(state.get("agent_status") or {}),
    }

    # ---------- 分支 1：致命错误（灾种串线 / Mock 冒充）→ 不重试 ----------
    if fatal:
        action = "stop_with_error"
        reason = f"致命错误（{error_type}）不重试，停止并转人工复核"
        updates["human_review_required"] = True

    # ---------- 分支 2：可重试且未达上限 ----------
    elif error_type in RETRYABLE_ERRORS and used < limit:
        action = "retry_current_agent"
        retry_counts[agent] = used + 1
        reason = f"{agent} 发生 {error_type}，重新调用（第 {used + 1}/{limit} 次重试）"
        # Agent 保持在队列头部，dispatch 会再次执行它

    # ---------- 分支 3：达上限 / 不可重试 → 终局处置 ----------
    else:
        if error_type in RETRYABLE_ERRORS:
            updates["error_history"].append(
                {
                    "agent": agent,
                    "error_type": ERROR_MAX_RETRIES,
                    "message": f"{agent} 重试 {used} 次仍失败（上限 {limit}）",
                    "fatal": False,
                    "attempt": used + 1,
                }
            )
        reason_tail = f"重试上限已达（{used}/{limit}）" if error_type in RETRYABLE_ERRORS else f"错误类型 {error_type} 不属于可重试范围"
        if agent == AGENT_SIMULATION and _sim_optional(state) and error_type == ERROR_SIMULATION_FAILURE:
            action = "skip_optional_agent"
            reason = f"模拟失败且按配置可选（SIMULATION_OPTIONAL），跳过并继续下游：{reason_tail}"
            if updates["pending_agents"] and updates["pending_agents"][0] == agent:
                updates["pending_agents"] = updates["pending_agents"][1:]
            if agent not in updates["skipped_agents"]:
                updates["skipped_agents"].append(agent)
            updates["agent_status"][agent] = "skipped"
        elif agent == AGENT_SIMULATION and error_type == ERROR_SIMULATION_FAILURE:
            action = "stop_with_error"  # 模拟为强制环节：失败/转人工
            reason = f"模拟为任务强制环节（require_simulation），不允许跳过：{reason_tail}"
            updates["human_review_required"] = True
        elif agent == AGENT_DATA and error_type in (ERROR_MISSING_DATA, "STALE_DATA"):
            action = "stop_with_insufficient_data"
            reason = f"关键数据不足/过期且无法补充：{reason_tail}；下游风险分析停止"
            updates["task_status"] = TASK_INSUFFICIENT_DATA
        elif agent == AGENT_FEEDBACK:
            action = "require_human_review"  # 上游结果保留，反馈缺失须人工把关
            reason = f"Feedback 复核未完成，已有结果保留但需人工复核：{reason_tail}"
            updates["human_review_required"] = True
        else:
            action = "stop_with_error"
            reason = f"{agent} 未能成功且不再重试：{reason_tail}"

    # 停止类动作：当前 Agent 记入 failed（若 result_check 尚未登记）
    if (
        action in ("stop_with_error", "stop_with_insufficient_data", "require_human_review")
        and agent
        and agent not in updates["failed_agents"]
    ):
        if updates["pending_agents"] and updates["pending_agents"][0] == agent:
            updates["pending_agents"] = updates["pending_agents"][1:]
        updates["failed_agents"].append(agent)
        updates["agent_status"][agent] = "failed"

    print(f"[Manager/Retry] {agent}（{error_type}）→ {action}｜{reason}")
    updates["check_result"] = {**check, "retry_action": action, "retry_reason": reason}
    updates["error_message"] = check.get("message", "") or reason
    return updates
