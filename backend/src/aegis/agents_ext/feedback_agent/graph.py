"""
Feedback Agent 的 LangGraph 图构建

【图结构】（对齐任务书建议架构 + langgraph-reflection 的条件路由思想）

    feedback_collect → feedback_analyze → feedback_evaluate → ConditionalRouter
        ├─ 有高置信可行动问题（且反思预算未耗尽） → reflection → suggestion ─┬─回环(有界)→ reflection
        │                                                                    └→ memory_store → feedback_output
        ├─ 有问题但证据不足/低置信（不值得反思） → memory_store → feedback_output
        ├─ 无问题 / 灾种被拒绝 → feedback_output
        └─ feedback_output → END

【防无限循环】（借鉴 langgraph-reflection 的最大迭代控制）
    reflection 每次进入 iteration_count 自增；
    evaluate 与 suggestion 两处路由都检查 iteration_count < max_iterations，
    预算耗尽一律走输出 → 图必然有限步终止。

【对外契约】
  build_feedback_agent_graph() / create_feedback_agent()（给 Manager 用）
"""

from langgraph.graph import END, StateGraph

from aegis.agents_ext.feedback_agent.nodes.feedback_analyze_node import feedback_analyze_node
from aegis.agents_ext.feedback_agent.nodes.feedback_collect_node import feedback_collect_node
from aegis.agents_ext.feedback_agent.nodes.feedback_evaluate_node import feedback_evaluate_node
from aegis.agents_ext.feedback_agent.nodes.feedback_output_node import feedback_output_node
from aegis.agents_ext.feedback_agent.nodes.memory_store_node import memory_store_node
from aegis.agents_ext.feedback_agent.nodes.reflection_node import reflection_node
from aegis.agents_ext.feedback_agent.nodes.suggestion_node import suggestion_node
from aegis.agents_ext.feedback_agent.state import FeedbackAgentState


def route_after_evaluate(state: FeedbackAgentState) -> str:
    """条件路由：有高置信问题且反思预算未耗尽 → reflection；
    有可行动问题但证据不足（反思无意义）→ memory_store 存档待复核；
    无问题 / 灾种被拒绝 → 直接输出。"""
    ev = state.get("evaluation_result", {}) or {}
    max_iter = state.get("max_iterations", 2)
    iteration = state.get("iteration_count", 0)

    if ev.get("recommendation") == "reflect" and iteration < max_iter:
        return "reflection"
    if ev.get("actionable_issue_count", 0) > 0:
        return "memory_store"
    return "feedback_output"


def route_after_suggestion(state: FeedbackAgentState) -> str:
    """建议后路由：回环请求且预算未耗尽 → 再次 reflection（有界）；否则存档。"""
    max_iter = state.get("max_iterations", 2)
    if state.get("needs_another_round") and state.get("iteration_count", 0) < max_iter:
        return "reflection"
    return "memory_store"


def build_feedback_agent_graph():
    """构建并编译 Feedback Agent 的 LangGraph 图。"""
    graph = StateGraph(FeedbackAgentState)

    graph.add_node("feedback_collect", feedback_collect_node)
    graph.add_node("feedback_analyze", feedback_analyze_node)
    graph.add_node("feedback_evaluate", feedback_evaluate_node)
    graph.add_node("reflection", reflection_node)
    graph.add_node("suggestion", suggestion_node)
    graph.add_node("memory_store", memory_store_node)
    graph.add_node("feedback_output", feedback_output_node)

    graph.set_entry_point("feedback_collect")
    graph.add_edge("feedback_collect", "feedback_analyze")
    graph.add_edge("feedback_analyze", "feedback_evaluate")

    graph.add_conditional_edges(
        "feedback_evaluate",
        route_after_evaluate,
        {"reflection": "reflection", "memory_store": "memory_store", "feedback_output": "feedback_output"},
    )
    graph.add_edge("reflection", "suggestion")
    graph.add_conditional_edges(
        "suggestion",
        route_after_suggestion,
        {"reflection": "reflection", "memory_store": "memory_store"},
    )
    graph.add_edge("memory_store", "feedback_output")
    graph.add_edge("feedback_output", END)

    return graph.compile()


def create_feedback_agent():
    """便捷函数：创建并返回编译好的 Feedback Agent 图。>>> 给 Manager 用"""
    return build_feedback_agent_graph()
