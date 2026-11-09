"""
数据标准化工具（灾种无关，精度规则由注册表驱动）

【本文件角色】
  - 统一时间格式（ISO 8601，与灾种无关）
  - 统一坐标精度（与灾种无关）
  - 按灾种注册表声明的精度，统一各观测字段的数值表示
  只做"格式转换"，不改变数据含义，不做任何风险判断。
  灾种专属字段不出现在本文件——字段清单来自 hazard_data_registry。

【对外契约】
  data_normalize_tool(cleaned_data, normalize_precision) ->
      {"events": [...], "cleaning_report": {...}, "normalization_applied": [...], "data_source": ...}
  提供给：data_clean_node.py
"""

from datetime import datetime


def data_normalize_tool(cleaned_data: dict, normalize_precision: dict) -> dict:
    """标准化观测数据格式（灾种无关）。

    参数:
        cleaned_data: data_clean_tool 返回的清洗后数据
        normalize_precision: {字段名: 小数位数}
                             （= 公共精度 + 灾种观测字段精度，来自 hazard_data_registry）

    返回:
        标准化后的数据 + 标准化说明清单
    """
    events = cleaned_data.get("events", [])
    normalized_events = []
    applied = ["time_iso8601", "coordinate_4dp"]

    for event in events:
        normalized = event.copy()

        # 1. 时间标准化：确保为 ISO 8601 格式（通用）
        time_str = normalized.get("time")
        if time_str:
            try:
                dt = datetime.fromisoformat(time_str.replace("Z", "+00:00"))
                normalized["time"] = dt.isoformat()
                normalized["time_valid"] = True
            except (ValueError, AttributeError):
                normalized["time_valid"] = False
        else:
            normalized["time_valid"] = False

        # 2. 坐标精度标准化：保留4位小数（通用）
        if normalized.get("latitude") is not None:
            normalized["latitude"] = round(float(normalized["latitude"]), 4)
        if normalized.get("longitude") is not None:
            normalized["longitude"] = round(float(normalized["longitude"]), 4)

        # 3. 观测字段精度标准化（按灾种注册表声明的精度逐字段处理）
        for field, digits in (normalize_precision or {}).items():
            if normalized.get(field) is not None:
                try:
                    value = float(normalized[field])
                    normalized[field] = round(value, int(digits)) if digits > 0 else value
                    applied.append(f"{field}_{int(digits)}dp")
                except (TypeError, ValueError):
                    # 非数值字段跳过，交由校验工具报告异常
                    pass

        normalized_events.append(normalized)

    result = {
        "events": normalized_events,
        "cleaning_report": cleaned_data.get("cleaning_report", {}),
        "normalization_applied": applied,
        "data_source": cleaned_data.get("data_source", "unknown"),
    }
    return result
