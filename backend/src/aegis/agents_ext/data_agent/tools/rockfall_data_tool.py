"""
崩塌危岩（rockfall）Mock 数据构造工具

【本文件角色】
  崩塌危岩灾种的数据源工具（Mock）。观测字段对齐 hazard_data_registry：
  坡高/坡角/危岩体积/历史落石 + 降雨（裂隙入渗）/裂缝开度/振动/破坏方式（表E.2 三型路由）。

【诚实声明 / 重要红线】
  ★ 本工具返回的是【Mock 合成数据】，不是任何真实危岩勘察数据。
    data_source 标记为 "mock"，is_mock 恒为 True。

【对外契约】
  返回结构与 landslide_data_tool 完全一致（全项目统一数据源契约）。
  提供给：agents/data_agent/tools/hazard_data_registry.py（按灾种路由）
"""


def rockfall_data_tool(query_params: dict) -> dict:
    """构造崩塌危岩灾种 Mock 观测数据（西藏昌都地区示例点位）。"""

    # 3 条记录：完整高值 / 缺可选观测 / 缺公共时间字段；破坏方式覆盖三型便于路由测试
    mock_events = [
        {
            "event_id": "RF2024001",
            "time": "2024-08-12T09:30:00Z",
            "latitude": 31.14,
            "longitude": 97.18,
            "region": "西藏昌都",
            "slope_height_m": 180.0,
            "slope_angle_deg": 72.0,
            "block_volume_m3": 6.5,
            "rockfall_history_count": 12,
            "rainfall_mm_h": 30.0,
            "crack_width_mm": 45.0,
            "vibration_level": "weak",
            "failure_mode": "sliding",
            "source": "mock_rockfall",
        },
        {
            "event_id": "RF2024002",
            "time": "2024-08-12T14:00:00Z",
            "latitude": 31.2,
            "longitude": 97.25,
            "region": "西藏昌都",
            "slope_height_m": 95.0,
            "slope_angle_deg": 63.0,
            "block_volume_m3": 1.2,
            "rockfall_history_count": 4,
            "rainfall_mm_h": 5.0,
            "crack_width_mm": 12.0,
            "vibration_level": "none",
            "failure_mode": "toppling",
            "source": "mock_rockfall",
        },
        {
            "event_id": "RF2024003",
            "time": None,  # 模拟公共字段缺失
            "latitude": 31.08,
            "longitude": 97.1,
            "region": "西藏昌都",
            "slope_height_m": 45.0,
            "slope_angle_deg": 55.0,
            "block_volume_m3": 0.3,
            "rockfall_history_count": 2,
            "rainfall_mm_h": 0.0,
            "crack_width_mm": 2.0,
            "vibration_level": "none",
            "failure_mode": "falling",
            "source": "mock_rockfall",
        },
    ]

    return {
        "query_params": query_params,
        "total_count": len(mock_events),
        "events": mock_events,
        "data_source": "mock",
        "data_source_detail": "mock_rockfall_constructor",
        "is_mock": True,
        "fetch_timestamp": "2024-08-13T00:00:00Z",
        "observation_note": "崩塌 Mock 观测：几何/裂隙/降雨/振动/破坏方式均为合成值，非真实勘察数据",
    }
