"""
Risk Agent 灾种风险规则注册表（因子化口径）

【本文件角色】
  五类山地灾害各自的风险评估要素声明中心：评估工具（因子引擎入口）、
  典型风险因素、必需因子、规则版本、实现状态、Mock 免责声明、当前局限。
  risk_assess_node 按【显式传入的 hazard_type】查本表路由评估逻辑，
  绝不默认调用某个灾种的工具，也不允许 LLM 猜测灾种。

【诚实声明 / 重要红线】
  1. 五灾种的因子清单/权重/规则【各自独立实现】（各 *_risk_tool.py 的 FACTOR_CONFIG），
     当前全部为 MOCK-v0.1；R 公式为项目原型自拟候选组合公式（非规范条款、非任务书公式）。
  2. 本项目不研究地震：注册表中不存在 earthquake。
  3. 灾种标准键与别名来自根目录 hazard_registry.py。

【对外契约】
  get_hazard_risk_profile(hazard_type) / list_hazard_risk_capabilities()
  提供给：risk_assess_node.py、risk_output_node.py、risk_agent/main.py、test_risk_agent.py
"""

from aegis.agents.hazards.hazard_registry import (
    DEBRIS_FLOW,
    GLACIAL_LAKE_OUTBURST,
    HAZARD_CN_NAME,
    ICE_ROCK_SNOW_AVALANCHE,
    LANDSLIDE,
    ROCKFALL,
    UnsupportedHazardError,
    normalize_hazard_type,
    supported_hazards_text,
)
from aegis.agents.hazards.risk_agent.tools.avalanche_risk_tool import (
    avalanche_factor_assessment_tool,
)
from aegis.agents.hazards.risk_agent.tools.debris_flow_risk_tool import (
    debris_flow_factor_assessment_tool,
)
from aegis.agents.hazards.risk_agent.tools.glacial_lake_risk_tool import (
    glacial_lake_factor_assessment_tool,
)
from aegis.agents.hazards.risk_agent.tools.landslide_risk_tool import (
    FACTOR_CONFIG as _LS_CONFIG,
)
from aegis.agents.hazards.risk_agent.tools.landslide_risk_tool import (
    landslide_factor_assessment_tool,
)
from aegis.agents.hazards.risk_agent.tools.rockfall_risk_tool import (
    rockfall_factor_assessment_tool,
)

