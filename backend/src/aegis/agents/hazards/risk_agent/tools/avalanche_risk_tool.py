"""
冰岩雪崩（avalanche）因子配置与评估工具 —— ⚠️ MOCK-v0.1

【本文件角色】
  声明冰岩雪崩的因子三组。坡度（30°~45° 最险带）、海拔（4500~5500m 雪线带）、
  坡向（亲北性）三因子采用藏东南文献候选参数（转引），validation_status=pending_review，
  【不是适用于所有区域的强制阈值】；触发权重为 Mock。释放体量/落差归入影响条件 I 组。

【诚实声明 / 重要红线】
  1. 触发权重为项目原型自拟 Mock 测试权重；文献候选带亦未核实适用范围；
  2. R 公式为项目原型自拟候选组合公式，非规范条款、非任务书公式，未经历史数据验证；
  3. 雪层薄弱界面、风成雪板、冰崩冲击等因子尚无数据字段，登记在 pending_factors。

【对外契约】
  avalanche_factor_assessment_tool(events, region) -> 统一评估信封
"""

from aegis.agents.hazards.risk_agent.tools.factor_engine import (
    compute_factor_assessment,
    literature_weight_source,
    mock_weight_source,
)

RULE_VERSION = "MOCK-v0.1"
DISCLAIMER = "冰岩雪崩风险评估为 MOCK 玩具规则 + 原型候选公式，非官方/科学标准，禁止作为科学结论使用。"

FACTOR_CONFIG = {
    "hazard_type": "avalanche",
    "hazard_type_cn": "冰岩雪崩",
    "rule_version": RULE_VERSION,
    "disclaimer": DISCLAIMER,
    "min_coverage": 0.6,
    "required_factors": ["slope_angle_deg", "snow_depth_cm"],
    "groups": {
        # ---- 先天孕灾条件 P（文献候选带 + 亲北性，均 pending_review）----
        "P": [
            {
                "key": "slope_angle_deg",
                "label": "坡度角（文献候选 30~45° 最险带）",
                "group": "P",
                "source_field": "slope_angle_deg",
                "unit": "°",
                "agg": "mean",
                "normalize": {"method": "band_pos", "L": 20.0, "band_lo": 30.0, "band_hi": 45.0, "U": 60.0},
                "weight": 0.5,
                "weight_source": literature_weight_source(),
                "data_status": "literature",
                "spec_reference": "DZ/T 0438—2023 6.3.8 + 藏东南文献（转引）",
                "monitor": "DEM",
            },
            {
                "key": "elevation_m",
                "label": "海拔（文献候选 4500~5500m 雪线附近）",
                "group": "P",
                "source_field": "elevation_m",
                "unit": "m",
                "agg": "mean",
                "normalize": {"method": "band_pos", "L": 3000.0, "band_lo": 4500.0, "band_hi": 5500.0, "U": 6000.0},
                "weight": 0.3,
                "weight_source": literature_weight_source(),
                "data_status": "literature",
                "spec_reference": "藏东南文献（转引）",
                "monitor": "DEM",
            },
            {
                "key": "aspect",
                "label": "坡向（文献'亲北性'候选）",
                "group": "P",
                "source_field": "aspect",
                "unit": "",
                "agg": "max",
                "type": "class",
                "normalize": {
                    "method": "class_map",
                    "class_map": {"N": 1.0, "NE": 0.8, "NW": 0.8, "E": 0.5, "W": 0.5, "SE": 0.3, "SW": 0.3, "S": 0.1},
                },
                "weight": 0.2,
                "weight_source": literature_weight_source(),
                "data_status": "literature",
                "spec_reference": "藏东南文献（转引）",
                "monitor": "DEM",
            },
        ],
        # ---- 动态触发因素 T ----
        "T": [
            {
                "key": "new_snow_24h_cm",
                "label": "24小时新雪量（加载触发）",
                "group": "T",
                "source_field": "new_snow_24h_cm",
                "unit": "cm",
                "agg": "max",
                "normalize": {"method": "linear_pos", "L": 0.0, "U": 50.0},
                "weight": 0.40,
                "weight_source": mock_weight_source(),
                "data_status": "mock",
                "spec_reference": "DZ/T 0438—2023 6.3.8（转引）",
                "monitor": "雪深计",
            },
            {
                "key": "air_temp_c",
                "label": "气温（升温层间润滑）",
                "group": "T",
                "source_field": "air_temp_c",
                "unit": "℃",
                "agg": "max",
                "normalize": {"method": "linear_pos", "L": -15.0, "U": 5.0},
                "weight": 0.35,
                "weight_source": mock_weight_source(),
                "data_status": "mock",
                "spec_reference": "DZ/T 0438—2023 6.3.8（转引）",
                "monitor": "温度传感器",
            },
            {
                "key": "snow_depth_cm",
                "label": "积雪厚度",
                "group": "T",
                "source_field": "snow_depth_cm",
                "unit": "cm",
                "agg": "max",
                "normalize": {"method": "linear_pos", "L": 0.0, "U": 300.0},
                "weight": 0.25,
                "weight_source": mock_weight_source(),
                "data_status": "mock",
                "spec_reference": "监测矩阵",
                "monitor": "雪深计",
            },
        ],
    },
    "route": None,
    "protected_rules": [],
    "pending_factors": [
        {"factor": "雪层薄弱界面（冰壳/新雪夹层）", "group": "P", "reason": "专业雪剖面观测，未数字化"},
        {"factor": "风成雪板破裂", "group": "T", "reason": "无数据字段"},
        {"factor": "冰崩坠落冲击触发", "group": "T", "reason": "联动判定，无数据字段"},
        {"factor": "外部振动", "group": "T", "reason": "无数据字段"},
    ],
}


def avalanche_factor_assessment_tool(observations: list, region: str = "unknown") -> dict:
    """冰岩雪崩因子评估（文献候选带 + Mock 触发权重）。"""
    return compute_factor_assessment(observations, region, FACTOR_CONFIG)
