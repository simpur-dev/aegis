"""
Data Agent 灾种数据需求注册表（五类山地灾害，因子来源可追溯）

【本文件角色】
  五类灾害各自的数据需求声明中心。观测字段依据《5类灾害触发条件.docx》
  （其规范依据为 DZ/T 0438—2023《地质灾害风险调查评价规范（1:50000）》，转引）
  与其中的监测矩阵扩充：区分 numeric（数值）/ class（分类）两类字段。

【诚实声明 / 重要红线】
  1. valid_ranges 是"物理合理范围"（数据质量口径），【不是】灾害风险阈值；
     风险阈值属于 Risk Agent 的因子注册表。
  2. 五个数据源工具当前均为 Mock 构造器（is_mock=True），不是真实监测数据。
  3. 分类字段的 allowed 取值集合为演示用映射，分级依据待专业核实。
  4. 灾种标准键与别名来自根目录 hazard_registry.py；不包含地震。

【对外契约】
  get_hazard_data_requirements / get_data_source_tool / build_query_param_model /
  list_hazard_data_capabilities
  提供给：data_agent 五节点 / 通用数据工具 / 测试。
"""

from typing import Any

from pydantic import Field, create_model

from aegis.agents_ext.data_agent.tools.avalanche_data_tool import avalanche_data_tool
from aegis.agents_ext.data_agent.tools.debris_flow_data_tool import debris_flow_data_tool
from aegis.agents_ext.data_agent.tools.glacial_lake_data_tool import glacial_lake_data_tool
from aegis.agents_ext.data_agent.tools.landslide_data_tool import landslide_data_tool
from aegis.agents_ext.data_agent.tools.rockfall_data_tool import rockfall_data_tool
from aegis.agents_ext.hazard_registry import (
    DEBRIS_FLOW,
    GLACIAL_LAKE_OUTBURST,
    HAZARD_CN_NAME,
    ICE_ROCK_SNOW_AVALANCHE,
    LANDSLIDE,
    ROCKFALL,
    UnsupportedHazardError,
    normalize_hazard_type,
)

# 所有灾种观测记录共有的公共字段（标识/时间/空间），与灾种无关
COMMON_FIELDS = ["event_id", "time", "latitude", "longitude", "region"]
COMMON_REQUIRED_FIELDS = ["event_id", "time", "latitude", "longitude"]
COMMON_VALID_RANGES = {"latitude": (-90.0, 90.0), "longitude": (-180.0, 180.0)}
COMMON_NORMALIZE_PRECISION = {"latitude": 4, "longitude": 4}

# 依据来源的规范写法（转引链：DZ/T 0438—2023 ← 《5类灾害触发条件.docx》）
SPEC_DZT = "DZ/T 0438—2023《地质灾害风险调查评价规范(1:50000)》（转引自《5类灾害触发条件.docx》）"
MATRIX_REF = "《5类灾害触发条件.docx》监测矩阵"


def _num(desc, unit, lo, hi, monitor, spec=SPEC_DZT):
    """numeric 字段声明快捷构造。"""
    return {"desc": desc, "unit": unit, "type": "numeric", "range": (lo, hi), "monitor": monitor, "spec_reference": spec}


def _cls(desc, allowed, monitor, spec):
    """class 字段声明快捷构造（分类变量，归一化映射在 Risk 因子注册表）。"""
    return {"desc": desc, "unit": "", "type": "class", "allowed": list(allowed), "monitor": monitor, "spec_reference": spec}


# ============================================================
# 五类灾害的数据需求声明（观测字段按 docx 因子体系与监测矩阵扩充）
# ============================================================

