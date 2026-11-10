"""
Data Agent 的 LangGraph 图构建

使用 LangGraph 的 StateGraph 构建：
  - Node（节点）
  - Edge（边）
  - Conditional Edge（条件边）
  - Loop（循环：数据不合格时重新获取）
  - END（终止）

图结构：
  DataPlan ─┬─(灾种合法)→ DataRetrieve → DataClean → DataValidate
            │                                       │
            │(灾种缺失/无法识别)          ┌─────────┴─────────┐
            │                            │                   │
            └──────────────────▶ (不合格/重试)         (合格/超重试)
                                         │                   │
                                         ▼                   ▼
                                   DataRetrieve          DataOutput → END
"""

from functools import lru_cache

from langgraph.graph import END, StateGraph
from langgraph.graph.state import CompiledStateGraph

from aegis.agents.hazards.data_agent.nodes import (
    data_clean_node,
    data_output_node,
    data_plan_node,
    data_retrieve_node,
    data_validate_node,
)
from aegis.agents.hazards.data_agent.state import DataAgentState


def route_after_plan(state: DataAgentState) -> str:
    """
    条件路由函数：在 DataPlan 之后决定下一步。

    逻辑：
      - data_requirement 为空（灾种缺失/无法识别，plan 已 fail-fast）→ 直接输出
      - 否则 → 正常进入数据获取
    """
    if not state.get("data_requirement"):
        return "data_output"
    return "data_retrieve"


def route_after_validate(state: DataAgentState) -> str:
    """
    条件路由函数：在 DataValidate 之后决定下一步。

    逻辑：
      - 数据合格 → 输出
      - 数据不合格 且 重试次数 < 3 → 重新获取
      - 数据不合格 且 重试次数 >= 3 → 强制输出（标记失败）
    """
    data_quality = state.get("data_quality", {})
    retry_count = state.get("retry_count", 0)
    error_message = state.get("error_message", "")

    # 如果有严重错误，直接输出
    if error_message:
        return "data_output"

    # 数据合格，输出
    if data_quality.get("is_valid", False):
        return "data_output"

    # 超过最大重试次数，强制输出
    MAX_RETRIES = 3
    if retry_count >= MAX_RETRIES:
        return "data_output"

    # 数据不合格，重新获取
    return "data_retrieve"


def build_data_agent_graph() -> CompiledStateGraph:
    """
    构建 Data Agent 的 LangGraph 图。

    返回编译后的可执行图。
    """

    # 1. 创建状态图
    graph = StateGraph(DataAgentState)

    # 2. 添加节点
    graph.add_node("data_plan", data_plan_node)
    graph.add_node("data_retrieve", data_retrieve_node)
    graph.add_node("data_clean", data_clean_node)
    graph.add_node("data_validate", data_validate_node)
    graph.add_node("data_output", data_output_node)

    # 3. 设置入口点
    graph.set_entry_point("data_plan")

    # 4. 添加边（plan 之后按灾种校验结果分流；其余顺序执行）
    graph.add_conditional_edges(
        "data_plan",
        route_after_plan,
        {
            "data_retrieve": "data_retrieve",  # 灾种合法 → 获取
            "data_output": "data_output",  # 灾种缺失/无法识别 → 直接输出（明确原因）
        },
    )
    graph.add_edge("data_retrieve", "data_clean")
    graph.add_edge("data_clean", "data_validate")

    # 5. 添加条件边（核心路由逻辑）
    graph.add_conditional_edges(
        "data_validate",  # 源节点
        route_after_validate,  # 路由函数
        {
            "data_retrieve": "data_retrieve",  # 不合格 → 重新获取（形成循环）
            "data_output": "data_output",  # 合格 → 输出
        },
    )

    # 6. 输出节点连接到 END
    graph.add_edge("data_output", END)

    # 7. 编译图
    compiled_graph = graph.compile()

    return compiled_graph


@lru_cache(maxsize=1)
def create_data_agent() -> CompiledStateGraph:
    """返回共享的已编译图：构建 + 编译只做一次。

    与 `risk_agent.create_risk_agent` 同一模式：LangGraph 的编译开销每次重建都要付
    （实测 p50 11.5ms 量级），mock/规则路径上它是主要成本；节点在执行期才取 LLM 工厂，
    缓存图不会锁死模型实例；LangGraph 的 `invoke` 每次新建运行态，同一编译图可被
    多线程并发调用。
    """
    return build_data_agent_graph()
