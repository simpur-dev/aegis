"""
泥石流（debris_flow）Mock 数据构造工具

【本文件角色】
  泥石流灾种的数据源工具（Mock）。观测字段对齐 hazard_data_registry：
  降雨/流域/比降/物源 + 水源类型（表E.3 四型路由）/气温/积雪/溃决联动/泉水骤增。

【诚实声明 / 重要红线】
  ★ 本工具返回的是【Mock 合成数据】，不是任何真实水文/地质观测数据。
    data_source 标记为 "mock"，is_mock 恒为 True。

【对外契约】
  返回结构与 landslide_data_tool 完全一致（全项目统一数据源契约）。
  提供给：agents/data_agent/tools/hazard_data_registry.py（按灾种路由）
"""


def debris_flow_data_tool(query_params: dict) -> dict:
    """构造泥石流灾种 Mock 观测数据（西藏波密地区示例沟域点位）。"""

    # 3 条记录：完整高值 / 缺可选观测 / 缺公共时间字段；水源类型覆盖多型便于路由测试
    mock_events = [
        {
            "event_id": "DF2024001",
            "time": "2024-09-03T20:00:00Z",
            "latitude": 29.86,
            "longitude": 95.77,
            "region": "西藏波密",
            "rainfall_mm_h": 65.0,
            "catchment_area_km2": 12.0,
            "channel_gradient": 0.35,
            "sediment_supply_index": 0.7,
            "water_source_type": "rainstorm",
            "air_temp_c": 18.0,
            "snow_depth_cm": 0.0,
            "upstream_breach_flag": "no",
            "spring_surge_flag": "no",
            "source": "mock_debris_flow",
        },
        {
            "event_id": "DF2024002",
            "time": "2024-09-04T02:00:00Z",
            "latitude": 29.9,
            "longitude": 95.8,
            "region": "西藏波密",
            "rainfall_mm_h": 28.0,
            "catchment_area_km2": 4.5,
            "channel_gradient": 0.22,
            "sediment_supply_index": None,  # 模拟可选观测缺失
            "water_source_type": "snowmelt",
            "air_temp_c": 15.0,
            "snow_depth_cm": 22.0,
            "upstream_breach_flag": "no",
            "spring_surge_flag": "no",
            "source": "mock_debris_flow",
        },
        {
            "event_id": "DF2024003",
            "time": None,  # 模拟公共字段缺失
            "latitude": 29.82,
            "longitude": 95.7,
            "region": "西藏波密",
            "rainfall_mm_h": 9.0,
            "catchment_area_km2": 1.8,
            "channel_gradient": 0.12,
            "sediment_supply_index": 0.3,
            "water_source_type": "outburst",
            "air_temp_c": 6.0,
            "snow_depth_cm": 8.0,
            "upstream_breach_flag": "yes",  # 溃决联动命中，供保护规则测试
            "spring_surge_flag": "no",
            "source": "mock_debris_flow",
        },
    ]

    return {
        "query_params": query_params,
        "total_count": len(mock_events),
        "events": mock_events,
        "data_source": "mock",
        "data_source_detail": "mock_debris_flow_constructor",
        "is_mock": True,
        "fetch_timestamp": "2024-09-05T00:00:00Z",
        "observation_note": "泥石流 Mock 观测：降雨/流域/比降/物源/水源类型/联动标志均为合成值，非真实水文数据",
    }
