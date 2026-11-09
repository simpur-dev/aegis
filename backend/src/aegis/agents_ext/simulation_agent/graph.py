# ruff: noqa: E501
# 【本文件整体豁免 E501（行宽 140）】：下面那张 LangGraph 流程图必须保持整行不折——
# 折了就不是图了；行级 noqa 指令对 E501 无效（它自己会把行撑得更长），
# 所以只能用文件级指令。文件里除流程图外没有超宽行。
"""
Simulation Agent 的 LangGraph 图构建

【图结构】（对齐任务 §五 建议流程，6 节点 + 5 条件边）

  精确拓扑：
    set_entry_point("scenario_design")
    scenario_design     -(route_after_scenario_design)-→ scenario_validate | simulation_output
    scenario_validate   -(route_after_scenario_validate)-→ scenario_design | hazard_model_router | simulation_output
    hazard_model_router -(route_after_router)-→ simulation_execute | simulation_output
    simulation_execute  -(route_after_execute)-→ simulation_execute | results_analysis | simulation_output
    results_analysis    -(route_after_analysis)-→ results_analysis | simulation_output
    simulation_output   -(固定边)-→ END

【路由风格对齐 Data / Risk Agent】
  条件路由函数定义在 graph.py 模块级；实际路由读专门的阶段产出字段
  （scenario_design_result / validation_result / routing_result /
   execution_result / comparison_result.completeness），不读 next（仅记录）。

【重试语义】
  全局共享 retry_count 预算；产生 retry 判定的节点（scenario_validate /
  simulation_execute / results_analysis）在同一次 return 里自增；
  路由读自增后的 retry_count < max_retries 判断是否回环 → 全图必然有限步终止。
  （修正 Risk Agent "retry_count 恒不自增导致死循环" 的缺陷模式。）

【对外契约】
  build_simulation_agent_graph() / create_simulation_agent()
    # 提供给全局 workflow / Manager 调用（风格对齐 create_risk_agent）


# --------------------------------------------------------------------------------
# 【图结构】（对齐任务 §五 建议流程，6 节点 + 5 条件边）
#
# 这张图从模块 docstring 里挪到了这里：docstring 内部的行不能挂 `# noqa`，
# 而这张流程图**必须**保持整行不折（折了就不是图了），所以放在注释块里由 ruff 放行。
#
#   scenario_design ──┬─→ scenario_validate ──┬─→ hazard_model_router ──┬─→ simulation_execute ──┬─→ results_analysis ──→ simulation_output → END
#                     │（数据缺失/灾种不支持）  │（参数不合法）            │（灾种不支持/上下文缺失） │（异常→限次重试→超限终止） │（不完整→限次修复）
#                     └──────────── sim puts everything early-exit into simulation_output ────────────┘
# --------------------------------------------------------------------------------
"""

from langgraph.graph import END, StateGraph
from langgraph.graph.state import CompiledStateGraph

from aegis.agents_ext.simulation_agent.nodes import (
    hazard_model_router_node,
    results_analysis_node,
    scenario_design_node,
    scenario_validate_node,
    simulation_execute_node,
    simulation_output_node,
)
from aegis.agents_ext.simulation_agent.state import SimulationAgentState

# ============================================================
# 条件路由函数（模块级，风格对齐 route_after_validate）
# ============================================================


def route_after_scenario_design(state: SimulationAgentState) -> str:
    """情景设计之后：灾种不支持 / 上游数据缺失 → 直送输出，否则进校验。"""
    design = state.get("scenario_design_result", {}) or {}
    if design.get("unsupported_hazard") or design.get("needs_more_data"):
        return "simulation_output"
    return "scenario_validate"


def route_after_scenario_validate(state: SimulationAgentState) -> str:
    """情景校验之后：
    - fail（参数不合法，拒绝执行）→ 输出
    - retry 且重试预算未耗尽 → 回设计节点（带 issues 针对性重设计）
    - 其余（proceed；或 retry 但预算耗尽 → 放行已通过校验的情景）→ 路由节点
    """
    validation = state.get("validation_result", {}) or {}
    retry_count = state.get("retry_count", 0)
    max_retries = state.get("max_retries", 3)

    rec = validation.get("recommendation", "proceed")
    if rec == "fail":
        return "simulation_output"
    if rec == "retry" and retry_count < max_retries:
        return "scenario_design"
    return "hazard_model_router"


