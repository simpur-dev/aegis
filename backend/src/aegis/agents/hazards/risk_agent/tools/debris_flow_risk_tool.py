"""
泥石流（debris_flow）因子配置与评估工具 —— ⚠️ MOCK-v0.1

【本文件角色】
  声明泥石流的因子三组与【按水源类型（暴雨/冰雪融水/溃决/泉水）路由的触发权重变体】
  （DZ/T 0438—2023 表E.3：四种触发水源机制不同，不用单一降雨评分处理所有情景），
  并调用 factor_engine 计算统一评估信封。溃决/泉水联动以保护规则通道单独命中。

【诚实声明 / 重要红线】
  1. 全部权重为项目原型自拟 Mock 测试权重；四型变体亦为演示假设；
  2. R 公式为项目原型自拟候选组合公式，非规范条款、非任务书公式，未经历史数据验证；
  3. 物源可动性细分、沟道水位等因子尚无数据字段，登记在 pending_factors。

【对外契约】
  debris_flow_factor_assessment_tool(events, region) -> 统一评估信封
"""

from aegis.agents.hazards.risk_agent.tools.factor_engine import (
    compute_factor_assessment,
    mock_weight_source,
)

RULE_VERSION = "MOCK-v0.1"
DISCLAIMER = "泥石流风险评估为 MOCK 玩具规则 + 原型候选公式，非官方/科学标准，禁止作为科学结论使用。"

FACTOR_CONFIG = {
    "hazard_type": "debris_flow",
    "hazard_type_cn": "泥石流",
    "rule_version": RULE_VERSION,
    "disclaimer": DISCLAIMER,
    "min_coverage": 0.6,
    "required_factors": ["rainfall_mm_h", "catchment_area_km2", "channel_gradient"],
    "groups": {
        # ---- 先天孕灾条件 P（沟道地形 + 物源，四型共用的"先天前提"）----
        "P": [
            {
                "key": "channel_gradient",
                "label": "沟道纵比降",
                "group": "P",
                "source_field": "channel_gradient",
                "unit": "",
                "agg": "mean",
                "normalize": {"method": "linear_pos", "L": 0.01, "U": 1.0},
                "weight": 0.4,
                "weight_source": mock_weight_source(),
                "data_status": "mock",
                "spec_reference": "DZ/T 0438—2023 6.4.3（转引）",
                "monitor": "DEM",
            },
            {
                "key": "catchment_area_km2",
                "label": "流域汇水面积（平根尺度）",
                "group": "P",
                "source_field": "catchment_area_km2",
                "unit": "km²",
                "agg": "mean",
                "normalize": {"method": "sqrt_pos", "U": 500.0},
                "weight": 0.3,
                "weight_source": mock_weight_source(),
                "data_status": "mock",
                "spec_reference": "DZ/T 0438—2023 6.4.3（转引）",
                "monitor": "DEM",
            },
            {
                "key": "sediment_supply_index",
                "label": "物源补给指数",
                "group": "P",
                "source_field": "sediment_supply_index",
                "unit": "",
                "agg": "mean",
                "normalize": {"method": "linear_pos", "L": 0.0, "U": 1.0},
                "weight": 0.3,
                "weight_source": mock_weight_source(),
                "data_status": "mock",
                "spec_reference": "DZ/T 0438—2023 6.4.3（转引）",
                "monitor": "勘察（Mock）",
            },
        ],
        # ---- 动态触发因素 T（默认权重：暴雨型为主；四型变体见 route.variants）----
        "T": [
            {
                "key": "rainfall_mm_h",
                "label": "小时降雨量（暴雨型主触发）",
                "group": "T",
                "source_field": "rainfall_mm_h",
                "unit": "mm/h",
                "agg": "max",
                "normalize": {"method": "linear_pos", "L": 0.0, "U": 150.0},
                "weight": 0.8,
                "weight_source": mock_weight_source(),
                "data_status": "mock",
                "spec_reference": "DZ/T 0438—2023 表E.3（转引）",
                "monitor": "雨量计",
            },
            {
                "key": "snow_depth_cm",
                "label": "积雪厚度（融雪水源）",
                "group": "T",
                "source_field": "snow_depth_cm",
                "unit": "cm",
                "agg": "max",
                "normalize": {"method": "linear_pos", "L": 0.0, "U": 300.0},
                "weight": 0.2,
                "weight_source": mock_weight_source(),
                "data_status": "mock",
                "spec_reference": "DZ/T 0438—2023 表E.3（转引）",
                "monitor": "雪深计",
            },
        ],
    },
    # ---- 路由：水源类型决定触发权重变体（表E.3 四型触发机制不同）----
    "route": {
        "factor_key": "water_source_type",
        "variants": {
            "rainstorm": {"T": {"rainfall_mm_h": 1.0}},  # 暴雨型
            "snowmelt": {
                "T": {
                    "air_temp_c": 0.5,
                    "snow_depth_cm": 0.35,  # 冰雪融水型
                    "rainfall_mm_h": 0.15,
                }
            },
            "outburst": {"T": {"upstream_breach_flag": 1.0}},  # 溃决型（联动另发保护规则）
            "spring": {"T": {"spring_surge_flag": 1.0}},  # 泉水型
        },
    },
    # ---- 保护规则（命中即升级响应，不被综合指数稀释；均为 MOCK）----
    "protected_rules": [
        {
            "rule_id": "DF-OUTBURST-LINK",
            "description": "上游冰湖/堰塞湖/水库溃决联动放水（链式灾害）",
            "when": {"field": "upstream_breach_flag", "equals": "yes"},
            "level_suggest": "high",
            "source_reference": "DZ/T 0438—2023 表E.3 溃决型（转引）",
            "validation_status": "mock",
        },
        {
            "rule_id": "DF-SPRING-SURGE",
            "description": "地下水/泉水出流骤增",
            "when": {"field": "spring_surge_flag", "equals": "yes"},
            "level_suggest": "medium",
            "source_reference": "DZ/T 0438—2023 表E.3 泉水型（转引）",
            "validation_status": "mock",
        },
    ],
    "pending_factors": [
        {"factor": "松散物源可动性细分（粒径/含水量）", "group": "P", "reason": "勘察类因子，未数字化"},
        {"factor": "沟道水位", "group": "T", "reason": "无数据字段"},
        {"factor": "历史泥石流事件档案", "group": "T", "reason": "无数据来源"},
    ],
}


def debris_flow_factor_assessment_tool(observations: list, region: str = "unknown") -> dict:
    """泥石流因子评估（含水源类型路由的触发权重变体与联动保护规则）。"""
    return compute_factor_assessment(observations, region, FACTOR_CONFIG)
