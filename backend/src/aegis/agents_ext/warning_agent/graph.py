"""
Warning Agent 的 LangGraph 图构建

【图结构】（对齐任务书建议流程）
    warning_decision ──┬─(条件)→ hazard_warning_rule → alert_builder → llm_explanation
                       │                                            │
                       │                                   ┌────────┴─────────┐
                       │                                   │(retry, 限次)      │(proceed/fail)
                       │                                   ▼                  ▼
                       └─(条件不足)──────────────────→ llm_explanation   warning_output → END

  精确拓扑：
    set_entry_point("warning_decision")
    warning_decision   -(route_after_decision)-→ hazard_warning_rule | warning_output
    hazard_warning_rule → alert_builder（固定边）
    alert_builder       → llm_explanation（固定边）
    llm_explanation     → warning_validator（固定边）
    warning_validator   -(route_after_validator)-→ llm_explanation | warning_output
    warning_output      → END

【重试语义】解释校验 retry 时，validator 节点原子自增 retry_count，
  路由读 retry_count < max_retries 判断回环 → 有限步终止。

【对外契约】
  build_warning_agent_graph() / create_warning_agent()（给 Manager 用）
"""

from langgraph.graph import END, StateGraph
from langgraph.graph.state import CompiledStateGraph

from aegis.agents_ext.warning_agent.nodes.alert_builder_node import alert_builder_node
from aegis.agents_ext.warning_agent.nodes.hazard_warning_rule_node import hazard_warning_rule_node
from aegis.agents_ext.warning_agent.nodes.llm_explanation_node import llm_explanation_node
from aegis.agents_ext.warning_agent.nodes.warning_decision_node import warning_decision_node
from aegis.agents_ext.warning_agent.nodes.warning_output_node import warning_output_node
from aegis.agents_ext.warning_agent.nodes.warning_validator_node import warning_validator_node
from aegis.agents_ext.warning_agent.state import WarningAgentState


def route_after_decision(state: WarningAgentState) -> str:
    """条件不足（灾种无效/上游不达标）→ 直送输出；否则进入规则映射。"""
    decision = state.get("decision_result", {}) or {}
    if decision.get("can_warn", False):
        return "hazard_warning_rule"
    return "warning_output"


def route_after_validator(state: WarningAgentState) -> str:
    """校验 retry 且预算未耗尽 → 回解释节点；否则 → 输出。"""
    validation = state.get("validation_result", {}) or {}
    retry_count = state.get("retry_count", 0)
    max_retries = state.get("max_retries", 3)

    if validation.get("recommendation") == "retry" and retry_count < max_retries:
        return "llm_explanation"
    return "warning_output"


def build_warning_agent_graph() -> CompiledStateGraph:
    """构建 Warning Agent 的 LangGraph 图，返回编译后的可执行图。"""

    graph = StateGraph(WarningAgentState)

    graph.add_node("warning_decision", warning_decision_node)
    graph.add_node("hazard_warning_rule", hazard_warning_rule_node)
    graph.add_node("alert_builder", alert_builder_node)
    graph.add_node("llm_explanation", llm_explanation_node)
    graph.add_node("warning_validator", warning_validator_node)
    graph.add_node("warning_output", warning_output_node)

    graph.set_entry_point("warning_decision")

    graph.add_conditional_edges(
        "warning_decision",
        route_after_decision,
        {"hazard_warning_rule": "hazard_warning_rule", "warning_output": "warning_output"},
    )
    graph.add_edge("hazard_warning_rule", "alert_builder")
    graph.add_edge("alert_builder", "llm_explanation")
    graph.add_edge("llm_explanation", "warning_validator")
    graph.add_conditional_edges(
        "warning_validator",
        route_after_validator,
        {"llm_explanation": "llm_explanation", "warning_output": "warning_output"},
    )

    graph.add_edge("warning_output", END)

    return graph.compile()


def create_warning_agent():
    """便捷函数：创建并返回编译好的 Warning Agent 图。>>> 给 Manager 用"""
    return build_warning_agent_graph()
