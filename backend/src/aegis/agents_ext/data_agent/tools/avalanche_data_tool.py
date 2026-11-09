"""
冰岩雪崩（ice_rock_snow_avalanche）Mock 数据构造工具

【本文件角色】
  冰岩雪崩灾种的数据源工具（Mock）。观测字段对齐 hazard_data_registry：
  释放体量/落差/坡度/气温 + 海拔/坡向（文献候选）/积雪厚度/24小时新雪（监测矩阵）。

【诚实声明 / 重要红线】
  ★ 本工具返回的是【Mock 合成数据】，不是任何真实气象/冰川监测数据。
    data_source 标记为 "mock"，is_mock 恒为 True。
    坡度 30°~45°、海拔 4500~5500m、北坡等文献候选指标仅作背景，不设强制阈值。

【对外契约】
  返回结构与 landslide_data_tool 完全一致（全项目统一数据源契约）。
  提供给：agents/data_agent/tools/hazard_data_registry.py（按灾种路由）
"""


def avalanche_data_tool(query_params: dict) -> dict:
    """构造冰岩雪崩灾种 Mock 观测数据（西藏聂拉木地区示例点位）。"""

    # 3 条记录：完整高值 / 缺可选观测 / 缺公共时间字段；坡向覆盖多值便于归一化测试
    mock_events = [
        {
            "event_id": "AV2024001",
            "time": "2024-12-15T07:00:00Z",
            "latitude": 28.18,
            "longitude": 86.02,
            "region": "西藏聂拉木",
            "release_volume_m3": 120000.0,
            "vertical_drop_m": 900.0,
            "slope_angle_deg": 38.0,
            "air_temp_c": 2.0,
            "elevation_m": 4800.0,
            "aspect": "N",
            "snow_depth_cm": 120.0,
            "new_snow_24h_cm": 45.0,
            "source": "mock_avalanche",
        },
        {
            "event_id": "AV2024002",
            "time": "2024-12-15T19:00:00Z",
            "latitude": 28.22,
            "longitude": 86.08,
            "region": "西藏聂拉木",
            "release_volume_m3": 8500.0,
            "vertical_drop_m": 420.0,
            "slope_angle_deg": 31.0,
            "air_temp_c": -3.0,
            "elevation_m": 5200.0,
            "aspect": "NE",
            "snow_depth_cm": 60.0,
            "new_snow_24h_cm": 10.0,
            "source": "mock_avalanche",
        },
        {
            "event_id": "AV2024003",
            "time": None,  # 模拟公共字段缺失
            "latitude": 28.15,
            "longitude": 85.98,
            "region": "西藏聂拉木",
            "release_volume_m3": 600.0,
            "vertical_drop_m": 180.0,
            "slope_angle_deg": 26.0,
            "air_temp_c": -12.0,
            "elevation_m": 5400.0,
            "aspect": "NW",
            "snow_depth_cm": 30.0,
            "new_snow_24h_cm": 2.0,
            "source": "mock_avalanche",
        },
    ]

    return {
        "query_params": query_params,
        "total_count": len(mock_events),
        "events": mock_events,
        "data_source": "mock",
        "data_source_detail": "mock_avalanche_constructor",
        "is_mock": True,
        "fetch_timestamp": "2024-12-16T00:00:00Z",
        "observation_note": "冰岩雪崩 Mock 观测：体量/落差/坡度/气温/海拔/坡向/雪深均为合成值，非真实监测数据",
    }
