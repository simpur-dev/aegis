"""
冰湖溃决（lake_outburst）因子配置与评估工具 —— ⚠️ MOCK-v0.1

【本文件角色】
  声明冰湖溃决的因子三组：孕灾 P（库容/水位比/坝高/冰川状态）、
  动态触发 T（冰崩入湖/渗漏管涌/滑坡入湖——均为 DZ/T 0438—2023 6.3.8.2 的扳机要素，
  以保护规则通道单独命中）、影响条件 I（下游距离，反向）。
  溃决危险性与下游影响风险分开输出，不混算。

【诚实声明 / 重要红线】
  1. 全部权重与 class_map 为项目原型自拟 Mock 测试参数；
  2. R 公式为项目原型自拟候选组合公式，非规范条款、非任务书公式，未经历史数据验证；
  3. 冰碛坝结构、湖面-坝顶高差等勘察因子尚无数据字段，登记在 pending_factors。

【对外契约】
  glacial_lake_factor_assessment_tool(events, region) -> 统一评估信封
"""

from aegis.agents.hazards.risk_agent.tools.factor_engine import (
    compute_factor_assessment,
    mock_weight_source,
)

RULE_VERSION = "MOCK-v0.1"
DISCLAIMER = "冰湖溃决风险评估为 MOCK 玩具规则 + 原型候选公式，非官方/科学标准，禁止作为科学结论使用。"

