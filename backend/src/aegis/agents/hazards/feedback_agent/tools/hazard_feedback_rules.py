"""
Feedback Agent 灾种差异化反馈检查规则（统一架构，按 hazard_type 选规则）

【本文件角色】
  为五类灾害分别声明"反馈检查关注项"：关注哪些观测字段是否得到合理处理、
  分类路由因子取值是否落在注册表允许的枚举内、数值是否落在模型适用范围内。

【诚实声明 / 重要红线】
  1. 本文件【不定义】任何新的权重、阈值或风险判据——
     字段清单 / 合法取值 / 物理合理范围 / 模型参数范围全部从既有注册表读取：
       agents/data_agent/tools/hazard_data_registry.py（观测字段声明）
       agents/simulation_agent/tools/hazard_model_router.py（模型参数菜单）
  2. "关注项"文本仅描述检查方向，检查依据仍是指定字段是否齐备/取值是否合法。
  3. 灾种标准键来自根目录 hazard_registry.py（本项目不研究地震）。

【对外契约】
  get_hazard_focus_checks(hazard_type) -> {focus_note, key_fields, class_fields, model_param_ranges}
  check_hazard_specific(hazard_type, events, ...) 由 feedback_check_tool 调用。
"""

from aegis.agents.hazards.hazard_registry import (
    DEBRIS_FLOW,
    GLACIAL_LAKE_OUTBURST,
    ICE_ROCK_SNOW_AVALANCHE,
    LANDSLIDE,
    ROCKFALL,
    normalize_hazard_type,
)

# 各灾种反馈检查关注项说明（只做"检查方向"描述，不引入新阈值；
# 依据：Data Agent 观测字段注册表 + 《5类灾害触发条件.docx》机制描述）
_FOCUS_NOTES = {
    LANDSLIDE: "关注降雨、坡度、土壤含水、地表位移(裂缝)、地下水、振动等触发因素是否在数据与因子评估中得到合理处理",
    ROCKFALL: "关注滑移/倾倒/坠落破坏方式路由是否匹配，坡高/危岩体积/裂隙/降雨/振动等数据是否完整合法",
    DEBRIS_FLOW: "关注沟道地形(汇水面积/纵比降)、松散物源、暴雨/冰雪融水/溃决/泉水四类触发路径(water_source_type)是否匹配完整",
    ICE_ROCK_SNOW_AVALANCHE: "关注积雪/新雪加载、气温回升、坡度/海拔/坡向是否与模型适用范围一致，释放体量与落差数据是否完整",
    GLACIAL_LAKE_OUTBURST: "关注冰湖库容/水位比、冰碛坝坝高与渗漏、冰崩/滑坡入湖扳机、下游影响距离情景是否完整",
}

# 各灾种"重点观测字段"（字段名必须是 hazard_data_registry 已声明的观测字段，
# 此处只做挑选，不新增字段）
_KEY_FIELDS = {
    LANDSLIDE: ["rainfall_mm_h", "slope_angle_deg", "soil_moisture_ratio", "displacement_mm_day", "groundwater_depth_m"],
    ROCKFALL: ["slope_height_m", "slope_angle_deg", "block_volume_m3", "crack_width_mm", "rainfall_mm_h", "failure_mode"],
    DEBRIS_FLOW: [
        "rainfall_mm_h",
        "catchment_area_km2",
        "channel_gradient",
        "sediment_supply_index",
        "water_source_type",
        "air_temp_c",
        "snow_depth_cm",
        "upstream_breach_flag",
    ],
    ICE_ROCK_SNOW_AVALANCHE: [
        "release_volume_m3",
        "vertical_drop_m",
        "slope_angle_deg",
        "air_temp_c",
        "elevation_m",
        "snow_depth_cm",
        "new_snow_24h_cm",
    ],
    GLACIAL_LAKE_OUTBURST: [
        "lake_volume_m3",
        "dam_height_m",
        "water_level_ratio",
        "downstream_distance_km",
        "seepage_level",
        "ice_avalanche_into_lake",
        "landslide_into_lake",
    ],
}


def get_hazard_focus_checks(hazard_type: str) -> dict:
    """返回该灾种的反馈检查关注项声明（从注册表取字段元数据，组装返回）。

    无效灾种返回空声明（调用方已在主检查器中先行拒绝无效灾种）。
    """
    key = normalize_hazard_type(hazard_type)
    if not key:
        return {"focus_note": "", "key_fields": [], "class_fields": {}, "model_param_ranges": {}}

    # 观测字段声明（desc/type/allowed/range）来自 Data Agent 灾种注册表
    from aegis.agents.hazards.data_agent.tools.hazard_data_registry import get_hazard_data_requirements

    req = get_hazard_data_requirements(key)
    obs_fields = req["observation_fields"]

    key_fields = [f for f in _KEY_FIELDS.get(key, []) if f in obs_fields]
    class_fields = {f: obs_fields[f]["allowed"] for f in key_fields if obs_fields.get(f, {}).get("type") == "class"}

    # 模型参数范围来自 Simulation Agent 模型路由（仅覆盖数值型、用于"模型适用范围"核验）
    model_param_ranges = {}
    try:
        from aegis.agents.hazards.simulation_agent.tools.hazard_model_router import get_model_requirements

        mreq = get_model_requirements(key)
        for p in mreq.get("param_menu", []):
            model_param_ranges[p["name"]] = p["range"]
    except Exception:
        pass

    return {
        "hazard_type": key,
        "focus_note": _FOCUS_NOTES.get(key, ""),
        "key_fields": key_fields,
        "class_fields": class_fields,  # 字段 → 合法枚举（注册表来源）
        "model_param_ranges": model_param_ranges,  # 参数 → [lo, hi]（模型菜单来源）
        "required_observation_fields": list(req["required_observation_fields"]),
        "valid_ranges": dict(req["valid_ranges"]),  # 物理合理范围（数据质量口径，非风险阈值）
        "field_desc": {f: obs_fields[f]["desc"] for f in key_fields if f in obs_fields},
    }
