"""
Manager → 五个专业 Agent 的调度适配层

【两条接法】
- **总线**（默认）：按 capability 向兄弟本体发一条标准请求、等一条标准回执。
  走总线才谈得上"接了总线"——进程内直调会绕过消息信封、deadline、`msg_id` 幂等与
  类型化错误，等于在自己包里私开一套接线，平台层的门禁与观测全都照不到它。
- **本地**（离线自测 / 单机演示）：直接调用各 Agent 的 `main` 函数。
  只有在没有总线上下文时才走这条。

两条接法产出**同形状**的交付物，闭环的其余部分不关心走的是哪条。

【红线】适配层只组装入参、透传交付物；对交付物内容只读不写。
"""

from typing import Any

from aegis.agents.hazards.bus_context import BusContext
from aegis.agents.hazards.contracts import (
    chain_audit_request,
    data_quality_request,
    factor_risk_request,
    scenario_sim_request,
    warn_content_request,
)
from aegis.agents.hazards.manager_agent.state import (
    AGENT_DATA,
    AGENT_FEEDBACK,
    AGENT_RISK,
    AGENT_SIMULATION,
    AGENT_WARNING,
)
from aegis.domain.enums import Action


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
    from aegis.agents.hazards.data_agent.main import run_data_agent

    return run_data_agent(_task_desc(state, "获取并处理观测数据"), hazard_type=state["hazard_type"])


def run_risk(state: dict) -> dict:
    from aegis.agents.hazards.risk_agent.legacy import run_risk_agent_legacy

    return run_risk_agent_legacy(
        _task_desc(state, "评估灾害风险"), data_result=state.get("data_result", {}) or {}, hazard_type=state["hazard_type"]
    )


def run_simulation(state: dict) -> dict:
    from aegis.agents.hazards.simulation_agent.main import run_simulation_agent

    return run_simulation_agent(
        _task_desc(state, "设计情景并模拟推演"),
        data_result=state.get("data_result", {}) or {},
        risk_result=state.get("risk_result", {}) or {},
        hazard_type=state["hazard_type"],
        preset_scenario_configs=list(state.get("preset_scenario_configs") or []) or None,
    )


def run_warning(state: dict) -> dict:
    from aegis.agents.hazards.warning_agent.main import run_warning_agent

    return run_warning_agent(
        _task_desc(state, "生成预警草稿"),
        hazard_type=state["hazard_type"],
        risk_result=state.get("risk_result", {}) or {},
        simulation_result=state.get("simulation_result", {}) or None,
        data_result=state.get("data_result", {}) or None,
    )


def run_feedback(state: dict) -> dict:
    from aegis.agents.hazards.feedback_agent.main import run_feedback_agent

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
    """执行一个专业 Agent，返回其交付物 dict（本地接法）。

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


# ============================================================
# 总线接法：闭环的五步 → 总线上的能力与动作
# ============================================================

#: 闭环的哪一步对应总线上哪项能力与哪个动作。与 `agents/capabilities.py` 的能力
#: 矩阵一一对应——同一个 capability 在平台链路与闭环里指的是**同一个本体**。
BUS_LEGS: dict[str, tuple[str, Action]] = {
    AGENT_DATA: ("data_quality", Action.ASSESS_DATA_QUALITY),
    AGENT_RISK: ("factor_risk_assess", Action.ASSESS_FACTOR_RISK),
    AGENT_SIMULATION: ("scenario_sim", Action.ASSESS_SCENARIO_SIM),
    AGENT_WARNING: ("warn_content", Action.EXECUTE_WARN_CONTENT),
    AGENT_FEEDBACK: ("chain_audit", Action.FEEDBACK_CHAIN_AUDIT),
}

_PURPOSES = {
    AGENT_DATA: "获取并处理观测数据",
    AGENT_RISK: "评估灾害风险",
    AGENT_SIMULATION: "设计情景并模拟推演",
    AGENT_WARNING: "生成预警草稿",
    AGENT_FEEDBACK: "复核本次分析链路",
}


def _deliverable(state: dict, key: str) -> dict[str, Any]:
    """取上一腿的交付物。

    优先取原生产出（`native`）：闭环的下游腿本来就是按原形状写的，硬改成契约形状
    反而要在下游再翻译一遍。`native` 不在时退回整份应答（本地接法的产物没有它）。
    """
    payload = state.get(key) or {}
    if not isinstance(payload, dict):
        return {}
    native = payload.get("native")
    return native if isinstance(native, dict) else payload


def bus_request_for(agent_name: str, state: dict) -> tuple[str, Action, dict[str, Any]]:
    """闭环这一步该向总线发什么：能力、动作、请求载荷。"""
    if agent_name not in BUS_LEGS:
        raise KeyError(f"闭环没有这条腿的总线映射: {agent_name}")
    capability, action = BUS_LEGS[agent_name]
    hazard_type = str(state.get("hazard_type") or "")
    task = _task_desc(state, _PURPOSES[agent_name])

    if agent_name == AGENT_DATA:
        payload = data_quality_request(
            hazard_type=hazard_type,
            region_code=str(state.get("region") or ""),
            fetch=True,
            task=task,
        )
    elif agent_name == AGENT_RISK:
        payload = factor_risk_request(
            hazard_type=hazard_type,
            region_code=str(state.get("region") or ""),
            data_result=_deliverable(state, "data_result"),
        )
    elif agent_name == AGENT_SIMULATION:
        payload = scenario_sim_request(
            hazard_type=hazard_type,
            data_result=_deliverable(state, "data_result"),
            risk_result=_deliverable(state, "risk_result"),
            task=task,
            preset_scenario_configs=list(state.get("preset_scenario_configs") or []) or None,
        )
    elif agent_name == AGENT_WARNING:
        payload = warn_content_request(
            hazard_type=hazard_type,
            risk_result=_deliverable(state, "risk_result"),
            simulation_result=_deliverable(state, "simulation_result"),
            data_result=_deliverable(state, "data_result"),
        )
    else:
        payload = chain_audit_request(
            hazard_type=hazard_type,
            data_result=_deliverable(state, "data_result"),
            risk_result=_deliverable(state, "risk_result"),
            simulation_result=_deliverable(state, "simulation_result"),
            warning_result=_deliverable(state, "warning_result"),
            event_id=str(state.get("event_id") or ""),
        )
    return capability, action, payload


def dispatch_over_bus(agent_name: str, state: dict, bus: BusContext, *, trace_id: str) -> dict[str, Any]:
    """通过总线向承接这一步的兄弟本体派单，返回它的交付物。"""
    capability, action, payload = bus_request_for(agent_name, state)
    return bus.request_sync(capability, action, payload, trace_id=trace_id)


__all__ = [
    "AGENT_RUNNERS",
    "BUS_LEGS",
    "bus_request_for",
    "dispatch",
    "dispatch_over_bus",
    "restore_runners",
    "set_runner",
]
