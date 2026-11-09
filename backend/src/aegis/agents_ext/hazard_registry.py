"""
项目公共灾种注册表（全项目唯一的灾种定义来源）

【本文件角色】
  定义"西藏山地灾害多智能体协同调控系统"支持的五类典型山地地质灾害：
  标准键名、中文名称、中文/英文别名与归一化函数。
  Data Agent、Risk Agent、Simulation Agent 全部从本模块取灾种定义，
  【禁止】任何 Agent 自行维护另一份别名表（避免映射漂移）。

【诚实声明 / 重要红线】
  1. 本项目研究五类山地地质灾害，【不研究地震灾害】：
     本注册表不包含、也不允许添加 earthquake（地震）灾种。
  2. 灾种必须由调用方【显式传入】hazard_type 决定；
     允许把明确的中文名称归一化为标准键；
     不允许 LLM 猜测灾种后直接选择规则/模型；
     未提供或无法识别时，调用方必须返回明确状态与原因，不得擅自换灾种。

【对外契约】
  常量：LANDSLIDE / ROCKFALL / DEBRIS_FLOW / ICE_ROCK_SNOW_AVALANCHE /
        GLACIAL_LAKE_OUTBURST / SUPPORTED_HAZARDS / HAZARD_CN_NAME / HAZARD_ALIAS
  函数：normalize_hazard_type / is_supported_hazard / require_hazard /
        supported_hazards_text / list_supported_hazards
  异常：UnsupportedHazardError
  提供给：三个 Agent（data / risk / simulation）及其工具、节点、测试。
"""

from aegis.domain.enums import HazardType

# ============================================================
# 五类灾种标准键（字符串常量，不用 Enum：全项目 State 均为字符串，
# 常量可直接参与 f-string / JSON 序列化 / == 比较，风格零摩擦）
#
# 【键名与平台一致，不再另立一套】键值直接取 `aegis.domain.enums.HazardType` 的
# 枚举值：外部实现与平台共用同一个 `assess.v1` 契约，灾种键若两套名字，
# 每次过契约都要翻译一次——那正是被删掉的适配层在做的事。
# 旧交付里的 `ice_rock_snow_avalanche` / `glacial_lake_outburst` 只在别名表里保留
# （兼容外部调用方），不再是标准键。
# ============================================================

LANDSLIDE = HazardType.LANDSLIDE.value  # 滑坡
ROCKFALL = HazardType.ROCKFALL.value  # 崩塌危岩
DEBRIS_FLOW = HazardType.DEBRIS_FLOW.value  # 泥石流
ICE_ROCK_SNOW_AVALANCHE = HazardType.AVALANCHE.value  # 冰岩雪崩
GLACIAL_LAKE_OUTBURST = HazardType.LAKE_OUTBURST.value  # 冰湖溃决

# 支持的灾种（有序；【禁止】添加 earthquake 或任何第六类）
SUPPORTED_HAZARDS = [
    LANDSLIDE,
    ROCKFALL,
    DEBRIS_FLOW,
    ICE_ROCK_SNOW_AVALANCHE,
    GLACIAL_LAKE_OUTBURST,
]

# 中文名称表（键与 SUPPORTED_HAZARDS 一一对应）
HAZARD_CN_NAME = {
    LANDSLIDE: "滑坡",
    ROCKFALL: "崩塌危岩",
    DEBRIS_FLOW: "泥石流",
    ICE_ROCK_SNOW_AVALANCHE: "冰岩雪崩",
    GLACIAL_LAKE_OUTBURST: "冰湖溃决",
}

# 别名归一化表：中文灾种名 / 英文变体 / 旧交付键 → 规范键。
# 原则"宁缺毋滥"：歧义词不收录（如"岩崩"），避免误路由。
HAZARD_ALIAS = {
    "滑坡": LANDSLIDE,
    "山体滑坡": LANDSLIDE,
    "landslide": LANDSLIDE,
    "landslides": LANDSLIDE,
    "崩塌": ROCKFALL,
    "危岩": ROCKFALL,
    "落石": ROCKFALL,
    "rockfall": ROCKFALL,
    "rock_fall": ROCKFALL,
    "泥石流": DEBRIS_FLOW,
    "debris_flow": DEBRIS_FLOW,
    "debrisflow": DEBRIS_FLOW,
    "mudflow": DEBRIS_FLOW,
    "冰岩雪崩": ICE_ROCK_SNOW_AVALANCHE,
    "雪崩": ICE_ROCK_SNOW_AVALANCHE,
    "冰岩崩": ICE_ROCK_SNOW_AVALANCHE,
    "ice_rock_snow_avalanche": ICE_ROCK_SNOW_AVALANCHE,  # 旧交付键，兼容外部调用方
    "avalanche": ICE_ROCK_SNOW_AVALANCHE,
    "snow_avalanche": ICE_ROCK_SNOW_AVALANCHE,
    "冰湖溃决": GLACIAL_LAKE_OUTBURST,
    "冰湖": GLACIAL_LAKE_OUTBURST,
    "溃决洪水": GLACIAL_LAKE_OUTBURST,
    "glacial_lake_outburst": GLACIAL_LAKE_OUTBURST,  # 旧交付键，兼容外部调用方
    "lake_outburst": GLACIAL_LAKE_OUTBURST,
    "glof": GLACIAL_LAKE_OUTBURST,
    "gflof": GLACIAL_LAKE_OUTBURST,
}


class UnsupportedHazardError(ValueError):
    """灾种未提供或无法识别。错误信息携带支持清单文本，由调用方透传给用户。"""


# ============================================================
# 归一化与查询函数
# ============================================================


def normalize_hazard_type(raw: str) -> str:
    """归一化灾种输入 → 标准键。

    步骤：strip → lower → 连字符/空格统一为下划线 → 标准键直通 → 别名表一跳。
    无法识别返回 ""（不抛异常；需要强校验的调用方用 require_hazard）。
    """
    if not raw or not isinstance(raw, str):
        return ""
    key = raw.strip().lower().replace("-", "_").replace(" ", "_")
    if key in SUPPORTED_HAZARDS:
        return key
    return HAZARD_ALIAS.get(key, "")


def is_supported_hazard(raw: str) -> bool:
    """归一化后是否为支持的灾种。空串 / 未知 → False。"""
    return normalize_hazard_type(raw) in SUPPORTED_HAZARDS


def require_hazard(raw: str) -> str:
    """归一化灾种，无效则抛 UnsupportedHazardError（错误信息含支持清单）。

    供需要"要么合法、要么明确报错"的节点/工具使用。
    """
    key = normalize_hazard_type(raw)
    if not key:
        raise UnsupportedHazardError(f"灾种'{raw}'未提供或无法识别。当前支持：{supported_hazards_text()}")
    return key


def supported_hazards_text() -> str:
    """支持灾种的可读文本（用于错误提示），如"滑坡(landslide)、泥石流(debris_flow)…"。"""
    return "、".join(f"{HAZARD_CN_NAME[k]}({k})" for k in SUPPORTED_HAZARDS)


def list_supported_hazards() -> list:
    """返回支持灾种基础清单：[{hazard_type, hazard_type_cn}]。

    各 Agent 如需携带自己的模型/工具版本信息，应在本清单基础上自行补充字段。
    """
    return [{"hazard_type": k, "hazard_type_cn": HAZARD_CN_NAME[k]} for k in SUPPORTED_HAZARDS]
