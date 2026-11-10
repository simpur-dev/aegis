"""
Manager 任务规划器（确定性，无 LLM）

【职责】
  1. task_type → 执行计划（结构化 Agent 序列，不产出自然语言计划）。
  2. 依赖校验：simulation_only / warning_only / feedback_only 等"直执行"任务
     必须已有合格的上游交付物；缺失时返回明确的缺依赖清单（不伪造输入）。
  3. 返工依赖闭包：给定目标 Agent 与原计划，返回"目标+下游"的最小返工序列。

【红线】依赖是否满足只看交付物实际 status（SUCCESS_STATUSES），
  Manager 不代替专业 Agent 判断业务内容对错。
"""

from aegis.agents.hazards.manager_agent.state import (
    AGENT_DATA,
    AGENT_FEEDBACK,
    AGENT_ORDER,
    AGENT_RESULT_KEY,
    AGENT_RISK,
    AGENT_SIMULATION,
    AGENT_WARNING,
    PARTIAL_STATUSES,
    SUCCESS_STATUSES,
)

# ============================================================
# 任务类型 → 执行计划
# ============================================================

TASK_TYPE_PLANS = {
    "data_only": [AGENT_DATA],
    "risk_only": [AGENT_DATA, AGENT_RISK],
    "simulation_only": [AGENT_SIMULATION],
    "warning_only": [AGENT_WARNING],
    "feedback_only": [AGENT_FEEDBACK],
    "full_analysis": list(AGENT_ORDER),
}

# 各 Agent 运行所需的上游交付物（键名 → 该上游被视为"可用"的 status 集合）
# data partial 是否可用需再看 data_sufficiency，由 check_ready_inputs 特判。
AGENT_DEPENDENCIES = {
    AGENT_DATA: [],
    AGENT_RISK: [AGENT_DATA],
    AGENT_SIMULATION: [AGENT_DATA, AGENT_RISK],
    AGENT_WARNING: [AGENT_RISK],  # Simulation 非硬依赖
    AGENT_FEEDBACK: [],  # 有任一本次结果即可（特判）
}


def _result_usable(agent: str, result: dict) -> bool:
    """某上游交付物是否存在且状态可用（只读 status，不看业务内容）。"""
    if not isinstance(result, dict) or not result:
        return False
    status = str(result.get("status", ""))
    if status in SUCCESS_STATUSES[agent]:
        return True
    if status in PARTIAL_STATUSES.get(agent, set()):
        # Data partial：仅当自报数据仍"足以支撑评估"才可用（不替它猜）
        if agent == AGENT_DATA:
            return bool((result.get("data_sufficiency") or {}).get("sufficient", False))
        return True
    return False


def check_plan_dependencies(task_type: str, state: dict) -> list:
    """对"直执行"类任务做计划期依赖校验。

    返回缺失依赖清单 [{agent, missing: [上游Agent名], reason}]；空列表 = 计划可行。
    计划内自带上游的任务（data_only/risk_only/full_analysis）天然满足，返回 []。
    """
    plan = TASK_TYPE_PLANS.get(task_type, [])
    missing = []
    for agent in plan:
        # 该 Agent 的上游若不在计划内，就必须已经作为交付物提供
        for dep in AGENT_DEPENDENCIES[agent]:
            if dep in plan:
                continue
            if not _result_usable(dep, state.get(AGENT_RESULT_KEY[dep], {}) or {}):
                missing.append(
                    {
                        "agent": agent,
                        "missing": [dep],
                        "reason": f"{agent} 依赖 {dep} 的合格交付物，但本次任务未提供或状态不可用",
                    }
                )
    # feedback 特判：仅当计划中没有任何生产环节（即 feedback_only 直执行）时，
    # 才要求至少提供一份本次已有结果可供复核
    production_in_plan = any(a in plan for a in (AGENT_DATA, AGENT_RISK, AGENT_SIMULATION, AGENT_WARNING))
    if (
        AGENT_FEEDBACK in plan
        and not production_in_plan
        and all(not (state.get(AGENT_RESULT_KEY[a], {}) or {}) for a in (AGENT_DATA, AGENT_RISK, AGENT_SIMULATION, AGENT_WARNING))
    ):
        missing.append(
            {
                "agent": AGENT_FEEDBACK,
                "missing": ["至少一份上游结果"],
                "reason": "feedback_only 需要提供已有分析结果（data/risk/simulation/warning 任一）",
            }
        )
    return missing


def build_plan(task_type: str) -> list:
    """task_type → 结构化执行计划（未知类型按 full_analysis 由调用方先行拦截）。"""
    return list(TASK_TYPE_PLANS.get(task_type, TASK_TYPE_PLANS["full_analysis"]))


def rework_closure(target_agent: str, original_plan: list) -> list:
    """最小返工计划 = 目标 Agent + 其在原计划中的全部下游（依赖闭包）。

    情况 A~D 的统一实现：
      rework("data_agent") ⊇ [data, risk, sim, warning, feedback]
      rework("risk_agent") ⊇ [risk, sim, warning, feedback]
      rework("warning_agent") = [warning, feedback]
    范围 = 原计划中的环节 ∪ 目标环节，按 AGENT_ORDER 规范顺序从目标位置开始；
    原计划没有的环节不借机扩大范围（如 warning_only 任务不会因此跑 data）。
    """
    if target_agent not in AGENT_ORDER:
        return []
    work_set = {a for a in original_plan if a in AGENT_ORDER} | {target_agent}
    start = AGENT_ORDER.index(target_agent)
    return [a for a in AGENT_ORDER[start:] if a in work_set]