HAZARD_RISK_PROFILES = {
    LANDSLIDE: {
        "hazard_type": LANDSLIDE,
        "hazard_type_cn": HAZARD_CN_NAME[LANDSLIDE],
        "mechanism_desc": "降雨入渗 + 高陡松散斜坡失稳（DZ/T 0438—2023 6.4.1，转引）",
        "risk_factors": ["持续或短时强降雨", "高陡边坡", "土壤含水率升高", "地表位移加速", "地下水埋深变浅"],
        "assessment_tool": landslide_factor_assessment_tool,
        "required_input_fields": list(_LS_CONFIG["required_factors"]),  # type: ignore[call-overload]  # type: ignore[call-overload]
        "rule_version": _LS_CONFIG["rule_version"],
        "implementation_status": "mock",
        "current_limitations": [
            "全部因子权重为项目原型自拟 Mock 测试权重（method=mock）",
            "R 公式为原型候选组合公式，未经本地历史灾害数据验证",
            "顺层斜坡/软弱结构面/岩体破碎等勘察因子未数字化（pending）",
        ],
        "sample_values": {
            "rainfall_mm_h": 85.0,
            "slope_angle_deg": 42.0,
            "soil_moisture_ratio": 0.78,
            "displacement_mm_day": 12.0,
            "groundwater_depth_m": 2.5,
            "vibration_level": "strong",
        },
    },
    ROCKFALL: {
        "hazard_type": ROCKFALL,
        "hazard_type_cn": HAZARD_CN_NAME[ROCKFALL],
        "mechanism_desc": "高陡坡体危岩按滑移/倾倒/坠落三型失稳（表E.2，转引）；触发权重按破坏方式路由变体",
        "risk_factors": ["高陡坡体", "危岩体发育", "裂隙扩张贯通", "降雨入渗增重", "振动扰动", "历史落石频繁"],
        "assessment_tool": rockfall_factor_assessment_tool,
        "required_input_fields": ["slope_height_m", "block_volume_m3"],
        "rule_version": "MOCK-v0.1",
        "implementation_status": "mock",
        "current_limitations": [
            "全部因子权重与三型变体权重为 Mock 测试假设",
            "R 公式为原型候选组合公式，未经历史数据验证",
            "软弱面/节理贯通/风化程度等勘察因子未数字化（pending）",
        ],
        "sample_values": {
            "slope_height_m": 180.0,
            "block_volume_m3": 6.5,
            "slope_angle_deg": 72.0,
            "rockfall_history_count": 12,
            "rainfall_mm_h": 30.0,
            "crack_width_mm": 45.0,
            "vibration_level": "weak",
            "failure_mode": "sliding",
        },
    },
    DEBRIS_FLOW: {
        "hazard_type": DEBRIS_FLOW,
        "hazard_type_cn": HAZARD_CN_NAME[DEBRIS_FLOW],
        "mechanism_desc": "按水源类型分暴雨/冰雪融水/溃决/泉水四型（表E.3，转引）；触发权重按水源类型路由变体，溃决/泉水联动走保护规则",
        "risk_factors": ["短时强降雨", "沟域物源丰富", "沟道纵比降大", "融雪快速升温", "上游溃决联动", "泉水出流骤增"],
        "assessment_tool": debris_flow_factor_assessment_tool,
        "required_input_fields": ["rainfall_mm_h", "catchment_area_km2", "channel_gradient"],
        "rule_version": "MOCK-v0.1",
        "implementation_status": "mock",
        "current_limitations": [
            "全部因子权重与四型变体权重为 Mock 测试假设",
            "R 公式为原型候选组合公式，未经历史数据验证",
            "未接入雨强-历时(I-D)临界雨量等专业判别方法",
        ],
        "sample_values": {
            "rainfall_mm_h": 65.0,
            "catchment_area_km2": 12.0,
            "channel_gradient": 0.35,
            "sediment_supply_index": 0.7,
            "water_source_type": "rainstorm",
            "air_temp_c": 18.0,
            "snow_depth_cm": 0.0,
            "upstream_breach_flag": "no",
            "spring_surge_flag": "no",
        },
    },
    ICE_ROCK_SNOW_AVALANCHE: {
        "hazard_type": ICE_ROCK_SNOW_AVALANCHE,
        "hazard_type_cn": HAZARD_CN_NAME[ICE_ROCK_SNOW_AVALANCHE],
        "mechanism_desc": "冰雪/冰岩体在新增降雪、升温下沿陡坡下滑；坡度/海拔/坡向采用藏东南文献候选参数（pending_review）",
        "risk_factors": ["新增降雪加载", "快速升温层间润滑", "30°~45° 陡坡（文献候选）", "雪线附近高海拔（文献候选）", "北坡（文献候选）"],
        "assessment_tool": avalanche_factor_assessment_tool,
        "required_input_fields": ["slope_angle_deg", "snow_depth_cm"],
        "rule_version": "MOCK-v0.1",
        "implementation_status": "mock",
        "current_limitations": [
            "触发权重为 Mock 测试假设；坡度/海拔/坡向文献候选参数未核实适用范围",
            "R 公式为原型候选组合公式，未经历史数据验证",
            "雪层弱面/风成雪板/冰崩冲击等因子未数字化（pending）",
        ],
        "sample_values": {
            "release_volume_m3": 120000.0,
            "vertical_drop_m": 900.0,
            "slope_angle_deg": 38.0,
            "air_temp_c": 2.0,
            "elevation_m": 4800.0,
            "aspect": "N",
            "snow_depth_cm": 120.0,
            "new_snow_24h_cm": 45.0,
        },
    },
    GLACIAL_LAKE_OUTBURST: {
        "hazard_type": GLACIAL_LAKE_OUTBURST,
        "hazard_type_cn": HAZARD_CN_NAME[GLACIAL_LAKE_OUTBURST],
        "mechanism_desc": "冰碛坝在冰崩涌浪、水位上涨、渗漏管涌下溃决（6.3.8.2，转引）；"
        "扳机要素同时走保护规则通道；下游影响（I 组）单独输出",
        "risk_factors": ["冰崩/雪崩入湖涌浪", "水位接近坝顶", "坝体渗漏管涌", "补给冰川活跃", "滑坡入湖"],
        "assessment_tool": glacial_lake_factor_assessment_tool,
        "required_input_fields": ["lake_volume_m3", "water_level_ratio"],
        "rule_version": "MOCK-v0.1",
        "implementation_status": "mock",
        "current_limitations": [
            "全部因子权重与分类映射为 Mock 测试假设",
            "R 公式为原型候选组合公式，未经历史数据验证",
            "冰碛坝结构/自由干舷/下游承灾体等因子未数字化（pending）",
        ],
        "sample_values": {
            "lake_volume_m3": 25000000.0,
            "dam_height_m": 80.0,
            "water_level_ratio": 0.85,
            "downstream_distance_km": 12.0,
            "glacier_activity": "active",
            "ice_avalanche_into_lake": "yes",
            "seepage_level": "mild",
            "landslide_into_lake": "no",
        },
    },
}


def get_hazard_risk_profile(hazard_type: str) -> dict:
    """返回某灾种的风险评估要素声明副本。无效灾种抛 UnsupportedHazardError。"""
    key = normalize_hazard_type(hazard_type)
    if not key or key not in HAZARD_RISK_PROFILES:
        raise UnsupportedHazardError(
            f"灾种'{hazard_type}'未提供或无法识别，Risk Agent 无法选择评估规则。当前支持：{supported_hazards_text()}"
        )
    return dict(HAZARD_RISK_PROFILES[key])


def list_hazard_risk_capabilities() -> list:
    """返回五灾种风险评估能力清单（含实现状态与局限）。"""
    out = []
    for key, profile in HAZARD_RISK_PROFILES.items():
        out.append(
            {
                "hazard_type": key,
                "hazard_type_cn": profile["hazard_type_cn"],
                "implementation_status": profile["implementation_status"],
                "rule_version": profile["rule_version"],
                "required_input_fields": list(profile["required_input_fields"]),
                "current_limitations": list(profile["current_limitations"]),
            }
        )
    return out


def sample_observations(hazard_type: str, *, observed_at: str = "2024-07-08T03:00:00.000Z") -> list[dict]:
    """把注册表 `sample_values` 转成 `FactorObservation` dump（自测/演示专用，不是真实观测）。"""
    profile = get_hazard_risk_profile(hazard_type)
    rows: list[dict] = []
    for factor, value in profile.get("sample_values", {}).items():
        row: dict = {
            "factor": factor,
            "samples": 2,
            "observed_at": observed_at,
            "sources": ["registry_sample"],
            "derivation": "sample_values",
        }
        if isinstance(value, str):
            row["class_value"] = value
        else:
            row.update(min_value=float(value), max_value=float(value), mean_value=float(value))
        rows.append(row)
    return rows
