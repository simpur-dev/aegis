"""
Manager Agent 图装配（LangGraph 原生 StateGraph，中心化 Supervisor 拓扑）

【拓扑】见 router.py 头图。dispatch 是唯一执行枢纽：
  五个专业 Agent 不互为边、不直连，全部经 dispatch 适配层调用，
  跨 Agent 数据只流经 ManagerState。

【Checkpoint】通过 checkpointer 参数注入 SQLite 持久化
  （agents/manager_agent/checkpoint.get_checkpointer，thread_id=event_id）；
  不传则为普通内存执行（测试/单次运行）。
"""

from functools import lru_cache

from langgraph.graph import END, START, StateGraph

from aegis.agents.hazards.manager_agent.nodes.dispatch_node import dispatch_node
from aegis.agents.hazards.manager_agent.nodes.feedback_review_node import feedback_review_node
from aegis.agents.hazards.manager_agent.nodes.finalize_node import finalize_node
from aegis.agents.hazards.manager_agent.nodes.result_check_node import result_check_node
from aegis.agents.hazards.manager_agent.nodes.retry_decision_node import retry_decision_node
from aegis.agents.hazards.manager_agent.nodes.task_analyze_node import task_analyze_node
from aegis.agents.hazards.manager_agent.nodes.task_plan_node import task_plan_node
from aegis.agents.hazards.manager_agent.router import (
    route_after_analyze,
    route_after_check,
    route_after_feedback_review,
    route_after_plan,
    route_after_retry,
)
from aegis.agents.hazards.manager_agent.state import ManagerState


def build_manager_graph() -> StateGraph:
    g = StateGraph(ManagerState)
    g.add_node("task_analyze", task_analyze_node)
    g.add_node("task_plan", task_plan_node)
    g.add_node("dispatch", dispatch_node)
    g.add_node("result_check", result_check_node)
    g.add_node("retry_decision", retry_decision_node)
    g.add_node("feedback_review", feedback_review_node)
    g.add_node("finalize", finalize_node)

    g.add_edge(START, "task_analyze")
    g.add_conditional_edges("task_analyze", route_after_analyze, ["task_plan", "finalize"])
    g.add_conditional_edges("task_plan", route_after_plan, ["dispatch", "finalize"])
    g.add_edge("dispatch", "result_check")
    g.add_conditional_edges("result_check", route_after_check, ["dispatch", "retry_decision", "feedback_review", "finalize"])
    g.add_conditional_edges("retry_decision", route_after_retry, ["dispatch", "feedback_review", "finalize"])
    g.add_conditional_edges("feedback_review", route_after_feedback_review, ["dispatch", "finalize"])
    g.add_edge("finalize", END)
    return g


@lru_cache(maxsize=1)
def _compiled_manager_graph():
    """无 checkpointer 的共享编译图（构建 + 编译只做一次）。

    与 `risk_agent.create_risk_agent` 同一模式：LangGraph 的编译开销每次重建都要付
    （实测 p50 11.5ms 量级）。LangGraph 的 `invoke` 每次新建运行态，同一编译图可被
    多线程并发调用。
    """
    return build_manager_graph().compile()


def create_manager_graph(checkpointer=None):
    """编译 Manager 全局图。

    checkpointer 为空（生产路径常态）时返回共享单例；带 checkpointer 的图因绑定了
    持久化状态不能共享，每次现编。
    """
    if checkpointer is None:
        return _compiled_manager_graph()
    return build_manager_graph().compile(checkpointer=checkpointer)