HAZARD_DATA_REQUIREMENTS: dict[str, dict[str, Any]] = {
    LANDSLIDE: {
        "hazard_type": LANDSLIDE,
        "hazard_type_cn": HAZARD_CN_NAME[LANDSLIDE],
        "mechanism_desc": "降雨入渗抬升孔隙水压力、软化结构面，高陡松散斜坡失稳滑动",
        "observation_fields": {
            "rainfall_mm_h": _num("小时降雨量（主触发）", "mm/h", 0.0, 300.0, "雨量计"),
            "slope_angle_deg": _num("坡度角", "°", 5.0, 80.0, "DEM/勘察"),
            "soil_moisture_ratio": _num("土壤含水率（体积比）", "", 0.0, 1.0, "土壤传感器"),
            "displacement_mm_day": _num("地表位移速率", "mm/d", 0.0, 500.0, "位移计/裂缝计", spec=MATRIX_REF),
            "groundwater_depth_m": _num("地下水埋深（越浅越危险）", "m", 0.0, 50.0, "水位/渗压计", spec=MATRIX_REF),
            "vibration_level": _cls("振动强度等级", ["none", "weak", "strong"], "振动/地震仪", MATRIX_REF),
        },
        "required_observation_fields": ["rainfall_mm_h", "slope_angle_deg"],
        "normalize_precision": {
            "rainfall_mm_h": 1,
            "slope_angle_deg": 1,
            "soil_moisture_ratio": 2,
            "displacement_mm_day": 1,
            "groundwater_depth_m": 1,
        },
        "data_source_tool": landslide_data_tool,
        "query_param_menu": [
            {"name": "region", "type": "str", "default": "西藏", "description": "查询区域，如 西藏/林芝"},
            {"name": "time_range", "type": "str", "default": "24h", "description": "时间范围，只允许 24h / 7d / 30d"},
            {
                "name": "min_rainfall_mm_h",
                "type": "float",
                "default": 0.0,
                "range": (0.0, 300.0),
                "description": "筛选阈值：最小小时降雨量(mm/h)",
            },
        ],
        "missing_capabilities": [
            "未接入真实雨量站/气象局降雨数据（当前为 Mock 构造）",
            "无 InSAR/GNSS 地表位移观测（位移字段为 Mock）",
            "无钻孔地下水位监测（埋深字段为 Mock）",
            "顺层斜坡、软弱结构面、岩体破碎等勘察类因子未数字化（pending）",
        ],
    },
    ROCKFALL: {
        "hazard_type": ROCKFALL,
        "hazard_type_cn": HAZARD_CN_NAME[ROCKFALL],
        "mechanism_desc": "高陡坡体危岩按滑移/倾倒/坠落三种破坏方式失稳（DZ/T 0438—2023 表E.2）",
        "observation_fields": {
            "slope_height_m": _num("坡高", "m", 2.0, 500.0, "DEM/勘察"),
            "slope_angle_deg": _num("坡度角", "°", 30.0, 90.0, "DEM/勘察"),
            "block_volume_m3": _num("危岩单体体积", "m³", 0.01, 100.0, "勘察"),
            "rockfall_history_count": _num("近一年落石次数", "次", 0, 100, "巡检台账"),
            "rainfall_mm_h": _num("小时降雨量（裂隙入渗增重）", "mm/h", 0.0, 300.0, "雨量计", spec=SPEC_DZT),
            "crack_width_mm": _num("裂缝开度", "mm", 0.0, 200.0, "裂缝计", spec=MATRIX_REF),
            "vibration_level": _cls("振动强度等级", ["none", "weak", "strong"], "振动/地震仪", spec=MATRIX_REF),
            "failure_mode": _cls("破坏方式（路由因子）", ["sliding", "toppling", "falling"], "勘察", SPEC_DZT),
        },
        "required_observation_fields": ["slope_height_m", "block_volume_m3"],
        "normalize_precision": {
            "slope_height_m": 1,
            "slope_angle_deg": 1,
            "block_volume_m3": 2,
            "rockfall_history_count": 0,
            "rainfall_mm_h": 1,
            "crack_width_mm": 1,
        },
        "data_source_tool": rockfall_data_tool,
        "query_param_menu": [
            {"name": "region", "type": "str", "default": "西藏", "description": "查询区域，如 西藏/昌都"},
            {"name": "time_range", "type": "str", "default": "24h", "description": "时间范围，只允许 24h / 7d / 30d"},
            {
                "name": "min_block_volume_m3",
                "type": "float",
                "default": 0.01,
                "range": (0.01, 100.0),
                "description": "筛选阈值：最小危岩单体体积(m³)",
            },
        ],
        "missing_capabilities": [
            "未接入危岩专业巡检数据库（当前为 Mock 构造）",
            "无三维激光扫描/摄影测量点云数据",
            "结构面产状、风化程度等勘察因子未数字化（pending）",
        ],
    },
    DEBRIS_FLOW: {
        "hazard_type": DEBRIS_FLOW,
        "hazard_type_cn": HAZARD_CN_NAME[DEBRIS_FLOW],
        "mechanism_desc": "按水源类型分暴雨/冰雪融水/溃决/泉水四型（DZ/T 0438—2023 表E.3），大比降沟道 + 丰富松散物源为先天前提",
        "observation_fields": {
            "rainfall_mm_h": _num("小时降雨量（暴雨型主触发）", "mm/h", 0.0, 300.0, "雨量计"),
            "catchment_area_km2": _num("流域汇水面积", "km²", 0.01, 500.0, "DEM"),
            "channel_gradient": _num("沟道纵比降（无量纲）", "", 0.01, 1.0, "DEM"),
            "sediment_supply_index": _num("物源补给指数（0~1）", "", 0.0, 1.0, "勘察（Mock）"),
            "water_source_type": _cls("水源类型（路由因子）", ["rainstorm", "snowmelt", "outburst", "spring"], "分类判定", SPEC_DZT),
            "air_temp_c": _num("气温（融雪型）", "℃", -40.0, 20.0, "温度传感器", spec=MATRIX_REF),
            "snow_depth_cm": _num("积雪厚度（融雪型）", "cm", 0.0, 300.0, "雪深计", spec=MATRIX_REF),
            "upstream_breach_flag": _cls("上游冰湖/堰塞湖溃决联动", ["no", "yes"], "水位计/联动判定", SPEC_DZT),
            "spring_surge_flag": _cls("泉水出流骤增", ["no", "yes"], "渗压计", SPEC_DZT),
        },
        "required_observation_fields": ["rainfall_mm_h", "catchment_area_km2", "channel_gradient"],
        "normalize_precision": {
            "rainfall_mm_h": 1,
            "catchment_area_km2": 2,
            "channel_gradient": 3,
            "sediment_supply_index": 2,
            "air_temp_c": 1,
            "snow_depth_cm": 1,
        },
        "data_source_tool": debris_flow_data_tool,
        "query_param_menu": [
            {"name": "region", "type": "str", "default": "西藏", "description": "查询区域，如 西藏/波密"},
            {"name": "time_range", "type": "str", "default": "24h", "description": "时间范围，只允许 24h / 7d / 30d"},
            {
                "name": "min_rainfall_mm_h",
                "type": "float",
                "default": 0.0,
                "range": (0.0, 300.0),
                "description": "筛选阈值：最小小时降雨量(mm/h)",
            },
        ],
        "missing_capabilities": [
            "未接入水文站/雨量站真实数据（当前为 Mock 构造）",
            "沟道物源储量详查缺失",
            "历史泥石流事件档案缺失",
        ],
    },
    ICE_ROCK_SNOW_AVALANCHE: {
        "hazard_type": ICE_ROCK_SNOW_AVALANCHE,
        "hazard_type_cn": HAZARD_CN_NAME[ICE_ROCK_SNOW_AVALANCHE],
        "mechanism_desc": "冰雪/冰岩体在新增降雪、升温等触发下沿陡坡高速下滑；"
        "坡度 30°~45°、海拔 4500~5500m、北坡为文献候选指标（藏东南，转引）",
        "observation_fields": {
            "release_volume_m3": _num("释放体量", "m³", 10.0, 1000000.0, "估算"),
            "vertical_drop_m": _num("垂直落差", "m", 50.0, 3000.0, "DEM"),
            "slope_angle_deg": _num("坡度角（文献候选 30~45° 最险）", "°", 20.0, 60.0, "DEM", spec="藏东南文献（转引）+物理范围"),
            "air_temp_c": _num("气温", "℃", -40.0, 20.0, "温度传感器"),
            "elevation_m": _num("海拔（文献候选 4500~5500m 雪线附近）", "m", 3000.0, 6000.0, "DEM", spec="藏东南文献（转引）"),
            "aspect": _cls("坡向（文献'亲北性'候选）", ["N", "NE", "E", "SE", "S", "SW", "W", "NW"], "DEM", "藏东南文献（转引）"),
            "snow_depth_cm": _num("积雪厚度", "cm", 0.0, 300.0, "雪深计", spec=MATRIX_REF),
            "new_snow_24h_cm": _num("24小时新雪量（加载触发）", "cm", 0.0, 100.0, "雪深计", spec=MATRIX_REF),
        },
        "required_observation_fields": ["release_volume_m3", "vertical_drop_m"],
        "normalize_precision": {
            "release_volume_m3": 1,
            "vertical_drop_m": 1,
            "slope_angle_deg": 1,
            "air_temp_c": 1,
            "elevation_m": 1,
            "snow_depth_cm": 1,
            "new_snow_24h_cm": 1,
        },
        "data_source_tool": avalanche_data_tool,
        "query_param_menu": [
            {"name": "region", "type": "str", "default": "西藏", "description": "查询区域，如 西藏/聂拉木"},
            {"name": "time_range", "type": "str", "default": "24h", "description": "时间范围，只允许 24h / 7d / 30d"},
            {
                "name": "min_release_volume_m3",
                "type": "float",
                "default": 10.0,
                "range": (10.0, 1000000.0),
                "description": "筛选阈值：最小释放体量(m³)",
            },
        ],
        "missing_capabilities": [
            "未接入气象站气温/降雪实况数据（当前为 Mock 构造）",
            "无雪层剖面与弱层专业观测（弱层因子 pending）",
            "无历史雪崩编目数据库",
        ],
    },
    GLACIAL_LAKE_OUTBURST: {
        "hazard_type": GLACIAL_LAKE_OUTBURST,
        "hazard_type_cn": HAZARD_CN_NAME[GLACIAL_LAKE_OUTBURST],
        "mechanism_desc": "冰碛坝在冰崩涌浪、水位上涨、渗漏管涌等作用下溃决（DZ/T 0438—2023 6.3.8.2）；需区分溃决危险性与下游影响风险",
        "observation_fields": {
            "lake_volume_m3": _num("冰湖库容", "m³", 1000.0, 100000000.0, "遥感/实测"),
            "dam_height_m": _num("冰碛坝坝高", "m", 5.0, 300.0, "勘察"),
            "water_level_ratio": _num("水位/坝高比（0~1）", "", 0.0, 1.0, "水位计"),
            "downstream_distance_km": _num("下游关注点距离（影响条件）", "km", 0.1, 100.0, "DEM"),
            "glacier_activity": _cls("补给冰川活跃状态", ["active", "stable", "retreating"], "遥感", SPEC_DZT),
            "ice_avalanche_into_lake": _cls("冰崩/雪崩入湖（最典型扳机）", ["no", "yes"], "联动判定/振动仪", SPEC_DZT),
            "seepage_level": _cls("坝体渗漏/管涌等级", ["none", "mild", "severe"], "渗压计/巡检", SPEC_DZT),
            "landslide_into_lake": _cls("滑坡入湖", ["no", "yes"], "联动判定", SPEC_DZT),
        },
        "required_observation_fields": ["lake_volume_m3", "dam_height_m"],
        "normalize_precision": {"lake_volume_m3": 1, "dam_height_m": 1, "water_level_ratio": 2, "downstream_distance_km": 2},
        "data_source_tool": glacial_lake_data_tool,
        "query_param_menu": [
            {"name": "region", "type": "str", "default": "西藏", "description": "查询区域，如 西藏/定结"},
            {"name": "time_range", "type": "str", "default": "24h", "description": "时间范围，只允许 24h / 7d / 30d"},
            {
                "name": "min_lake_volume_m3",
                "type": "float",
                "default": 1000.0,
                "range": (1000.0, 100000000.0),
                "description": "筛选阈值：最小冰湖库容(m³)",
            },
        ],
        "missing_capabilities": [
            "未接入卫星遥感湖面动态监测（当前为 Mock 构造）",
            "无冰碛坝物质组成与渗透性勘察数据（坝体结构因子 pending）",
            "无下游河谷断面与承灾体分布数据",
        ],
    },
}


