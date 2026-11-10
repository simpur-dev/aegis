"""
DataValidate 节点（面向西藏五类山地地质灾害）

职责：
  - 从灾种数据注册表取必需字段与物理合理范围
  - 调用数据质量校验工具，将质量报告写入 State
  - 设置路由标识（供 Conditional Edge 使用）

设计思想：
  此节点只负责"检查"并输出结果，路由决策由 Conditional Edge 完成。
  ★ valid_ranges 是数据质量的"物理合理范围"，不是灾害风险阈值。
"""

from typing import Any

from aegis.agents.hazards.data_agent.state import DataAgentState
from aegis.agents.hazards.data_agent.tools import data_validate_tool
from aegis.agents.hazards.data_agent.tools.hazard_data_registry import get_hazard_data_requirements
from aegis.agents.hazards.hazard_registry import UnsupportedHazardError, normalize_hazard_type


def data_validate_node(state: DataAgentState) -> dict[str, Any]:
    """数据质量校验节点（按灾种注册表驱动）。"""

    processed_data = state.get("processed_data", {})
    retry_count = state.get("retry_count", 0)

    if not processed_data:
        return {
            "data_quality": {"is_valid": False, "reason": "无数据"},
            "missing_data": ["所有数据缺失"],
            "retry_count": retry_count + 1,
            "next": "data_retrieve",
        }

    try:
        # 从注册表取本灾种的必需字段与物理合理范围
        hazard_key = normalize_hazard_type(state.get("hazard_type", ""))
        req = get_hazard_data_requirements(hazard_key)

        quality_report = data_validate_tool(
            processed_data,
            required_fields=req["required_fields"],
            valid_ranges=req["valid_ranges"],
            allowed_values=req.get("allowed_values", {}),
            # max_age_hours 未启用：Mock 数据时间戳固定（2024 年），开启会导致永远"过期"；
            # 接入真实数据源后按灾种配置过期阈值
        )

        # 提取缺失数据信息（仅校验失败时收集）
        missing_data = []
        if not quality_report.get("is_valid", False):
            missing_summary = quality_report.get("missing_fields_summary", {})
            for field, count in missing_summary.items():
                missing_data.append(f"{field}: 缺失 {count} 条")
            if quality_report.get("anomalies"):
                missing_data.append(f"异常值: {len(quality_report['anomalies'])} 处")

        is_valid = quality_report.get("is_valid", False)  # ★ 用 .get，避免键缺失时 KeyError
        next_step = "data_output" if is_valid else "data_retrieve"

        return {
            "data_quality": quality_report,
            "missing_data": missing_data,
            "retry_count": retry_count + 1 if not is_valid else retry_count,
            "next": next_step,
        }

    except UnsupportedHazardError as e:
        return {
            "data_quality": {"is_valid": False, "reason": str(e)},
            "missing_data": [f"灾种无法识别: {e}"],
            "retry_count": retry_count + 1,
            "next": "data_output",  # 灾种都无效，重试获取无意义
        }
    except Exception as e:
        return {
            "data_quality": {"is_valid": False, "reason": str(e)},
            "missing_data": [f"校验异常: {e!s}"],
            "retry_count": retry_count + 1,
            "next": "data_retrieve",
        }
