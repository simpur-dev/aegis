"""
滑坡（landslide）因子配置与评估工具 —— ⚠️ MOCK-v0.1

【本文件角色】
  声明滑坡的因子三组（先天孕灾 P / 动态触发 T），权重与来源、必需因子，
  并调用 factor_engine 计算统一评估信封（P/T/R + 因子得分 + 覆盖率）。

【因子来源】
  因子候选与分类依据 DZ/T 0438—2023《地质灾害风险调查评价规范(1:50000)》
  6.4.1 / 表E.1（转引自《5类灾害触发条件.docx》）；
  观测字段来源见 data_agent 的 hazard_data_registry。

【诚实声明 / 重要红线】
  1. 全部权重为项目原型自拟的 Mock 测试权重（method=mock,
     validation_status=mock_assumption），不是专业结论；
  2. R 公式为项目原型自拟候选组合公式（见 factor_engine.R_FORMULA_NOTE），
     不是规范条款、不是课题任务书公式，未经历史数据验证；
  3. 顺层斜坡、软弱结构面、岩体破碎、坡脚掏蚀、后部加载等勘察/巡检类因子
     尚无数据字段，登记在 pending_factors，不参与计算、不伪装成已有输入。

【对外契约】
  landslide_factor_assessment_tool(events, region) -> 统一评估信封
  提供给：hazard_risk_registry.py（注册）→ risk_assess_node.py（按灾种路由）
"""

from aegis.agents_ext.risk_agent.tools.factor_engine import (
    compute_factor_assessment,
    mock_weight_source,
)

RULE_VERSION = "MOCK-v0.1"
DISCLAIMER = "滑坡风险评估为 MOCK 玩具规则 + 原型候选公式，非官方/科学标准，禁止作为科学结论使用。"

# ============================================================
# 因子配置（权重全部 Mock；归一化边界除标注外均为 Mock 物理范围）
# ============================================================

FACTOR_CONFIG = {
    "hazard_type": "landslide",
    "hazard_type_cn": "滑坡",
    "rule_version": RULE_VERSION,
    "disclaimer": DISCLAIMER,
    "min_coverage": 0.6,
    "required_factors": ["rainfall_mm_h", "slope_angle_deg"],
    "groups": {
        # ---- 先天孕灾条件 P（静态几何/水文条件）----
        "P": [
            {
                "key": "slope_angle_deg",
                "label": "坡度角",
                "group": "P",
                "source_field": "slope_angle_deg",
                "unit": "°",
                "agg": "mean",
                "normalize": {"method": "linear_pos", "L": 5.0, "U": 80.0},
                "weight": 0.5,
                "weight_source": mock_weight_source(),
                "data_status": "mock",
                "spec_reference": "DZ/T 0438—2023 6.4.1（转引）",
                "monitor": "DEM/勘察",
            },
            {
                "key": "groundwater_depth_m",
                "label": "地下水埋深（越浅越危险，反向）",
                "group": "P",
                "source_field": "groundwater_depth_m",
                "unit": "m",
                "agg": "mean",
                "normalize": {"method": "linear_neg", "L": 0.0, "U": 50.0},
                "weight": 0.5,
                "weight_source": mock_weight_source(),
                "data_status": "mock",
                "spec_reference": "DZ/T 0438—2023 6.4.1（转引）",
                "monitor": "水位/渗压计",
            },
        ],
        # ---- 动态触发因素 T（随时间变化）----
        "T": [
            {
                "key": "rainfall_mm_h",
                "label": "小时降雨量（最主要扳机）",
                "group": "T",
                "source_field": "rainfall_mm_h",
                "unit": "mm/h",
                "agg": "max",
                "normalize": {"method": "linear_pos", "L": 0.0, "U": 150.0},
                "weight": 0.45,
                "weight_source": mock_weight_source(),
                "data_status": "mock",
                "spec_reference": "DZ/T 0438—2023 6.4.1（转引）",
                "monitor": "雨量计",
            },
            {
                "key": "displacement_mm_day",
                "label": "地表位移速率",
                "group": "T",
                "source_field": "displacement_mm_day",
                "unit": "mm/d",
                "agg": "max",
                "normalize": {"method": "linear_pos", "L": 0.0, "U": 100.0},
                "weight": 0.30,
                "weight_source": mock_weight_source(),
                "data_status": "mock",
                "spec_reference": "监测矩阵",
                "monitor": "位移计/裂缝计",
            },
            {
                "key": "soil_moisture_ratio",
                "label": "土壤含水率",
                "group": "T",
                "source_field": "soil_moisture_ratio",
                "unit": "",
                "agg": "max",
                "normalize": {"method": "linear_pos", "L": 0.0, "U": 1.0},
                "weight": 0.15,
                "weight_source": mock_weight_source(),
                "data_status": "mock",
                "spec_reference": "监测矩阵",
                "monitor": "土壤传感器",
            },
            {
                "key": "vibration_level",
                "label": "振动强度（爆破/地震扰动）",
                "group": "T",
                "source_field": "vibration_level",
                "unit": "",
                "agg": "max",
                "type": "class",
                "normalize": {"method": "class_map", "class_map": {"none": 0.0, "weak": 0.4, "strong": 1.0}},
                "weight": 0.10,
                "weight_source": mock_weight_source(),
                "data_status": "mock",
                "spec_reference": "监测矩阵",
                "monitor": "振动/地震仪",
            },
        ],
    },
    "route": None,
    "protected_rules": [],
    # 尚无数据字段、暂不参与计算的候选因子（诚实申报，不伪装成输入）
    "pending_factors": [
        {"factor": "顺层斜坡（岩层倾向与坡向一致）", "group": "P", "reason": "勘察类定性因子，未数字化"},
        {"factor": "软弱结构面/软弱层", "group": "P", "reason": "勘察类定性因子，未数字化"},
        {"factor": "岩体破碎程度", "group": "P", "reason": "勘察类定性因子，未数字化"},
        {"factor": "坡脚掏蚀/人工开挖", "group": "T", "reason": "巡检类因子，无数据字段"},
        {"factor": "后部加载/水库蓄水", "group": "T", "reason": "巡检类因子，无数据字段"},
        {"factor": "冻融循环", "group": "T", "reason": "无数据字段"},
    ],
}


def landslide_factor_assessment_tool(observations: list, region: str = "unknown") -> dict:
    """滑坡因子评估：返回 P/T/R + 因子得分 + 覆盖率 + 规则命中的统一信封。"""
    return compute_factor_assessment(observations, region, FACTOR_CONFIG)