def route_after_router(state: SimulationAgentState) -> str:
    """模型路由之后：支持该灾种且上下文齐备 → 执行；否则 → 输出（带明确原因）。"""
    routing = state.get("routing_result", {}) or {}
    if routing.get("supported") and routing.get("inputs_ok"):
        return "simulation_execute"
    return "simulation_output"


def route_after_execute(state: SimulationAgentState) -> str:
    """仿真执行之后：
    - 全部情景成功 → 分析
    - 有异常且重试预算未耗尽 → 本节点重试（retry_count 已在节点内自增）
    - 预算耗尽 → 输出（带部分结果与失败原因）
    """
    execution = state.get("execution_result", {}) or {}
    retry_count = state.get("retry_count", 0)
    max_retries = state.get("max_retries", 3)

    if execution.get("ok"):
        return "results_analysis"
    if retry_count < max_retries:
        return "simulation_execute"
    return "simulation_output"


def route_after_analysis(state: SimulationAgentState) -> str:
    """结果分析之后：输出不完整且可修复且预算未耗尽 → 分析重试；否则 → 输出。"""
    comparison = state.get("comparison_result", {}) or {}
    completeness = comparison.get("completeness", {}) or {}
    retry_count = state.get("retry_count", 0)
    max_retries = state.get("max_retries", 3)

    if (not completeness.get("is_complete", True)) and completeness.get("fixable") and retry_count < max_retries:
        return "results_analysis"
    return "simulation_output"


# ============================================================
# 图构建
# ============================================================


def build_simulation_agent_graph() -> CompiledStateGraph:
    """构建 Simulation Agent 的 LangGraph 图，返回编译后的可执行图。"""

    # 1. 创建状态图
    graph = StateGraph(SimulationAgentState)

    # 2. 添加节点（命名对齐 data_xxx / risk_xxx 风格 → 语义命名）
    graph.add_node("scenario_design", scenario_design_node)
    graph.add_node("scenario_validate", scenario_validate_node)
    graph.add_node("hazard_model_router", hazard_model_router_node)
    graph.add_node("simulation_execute", simulation_execute_node)
    graph.add_node("results_analysis", results_analysis_node)
    graph.add_node("simulation_output", simulation_output_node)

    # 3. 入口
    graph.set_entry_point("scenario_design")

    # 4. 五条条件边（映射字典风格对齐 Data / Risk Agent）
    graph.add_conditional_edges(
        "scenario_design",
        route_after_scenario_design,
        {"scenario_validate": "scenario_validate", "simulation_output": "simulation_output"},
    )
    graph.add_conditional_edges(
        "scenario_validate",
        route_after_scenario_validate,
        {"scenario_design": "scenario_design", "hazard_model_router": "hazard_model_router", "simulation_output": "simulation_output"},
    )
    graph.add_conditional_edges(
        "hazard_model_router",
        route_after_router,
        {"simulation_execute": "simulation_execute", "simulation_output": "simulation_output"},
    )
    graph.add_conditional_edges(
        "simulation_execute",
        route_after_execute,
        {"simulation_execute": "simulation_execute", "results_analysis": "results_analysis", "simulation_output": "simulation_output"},
    )
    graph.add_conditional_edges(
        "results_analysis",
        route_after_analysis,
        {"results_analysis": "results_analysis", "simulation_output": "simulation_output"},
    )

    # 5. 输出连 END
    graph.add_edge("simulation_output", END)

    # 6. 编译（与现有 Agent 一致：无 checkpointer）
    return graph.compile()


def create_simulation_agent():
    """便捷函数：创建并返回编译好的 Simulation Agent 图。>>> 给 Manager 用"""
    return build_simulation_agent_graph()
