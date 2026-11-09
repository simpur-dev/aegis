"""
Manager → 五个专业 Agent 的调度适配层

【设计】
  - 直接复用各 Agent main.py 已声明的"对外契约"函数（run_*_agent），
    不复制任何 Agent 内部业务逻辑，也不重写它们；五个 Agent 零修改。
  - AGENT_RUNNERS 是名字→适配函数的注册表：单元测试整体替换为 Mock 子图
    （不依赖真实 LLM/API），集成测试换回默认真实实现即可。
  - 每次调用产出结构化记录（agent / attempt / status / error），由
    dispatch_node 写入 ManagerState.agent_call_log。

【红线】适配函数只组装入参、透传交付物；对交付物内容只读不写。
"""

from aegis.agents_ext.manager_agent.state import (
    AGENT_DATA,
    AGENT_FEEDBACK,
    AGENT_RISK,
    AGENT_SIMULATION,
    AGENT_WARNING,
)


def _task_desc(state: dict, purpose: str) -> str:
    """组装传给专业 Agent 的任务描述文本（region/analysis_time 仅作为上下文
    写入描述 —— Data Agent 并不按它们过滤数据，能力限制在最终报告中说明）。"""
    parts = [f"[Manager调度|{state.get('event_id', '')}] {purpose}"]
    if state.get("region"):
        parts.append(f"关注区域：{state['region']}（上下文信息）")
    if state.get("analysis_time"):
        parts.append(f"分析时间：{state['analysis_time']}（上下文信息）")
    if state.get("user_request"):
        parts.append(f"用户原始请求：{state['user_request']}")
    return "；".join(parts)


# ============================================================
# 默认适配函数：调用各 Agent 的对外契约入口（懒导入，避免测试装载全部重依赖）
# ============================================================


def run_data(state: dict) -> dict:
    """Data Agent：返回最终平铺 State（全量保留 —— 下游就按平铺口径读取）。"""
    from aegis.agents_ext.data_agent.main import run_data_agent

    return run_data_agent(_task_desc(state, "获取并处理观测数据"), hazard_type=state["hazard_type"])


def run_risk(state: dict) -> dict:
    from aegis.agents_ext.risk_agent.legacy import run_risk_agent_legacy

    return run_risk_agent_legacy(
        _task_desc(state, "评估灾害风险"), data_result=state.get("data_result", {}) or {}, hazard_type=state["hazard_type"]
    )


def run_simulation(state: dict) -> dict:
    from aegis.agents_ext.simulation_agent.main import run_simulation_agent

    return run_simulation_agent(
        _task_desc(state, "设计情景并模拟推演"),
        data_result=state.get("data_result", {}) or {},
        risk_result=state.get("risk_result", {}) or {},
        hazard_type=state["hazard_type"],
        preset_scenario_configs=list(state.get("preset_scenario_configs") or []) or None,
    )


def run_warning(state: dict) -> dict:
    from aegis.agents_ext.warning_agent.main import run_warning_agent

    return run_warning_agent(
        _task_desc(state, "生成预警草稿"),
        hazard_type=state["hazard_type"],
        risk_result=state.get("risk_result", {}) or {},
        simulation_result=state.get("simulation_result", {}) or None,
        data_result=state.get("data_result", {}) or None,
    )


def run_feedback(state: dict) -> dict:
    from aegis.agents_ext.feedback_agent.main import run_feedback_agent

    return run_feedback_agent(
        _task_desc(state, "复核本次分析链路"),
        hazard_type=state["hazard_type"],
        data_result=state.get("data_result", {}) or {},
        risk_result=state.get("risk_result", {}) or {},
        simulation_result=state.get("simulation_result", {}) or None,
        warning_result=state.get("warning_result", {}) or None,
        event_id=state.get("event_id", ""),
        external_feedback=state.get("test_hooks", {}).get("external_feedback") or None,
    )


# 注册表：单元测试可整体/逐个替换为 Mock 子图（set_runner / restore_runners）
AGENT_RUNNERS = {
    AGENT_DATA: run_data,
    AGENT_RISK: run_risk,
    AGENT_SIMULATION: run_simulation,
    AGENT_WARNING: run_warning,
    AGENT_FEEDBACK: run_feedback,
}

_DEFAULT_RUNNERS = dict(AGENT_RUNNERS)


def set_runner(agent_name: str, fn):
    """替换某 Agent 的适配实现（测试注入 Mock 用）。"""
    AGENT_RUNNERS[agent_name] = fn


def restore_runners():
    """恢复全部默认（真实 Agent）适配实现。"""
    AGENT_RUNNERS.clear()
    AGENT_RUNNERS.update(_DEFAULT_RUNNERS)


def dispatch(agent_name: str, state: dict) -> dict:
    """执行一个专业 Agent，返回其交付物 dict。

    异常不在这里吞掉 —— 由 dispatch_node 统一转成 AGENT_EXECUTION_ERROR
    / AGENT_TIMEOUT 错误记录，保证 error_history 可追溯。
    """
    runner = AGENT_RUNNERS.get(agent_name)
    if runner is None:
        raise KeyError(f"未注册的专业 Agent: {agent_name}")
    output = runner(state)
    if not isinstance(output, dict):
        raise TypeError(f"{agent_name} 返回的不是 dict，而是 {type(output).__name__}")
    return output