FACTOR_CONFIG = {
    "hazard_type": "lake_outburst",
    "hazard_type_cn": "冰湖溃决",
    "rule_version": RULE_VERSION,
    "disclaimer": DISCLAIMER,
    "min_coverage": 0.6,
    "required_factors": ["lake_volume_m3", "water_level_ratio"],
    "groups": {
        # ---- 先天孕灾条件 P ----
        "P": [
            {
                "key": "lake_volume_m3",
                "label": "冰湖库容（对数尺度）",
                "group": "P",
                "source_field": "lake_volume_m3",
                "unit": "m³",
                "agg": "mean",
                "normalize": {"method": "log_pos", "L": 1000.0, "U": 100000000.0},
                "weight": 0.35,
                "weight_source": mock_weight_source(),
                "data_status": "mock",
                "spec_reference": "DZ/T 0438—2023 6.3.8.2（转引）",
                "monitor": "遥感/实测",
            },
            {
                "key": "water_level_ratio",
                "label": "水位/坝高比（湖面距坝顶越近越危险）",
                "group": "P",
                "source_field": "water_level_ratio",
                "unit": "",
                "agg": "mean",
                "normalize": {"method": "linear_pos", "L": 0.0, "U": 1.0},
                "weight": 0.35,
                "weight_source": mock_weight_source(),
                "data_status": "mock",
                "spec_reference": "DZ/T 0438—2023 6.3.8.2（转引）",
                "monitor": "水位计",
            },
            {
                "key": "dam_height_m",
                "label": "冰碛坝坝高",
                "group": "P",
                "source_field": "dam_height_m",
                "unit": "m",
                "agg": "mean",
                "normalize": {"method": "linear_pos", "L": 5.0, "U": 300.0},
                "weight": 0.10,
                "weight_source": mock_weight_source(),
                "data_status": "mock",
                "spec_reference": "DZ/T 0438—2023 6.3.8.2（转引）",
                "monitor": "勘察",
            },
            {
                "key": "glacier_activity",
                "label": "补给冰川活跃状态",
                "group": "P",
                "source_field": "glacier_activity",
                "unit": "",
                "agg": "max",
                "type": "class",
                "normalize": {"method": "class_map", "class_map": {"active": 1.0, "stable": 0.5, "retreating": 0.2}},
                "weight": 0.20,
                "weight_source": mock_weight_source(),
                "data_status": "mock",
                "spec_reference": "DZ/T 0438—2023 6.3.8.2（转引）",
                "monitor": "遥感",
            },
        ],
        # ---- 动态触发因素 T（扳机要素；同时挂保护规则通道）----
        "T": [
            {
                "key": "ice_avalanche_into_lake",
                "label": "冰崩/雪崩入湖（最典型扳机）",
                "group": "T",
                "source_field": "ice_avalanche_into_lake",
                "unit": "",
                "agg": "max",
                "type": "class",
                "normalize": {"method": "class_map", "class_map": {"no": 0.0, "yes": 1.0}},
                "weight": 0.50,
                "weight_source": mock_weight_source(),
                "data_status": "mock",
                "spec_reference": "DZ/T 0438—2023 6.3.8.2（转引）",
                "monitor": "联动判定/振动仪",
            },
            {
                "key": "seepage_level",
                "label": "坝体渗漏/管涌等级",
                "group": "T",
                "source_field": "seepage_level",
                "unit": "",
                "agg": "max",
                "type": "class",
                "normalize": {"method": "class_map", "class_map": {"none": 0.0, "mild": 0.5, "severe": 1.0}},
                "weight": 0.30,
                "weight_source": mock_weight_source(),
                "data_status": "mock",
                "spec_reference": "DZ/T 0438—2023 6.3.8.2（转引）",
                "monitor": "渗压计/巡检",
            },
            {
                "key": "landslide_into_lake",
                "label": "滑坡入湖",
                "group": "T",
                "source_field": "landslide_into_lake",
                "unit": "",
                "agg": "max",
                "type": "class",
                "normalize": {"method": "class_map", "class_map": {"no": 0.0, "yes": 1.0}},
                "weight": 0.20,
                "weight_source": mock_weight_source(),
                "data_status": "mock",
                "spec_reference": "DZ/T 0438—2023 6.3.8.2（转引）",
                "monitor": "联动判定",
            },
        ],
        # ---- 影响条件 I（溃决洪水对下游的影响风险，单独输出，不并入 P/T）----
        "I": [
            {
                "key": "downstream_distance_km",
                "label": "下游关注点距离（越近影响越大，反向）",
                "group": "I",
                "source_field": "downstream_distance_km",
                "unit": "km",
                "agg": "mean",
                "normalize": {"method": "linear_neg", "L": 0.1, "U": 100.0},
                "weight": 1.0,
                "weight_source": mock_weight_source(),
                "data_status": "mock",
                "spec_reference": "DZ/T 0438—2023 6.3.8.2（转引）",
                "monitor": "DEM",
            },
        ],
    },
    "route": None,
    # ---- 保护规则（命中即升级响应；均为 MOCK）----
    "protected_rules": [
        {
            "rule_id": "GL-ICE-AVALANCHE-SURGE",
            "description": "冰崩/雪崩入湖引发涌浪漫坝或冲蚀坝体（最典型扳机）",
            "when": {"field": "ice_avalanche_into_lake", "equals": "yes"},
            "level_suggest": "high",
            "source_reference": "DZ/T 0438—2023 6.3.8.2（转引）",
            "validation_status": "mock",
        },
        {
            "rule_id": "GL-SEVERE-SEEPAGE",
            "description": "冰碛坝渗漏/管涌达到严重等级",
            "when": {"field": "seepage_level", "equals": "severe"},
            "level_suggest": "high",
            "source_reference": "DZ/T 0438—2023 6.3.8.2（转引）",
            "validation_status": "mock",
        },
        {
            "rule_id": "GL-LANDSLIDE-SURGE",
            "description": "滑坡入湖引发涌浪",
            "when": {"field": "landslide_into_lake", "equals": "yes"},
            "level_suggest": "medium",
            "source_reference": "DZ/T 0438—2023 6.3.8.2（转引）",
            "validation_status": "mock",
        },
    ],
    "pending_factors": [
        {"factor": "冰碛坝物质组成与结构（松散程度）", "group": "P", "reason": "勘察类因子，未数字化"},
        {"factor": "湖面与坝顶相对高差（自由干舷）", "group": "P", "reason": "可由坝高-水位推算，待接入"},
        {"factor": "持续升温+降水致水位快涨", "group": "T", "reason": "需真实水文序列，未接入"},
    ],
}


def glacial_lake_factor_assessment_tool(observations: list, region: str = "unknown") -> dict:
    """冰湖溃决因子评估（含扳机保护规则与下游影响因子）。"""
    return compute_factor_assessment(observations, region, FACTOR_CONFIG)
