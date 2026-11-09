"""
task_plan_node：根据任务目标与依赖关系生成结构化执行计划（全确定性）

【规则（任务书第五节 3）】
  - 计划必须是结构化 Agent 序列，不产出自然语言计划；
  - simulation_only / warning_only / feedback_only 缺少合格前置结果时，
    返回明确的缺依赖状态（MISSING_DEPENDENCY），绝不伪造输入；
  - 外部直供的交付物只登记为 "provided"，Manager 不修改其内容。
"""

from aegis.agents_ext.manager_agent import planner
from aegis.agents_ext.manager_agent.state import (
    AGENT_ORDER,
    AGENT_RESULT_KEY,
    ERROR_MISSING_DEPENDENCY,
    TASK_INSUFFICIENT_DATA,
)


def task_plan_node(state: dict) -> dict:
    """生成执行计划并做计划期依赖校验。"""
    updates: dict = {"error_history": list(state.get("error_history") or [])}

    task_type = state.get("task_type", "full_analysis")
    plan = planner.build_plan(task_type)
    missing = planner.check_plan_dependencies(task_type, state)

    if missing:
        reasons = "；".join(m["reason"] for m in missing)
        updates["task_status"] = TASK_INSUFFICIENT_DATA
        updates["error_message"] = f"任务'{task_type}'缺少合格前置结果：{reasons}"
        updates["error_history"].append({"agent": "manager", "error_type": ERROR_MISSING_DEPENDENCY, "message": reasons, "attempt": 0})
        updates["check_result"] = {"verdict": "missing_dependency", "missing": missing}
        print(f"[Manager/Plan] 依赖不满足 → {TASK_INSUFFICIENT_DATA}：{reasons}")
        return updates

    updates["execution_plan"] = plan
    updates["pending_agents"] = list(plan)
    # 登记外部直供的上游交付物（不在计划内但已可用）
    agent_status = dict(state.get("agent_status") or {})
    provided = [a for a in AGENT_ORDER if a not in plan and (state.get(AGENT_RESULT_KEY[a], {}) or {})]
    for a in provided:
        agent_status.setdefault(a, "provided")
    updates["agent_status"] = agent_status

    print(f"[Manager/Plan] task_type={task_type} → 执行计划={plan}" + (f" | 直供上游={provided}" if provided else ""))
    return updates
