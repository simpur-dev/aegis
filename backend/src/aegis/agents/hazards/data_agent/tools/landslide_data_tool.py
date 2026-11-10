"""
滑坡（landslide）Mock 数据构造工具

【本文件角色】
  滑坡灾种的数据源工具（Mock）。观测字段对齐 hazard_data_registry：
  降雨/坡度/土壤含水率 + 位移/地下水埋深/振动等级（docx 监测矩阵）。

【诚实声明 / 重要红线】
  ★ 本工具返回的是【Mock 合成数据】，不是任何真实滑坡监测数据。
    data_source 标记为 "mock"，is_mock 恒为 True。
    接入真实雨量站/InSAR/水位计数据时替换实现，保持返回结构契约不变。

【对外契约】
  返回结构（全项目统一数据源契约）：
    {"query_params", "total_count", "events", "data_source": "mock",
     "data_source_detail", "is_mock": True, "fetch_timestamp", "observation_note"}
  提供给：agents/data_agent/tools/hazard_data_registry.py（按灾种路由）
"""


def landslide_data_tool(query_params: dict) -> dict:
    """构造滑坡灾种 Mock 观测数据（西藏林芝地区示例点位）。"""

    # 3 条记录：1 条完整高值、1 条缺可选观测、1 条缺公共时间字段（测试清洗/校验）
    mock_events = [
        {
            "event_id": "LS2024001",
            "time": "2024-07-08T03:00:00Z",
            "latitude": 29.65,
            "longitude": 95.32,
            "region": "西藏林芝",
            "rainfall_mm_h": 85.0,
            "slope_angle_deg": 42.0,
            "soil_moisture_ratio": 0.78,
            "displacement_mm_day": 12.0,
            "groundwater_depth_m": 2.5,
            "vibration_level": "strong",
            "source": "mock_landslide",
        },
        {
            "event_id": "LS2024002",
            "time": "2024-07-08T15:00:00Z",
            "latitude": 29.71,
            "longitude": 95.4,
            "region": "西藏林芝",
            "rainfall_mm_h": 42.5,
            "slope_angle_deg": 36.0,
            "soil_moisture_ratio": 0.55,
            "displacement_mm_day": 3.5,
            "groundwater_depth_m": 6.0,
            "vibration_level": "weak",
            "source": "mock_landslide",
        },
        {
            "event_id": "LS2024003",
            "time": None,  # 模拟公共字段缺失
            "latitude": 29.58,
            "longitude": 95.21,
            "region": "西藏林芝",
            "rainfall_mm_h": 12.0,
            "slope_angle_deg": 28.0,
            "soil_moisture_ratio": 0.41,
            "displacement_mm_day": 0.8,
            "groundwater_depth_m": 12.0,
            "vibration_level": "none",
            "source": "mock_landslide",
        },
    ]

    return {
        "query_params": query_params,
        "total_count": len(mock_events),
        "events": mock_events,
        "data_source": "mock",
        "data_source_detail": "mock_landslide_constructor",
        "is_mock": True,
        "fetch_timestamp": "2024-07-09T00:00:00Z",
        "observation_note": "滑坡 Mock 观测：降雨/坡度/含水率/位移/地下水/振动均为合成值，非真实监测数据",
    }
