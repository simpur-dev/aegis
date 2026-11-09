"""
崩塌危岩（rockfall）因子配置与评估工具 —— ⚠️ MOCK-v0.1

【本文件角色】
  声明崩塌危岩的因子三组与【按破坏方式（滑移/倾倒/坠落）路由的触发权重变体】
  （DZ/T 0438—2023 表E.2：三型触发条件不同，不共用同一套权重），
  并调用 factor_engine 计算统一评估信封。

【诚实声明 / 重要红线】
  1. 全部权重为项目原型自拟 Mock 测试权重，三型变体亦为演示假设；
  2. R 公式为项目原型自拟候选组合公式，非规范条款、非任务书公式，未经历史数据验证；
  3. 软弱面/节理贯通、风化、冻融、坡脚掏蚀等因子尚无数据字段，登记在 pending_factors。

【对外契约】
  rockfall_factor_assessment_tool(events, region) -> 统一评估信封
"""

from aegis.agents_ext.risk_agent.tools.factor_engine import (
    compute_factor_assessment,
    mock_weight_source,
)

RULE_VERSION = "MOCK-v0.1"
DISCLAIMER = "崩塌危岩风险评估为 MOCK 玩具规则 + 原型候选公式，非官方/科学标准，禁止作为科学结论使用。"

FACTOR_CONFIG = {
    "hazard_type": "rockfall",
    "hazard_type_cn": "崩塌危岩",
    "rule_version": RULE_VERSION,
    "disclaimer": DISCLAIMER,
    "min_coverage": 0.6,
    "required_factors": ["slope_height_m", "block_volume_m3"],
    "groups": {
        # ---- 先天孕灾条件 P（三型共用几何/规模因子）----
        "P": [
            {
                "key": "slope_height_m",
                "label": "坡高",
                "group": "P",
                "source_field": "slope_height_m",
                "unit": "m",
                "agg": "mean",
                "normalize": {"method": "linear_pos", "L": 2.0, "U": 500.0},
                "weight": 0.4,
                "weight_source": mock_weight_source(),
                "data_status": "mock",
                "spec_reference": "DZ/T 0438—2023 6.4.2（转引）",
                "monitor": "DEM/勘察",
            },
            {
                "key": "block_volume_m3",
                "label": "危岩单体体积（对数尺度）",
                "group": "P",
                "source_field": "block_volume_m3",
                "unit": "m³",
                "agg": "mean",
                "normalize": {"method": "log_pos", "L": 0.01, "U": 100.0},
                "weight": 0.4,
                "weight_source": mock_weight_source(),
                "data_status": "mock",
                "spec_reference": "DZ/T 0438—2023 6.4.2（转引）",
                "monitor": "勘察",
            },
            {
                "key": "slope_angle_deg",
                "label": "坡度角",
                "group": "P",
                "source_field": "slope_angle_deg",
                "unit": "°",
                "agg": "mean",
                "normalize": {"method": "linear_pos", "L": 30.0, "U": 90.0},
                "weight": 0.2,
                "weight_source": mock_weight_source(),
                "data_status": "mock",
                "spec_reference": "DZ/T 0438—2023 6.4.2（转引）",
                "monitor": "DEM/勘察",
            },
        ],
        # ---- 动态触发因素 T（默认权重；三型变体见 route.variants）----
        "T": [
            {
                "key": "crack_width_mm",
                "label": "裂缝开度（裂隙扩张/贯通）",
                "group": "T",
                "source_field": "crack_width_mm",
                "unit": "mm",
                "agg": "max",
                "normalize": {"method": "linear_pos", "L": 0.0, "U": 200.0},
                "weight": 0.5,
                "weight_source": mock_weight_source(),
                "data_status": "mock",
                "spec_reference": "DZ/T 0438—2023 表E.2（转引）",
                "monitor": "裂缝计",
            },
            {
                "key": "rainfall_mm_h",
                "label": "小时降雨量（裂隙入渗增重）",
                "group": "T",
                "source_field": "rainfall_mm_h",
                "unit": "mm/h",
                "agg": "max",
                "normalize": {"method": "linear_pos", "L": 0.0, "U": 150.0},
                "weight": 0.3,
                "weight_source": mock_weight_source(),
                "data_status": "mock",
                "spec_reference": "DZ/T 0438—2023 表E.2（转引）",
                "monitor": "雨量计",
            },
            {
                "key": "vibration_level",
                "label": "振动强度（震/爆扰动）",
                "group": "T",
                "source_field": "vibration_level",
                "unit": "",
                "agg": "max",
                "type": "class",
                "normalize": {"method": "class_map", "class_map": {"none": 0.0, "weak": 0.4, "strong": 1.0}},
                "weight": 0.2,
                "weight_source": mock_weight_source(),
                "data_status": "mock",
                "spec_reference": "DZ/T 0438—2023 表E.2（转引）",
                "monitor": "振动/地震仪",
            },
        ],
    },
    # ---- 路由：破坏方式决定触发权重变体（表E.2 三型触发条件不同）----
    "route": {
        "factor_key": "failure_mode",
        "variants": {
            "sliding": {"T": {"crack_width_mm": 0.45, "rainfall_mm_h": 0.40, "vibration_level": 0.15}},  # 降雨灌入增重、软化软弱面为主
            "toppling": {"T": {"crack_width_mm": 0.40, "vibration_level": 0.35, "rainfall_mm_h": 0.25}},  # 根部临空、震/雨加速
            "falling": {"T": {"crack_width_mm": 0.40, "vibration_level": 0.30, "rainfall_mm_h": 0.30}},  # 贯通+风化+冻融楔裂
        },
    },
    "protected_rules": [],
    "pending_factors": [
        {"factor": "软弱面与节理裂隙贯通程度", "group": "P", "reason": "勘察类定性因子，未数字化"},
        {"factor": "岩体风化程度", "group": "P", "reason": "勘察类定性因子，未数字化"},
        {"factor": "冻融循环", "group": "T", "reason": "无数据字段"},
        {"factor": "坡脚掏蚀", "group": "T", "reason": "巡检类因子，无数据字段"},
    ],
}


def rockfall_factor_assessment_tool(events: list, region: str = "unknown") -> dict:
    """崩塌危岩因子评估（含破坏方式路由的触发权重变体）。"""
    return compute_factor_assessment(events, region, FACTOR_CONFIG)
