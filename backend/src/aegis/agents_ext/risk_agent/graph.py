"""
Risk Agent 的 LangGraph 图构建

【图结构】与你 Data Agent 对称：
    assess → validate → [条件路由]
                          ├─ output → END
                          └─ assess (重试回环)

【路由风格对齐你 Data】
  你 Data 的 route_after_validate 读 data_quality + retry_count + 常量 MAX_RETRIES；
  这里 route_after_validate 读 validation_result + retry_count + state.max_retries(默认3)。
  都不读 next（next 仅作记录），完全镜像。

【对外契约】
  build_risk_agent_graph() / create_risk_agent()
    # 提供给全局 workflow / Manager 调用
"""

from langgraph.graph import END, StateGraph
from langgraph.graph.state import CompiledStateGraph

from aegis.agents_ext.risk_agent.nodes.risk_assess_node import risk_assess_node
from aegis.agents_ext.risk_agent.nodes.risk_output_node import risk_output_node
from aegis.agents_ext.risk_agent.nodes.risk_validate_node import risk_validate_node
from aegis.agents_ext.risk_agent.state import RiskAgentState


def route_after_validate(state: RiskAgentState) -> str:
    """
    条件路由：在 RiskValidate 之后决定下一步。

    逻辑（镜像你 Data 的 route_after_validate）：
      - 验证通过(proceed) → 输出
      - 建议 retry 且 重试次数 < max_retries → 重新评估（循环）
      - 建议 retry 但 超过上限 → 强制输出（标 uncertain）
      - 建议 fail → 直接输出（标 failed）
    """
    validation = state.get("validation_result", {})
    retry_count = state.get("retry_count", 0)
    # max_retries 可选：State 没填则默认 3，对齐你 Data 的 MAX_RETRIES=3
    max_retries = state.get("max_retries", 3)

    rec = validation.get("recommendation", "output")  ##读取校验报告里面的`recommendation`推荐指令

    # fail：数据根本不可用，重试评估没用，直接输出
    if rec == "fail":
        return "risk_output"

    # proceed：通过，输出
    if rec == "proceed":
        return "risk_output"

    # retry：证据不足，未超重试则回 assess，否则强制输出
    if rec == "retry" and retry_count < max_retries:
        return "risk_assess"
    return "risk_output"


def build_risk_agent_graph() -> CompiledStateGraph:
    """构建 Risk Agent 的 LangGraph 图，返回编译后的可执行图。"""

    # 1. 创建状态图
    graph = StateGraph(RiskAgentState)

    # 2. 添加节点（命名对齐你 Data 的 data_xxx 风格 → risk_xxx）
    graph.add_node("risk_assess", risk_assess_node)
    graph.add_node("risk_validate", risk_validate_node)
    graph.add_node("risk_output", risk_output_node)

    # 3. 入口
    graph.set_entry_point("risk_assess")

    # 4. 固定边
    graph.add_edge("risk_assess", "risk_validate")

    # 5. 条件边（核心路由，镜像你 Data）
    graph.add_conditional_edges(
        "risk_validate",
        route_after_validate,
        {
            "risk_assess": "risk_assess",  # 重试 → 回到评估（形成循环）
            "risk_output": "risk_output",  # 通过/放弃 → 输出
        },
    )

    # 6. 输出连 END
    graph.add_edge("risk_output", END)

    # 7. 编译
    return graph.compile()


def create_risk_agent():
    """便捷函数：创建并返回编译好的 Risk Agent 图。>>> 给 Manager 用"""
    return build_risk_agent_graph()