# ============================================================
# 查询函数
# ============================================================


def get_hazard_data_requirements(hazard_type: str) -> dict:
    """返回某灾种的数据需求声明副本（含合成的必需字段/范围/精度）。

    无效灾种 → raise UnsupportedHazardError（错误信息含支持清单）。
    """
    key = normalize_hazard_type(hazard_type)
    if not key or key not in HAZARD_DATA_REQUIREMENTS:
        raise UnsupportedHazardError(f"灾种'{hazard_type}'未提供或无法识别，Data Agent 无法组织数据需求。当前支持：{supported_text()}")
    req = dict(HAZARD_DATA_REQUIREMENTS[key])
    req["required_fields"] = list(COMMON_REQUIRED_FIELDS) + list(req["required_observation_fields"])

    ranges = dict(COMMON_VALID_RANGES)
    allowed = {}
    for name, spec in req["observation_fields"].items():
        if spec["type"] == "numeric":
            ranges[name] = spec["range"]
        else:
            allowed[name] = list(spec["allowed"])
    req["valid_ranges"] = ranges
    req["allowed_values"] = allowed

    precision = dict(COMMON_NORMALIZE_PRECISION)
    precision.update(req["normalize_precision"])
    req["normalize_precision_full"] = precision
    return req


def get_data_source_tool(hazard_type: str):
    """返回该灾种的数据源工具函数（当前为 Mock 构造器）。无效灾种抛异常。"""
    return get_hazard_data_requirements(hazard_type)["data_source_tool"]


