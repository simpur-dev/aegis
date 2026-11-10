"""
DataRetrieve 节点（面向西藏五类山地地质灾害）

职责：
  - 根据灾种从注册表路由到对应数据源工具（当前为五灾种 Mock 构造器）
  - 将原始数据写入 State

设计思想：
  此节点是"执行者"，不做判断。灾种 → 数据源工具的映射由
  hazard_data_registry 决定，本节点不再硬编码任何灾种工具。
"""

from typing import Any

from aegis.agents.hazards.data_agent.state import DataAgentState
from aegis.agents.hazards.data_agent.tools.hazard_data_registry import (
    get_data_source_tool,
)
from aegis.agents.hazards.hazard_registry import UnsupportedHazardError, normalize_hazard_type


def data_retrieve_node(state: DataAgentState) -> dict[str, Any]:
    """数据获取节点：按灾种路由数据源工具。"""

    data_requirement = state.get("data_requirement", {})
    query_params = data_requirement.get("query_params", {})
    hazard_key = normalize_hazard_type(state.get("hazard_type", ""))

    try:
        # 按灾种从注册表取数据源工具（当前为 Mock 构造器）
        source_tool = get_data_source_tool(hazard_key)
        raw_data = source_tool(query_params)

        return {
            "raw_data": raw_data,
            "error_message": "",
            "next": "data_clean",
        }

    except UnsupportedHazardError as e:
        # 防御分支：plan 节点已保证灾种合法；若上游被绕过则明确报错
        return {
            "raw_data": {},
            "error_message": f"灾种无法识别，无法路由数据源: {e}",
            "next": "data_output",
        }
    except Exception as e:
        return {
            "raw_data": {},
            "error_message": f"数据获取失败: {e!s}",
            "next": "data_output",
        }
