"""
DataClean 节点（面向西藏五类山地地质灾害）

职责：
  - 从灾种数据注册表取必需字段与标准化精度
  - 调用清洗工具（去重 + 缺失标记）与标准化工具（按注册表精度）
  - 将处理后的数据写入 State

设计思想：
  清洗和标准化是两个独立步骤，合并在一个节点中顺序执行，减少图的复杂度。
  本节点不硬编码任何灾种字段——全部来自 hazard_data_registry。
"""

from typing import Any

from aegis.agents.hazards.data_agent.state import DataAgentState
from aegis.agents.hazards.data_agent.tools import data_clean_tool, data_normalize_tool
from aegis.agents.hazards.data_agent.tools.hazard_data_registry import get_hazard_data_requirements
from aegis.agents.hazards.hazard_registry import UnsupportedHazardError, normalize_hazard_type


def data_clean_node(state: DataAgentState) -> dict[str, Any]:
    """数据清洗与标准化节点（按灾种注册表驱动）。"""

    raw_data = state.get("raw_data", {})

    if not raw_data:
        return {
            "processed_data": {},
            "error_message": "无原始数据可供清洗",
            "next": "data_validate",
        }

    try:
        # 从注册表取本灾种的必需字段与标准化精度
        hazard_key = normalize_hazard_type(state.get("hazard_type", ""))
        req = get_hazard_data_requirements(hazard_key)

        # 步骤1: 清洗（去重 + 按必需字段标记缺失）
        cleaned_data = data_clean_tool(raw_data, required_fields=req["required_fields"])

        # 步骤2: 标准化（时间/坐标通用 + 观测字段按注册表精度）
        normalized_data = data_normalize_tool(cleaned_data, normalize_precision=req["normalize_precision_full"])

        return {
            "processed_data": normalized_data,
            "error_message": "",
            "next": "data_validate",
        }

    except UnsupportedHazardError as e:
        return {
            "processed_data": {},
            "error_message": f"灾种无法识别，无法组织清洗规则: {e}",
            "next": "data_validate",
        }
    except Exception as e:
        return {
            "processed_data": {},
            "error_message": f"数据清洗/标准化失败: {e!s}",
            "next": "data_validate",
        }
