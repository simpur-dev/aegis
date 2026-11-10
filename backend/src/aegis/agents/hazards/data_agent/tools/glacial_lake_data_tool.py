"""
冰湖溃决（lake_outburst）Mock 数据构造工具

【本文件角色】
  冰湖溃决灾种的数据源工具（Mock）。观测字段对齐 hazard_data_registry：
  库容/坝高/水位比/下游距离 + 补给冰川状态/冰崩入湖/渗漏管涌/滑坡入湖
  （后四者为 DZ/T 0438—2023 6.3.8.2 的扳机/孕灾要素，供保护规则使用）。

【诚实声明 / 重要红线】
  ★ 本工具返回的是【Mock 合成数据】，不是任何真实遥感/勘察数据。
    data_source 标记为 "mock"，is_mock 恒为 True。

【对外契约】
  返回结构与 landslide_data_tool 完全一致（全项目统一数据源契约）。
  提供给：agents/data_agent/tools/hazard_data_registry.py（按灾种路由）
"""


def glacial_lake_data_tool(query_params: dict) -> dict:
    """构造冰湖溃决灾种 Mock 观测数据（西藏定结地区示例冰湖点位）。"""

    # 3 条记录：完整高值（含扳机命中）/ 缺可选观测 / 缺公共时间字段
    mock_events = [
        {
            "event_id": "GL2024001",
            "time": "2024-10-06T12:00:00Z",
            "latitude": 27.9,
            "longitude": 87.35,
            "region": "西藏定结",
            "lake_volume_m3": 25000000.0,
            "dam_height_m": 80.0,
            "water_level_ratio": 0.85,
            "downstream_distance_km": 12.0,
            "glacier_activity": "active",
            "ice_avalanche_into_lake": "yes",  # 冰崩入湖命中，供保护规则测试
            "seepage_level": "mild",
            "landslide_into_lake": "no",
            "source": "mock_glacial_lake",
        },
        {
            "event_id": "GL2024002",
            "time": "2024-10-06T18:00:00Z",
            "latitude": 27.95,
            "longitude": 87.4,
            "region": "西藏定结",
            "lake_volume_m3": 3200000.0,
            "dam_height_m": 35.0,
            "water_level_ratio": 0.6,
            "downstream_distance_km": 4.0,
            "glacier_activity": "stable",
            "ice_avalanche_into_lake": "no",
            "seepage_level": None,  # 模拟可选观测缺失
            "landslide_into_lake": "no",
            "source": "mock_glacial_lake",
        },
        {
            "event_id": "GL2024003",
            "time": None,  # 模拟公共字段缺失
            "latitude": 27.85,
            "longitude": 87.3,
            "region": "西藏定结",
            "lake_volume_m3": 800000.0,
            "dam_height_m": 18.0,
            "water_level_ratio": 0.4,
            "downstream_distance_km": 1.5,
            "glacier_activity": "retreating",
            "ice_avalanche_into_lake": "no",
            "seepage_level": "none",
            "landslide_into_lake": "no",
            "source": "mock_glacial_lake",
        },
    ]

    return {
        "query_params": query_params,
        "total_count": len(mock_events),
        "events": mock_events,
        "data_source": "mock",
        "data_source_detail": "mock_glacial_lake_constructor",
        "is_mock": True,
        "fetch_timestamp": "2024-10-07T00:00:00Z",
        "observation_note": "冰湖 Mock 观测：库容/坝高/水位/距离/冰川状态/扳机标志均为合成值，非真实遥感数据",
    }