def build_query_param_model(hazard_type: str):
    """按灾种查询参数菜单动态构建 pydantic 模型（data_plan 的 LLM 结构化输出用）。"""
    req = get_hazard_data_requirements(hazard_type)
    # 【并入主线时的改动】补注解：字段值有 float/int/str 三种类型，不标注会被推成
    # `tuple[type[float], Any]`，随后 `create_model(**fields)` 就过不了类型检查。
    fields: dict[str, tuple[Any, Any]] = {}
    for p in req["query_param_menu"]:
        desc = p["description"]
        if p["type"] == "float":
            lo, hi = p.get("range", (None, None))
            fields[p["name"]] = (float, Field(default=p["default"], ge=lo, le=hi, description=desc))
        elif p["type"] == "int":
            fields[p["name"]] = (int, Field(default=p["default"], description=desc))
        else:
            fields[p["name"]] = (str, Field(default=p["default"], description=desc))
    # 【并入主线时的改动】`create_model(**fields)` 的重载不接受 `dict[str, tuple[Any, Any]]`
    # 这种动态字段字典（pydantic 的静态签名表达不了"运行时才知道字段"），故精确豁免这一处。
    return create_model(  # type: ignore[call-overload]
        f"{req['hazard_type'].title().replace('_', '')}QueryParams", **fields
    )


def list_hazard_data_capabilities() -> list:
    """返回五灾种数据能力清单（含 Mock 标记与缺失能力）。"""
    out = []
    for key, req in HAZARD_DATA_REQUIREMENTS.items():
        out.append(
            {
                "hazard_type": key,
                "hazard_type_cn": req["hazard_type_cn"],
                "data_source": "mock",
                "is_mock": True,
                "observation_fields": list(req["observation_fields"].keys()),
                "missing_capabilities": list(req["missing_capabilities"]),
            }
        )
    return out


def supported_text() -> str:
    from aegis.agents_ext.hazard_registry import supported_hazards_text

    return supported_hazards_text()
