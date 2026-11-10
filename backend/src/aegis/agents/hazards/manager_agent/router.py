"""
Manager 条件路由（全部为确定性 Python 逻辑，LLM 不参与控制流决策）

拓扑（hub-and-spoke，dispatch 是唯一执行枢纽）：

  START → task_analyze ─┬→ task_plan ─┬→ dispatch → result_check ─┬→ dispatch（下一环节）
                        │             │（依赖不满足）              ├→ retry_decision ─┬→ dispatch（重试/跳过）
                        │             └→ finalize                  │                  ├→ feedback_review
                        ├→ finalize（need_clarification /          │                  └→ finalize（停止）
                        │   unsupported_hazard）                   ├→ feedback_review（feedback 完成）
                        └→（正常继续）                              └→ finalize（队列完成）
                                          feedback_review ─┬→ dispatch（最小返工计划）
                                                           └→ finalize
"""

from aegis.agents.hazards.manager_agent.state import AGENT_FEEDBACK


def route_after_analyze(state: dict) -> str:
    """解析定终态（need_clarification/unsupported_hazard）→ 直接收尾。"""
    if state.get("task_status"):
        return "finalize"
    return "task_plan"


def route_after_plan(state: dict) -> str:
    """依赖不满足 → 收尾；否则进入调度枢纽。"""
    if state.get("task_status"):
        return "finalize"
    return "dispatch"


def route_after_check(state: dict) -> str:
    """刚核验的交付物：
    failed        → retry_decision
    ok + feedback → feedback_review
    ok + 队列非空 → dispatch（下一环节）
    ok + 队列空   → finalize
    """
    check = state.get("check_result") or {}
    if check.get("verdict") == "failed":
        return "retry_decision"
    if check.get("agent") == AGENT_FEEDBACK:
        return "feedback_review"
    if state.get("pending_agents"):
        return "dispatch"
    return "finalize"


def route_after_retry(state: dict) -> str:
    """按 retry_decision 写入 check_result.retry_action 分派（确定性动作集合）。"""
    action = (state.get("check_result") or {}).get("retry_action", "stop_with_error")
    if action in (
        "retry_current_agent",  # 同 Agent 重试（仍在队首）
        "call_next_agent",
        "skip_optional_agent",
    ):  # 已弹出并登记 skipped，继续下游
        return "dispatch"
    # stop_with_insufficient_data / stop_with_error / require_human_review → 收尾
    return "finalize"


def route_after_feedback_review(state: dict) -> str:
    """有获批返工计划 → 回到调度枢纽；否则收尾。"""
    if state.get("pending_agents"):
        return "dispatch"
    return "finalize"
