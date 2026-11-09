"""
Warning Agent 灾种预警规则注册表

【本文件角色】
  五类灾害各自的预警等级映射规则：由 Risk Agent 的结构化结果
  （risk_level / 组合指数 R / 保护规则命中）映射到内部 warning_level。
  【五灾种独立声明，不共用同一组阈值】；当前全部为 MOCK 映射规则。

【诚实声明 / 重要红线】
  1. mapping_version = "MOCK-v0.1"：R 阈值（65/40）与升级条件均为项目原型自拟测试假设，
     不是任何官方/科学预警标准，未经历史案例验证。
  2. 不输出红/橙/黄/蓝等官方等级：official_grade 恒为 pending_review。
  3. 预警等级与风险指数分离：本表只做"等级映射"，不重新计算风险。

【warning_level 枚举（内部口径，非官方分级）】
  "none"     不构成预警建议（数据不足 / 指数为 0）
  "advisory" 提示级（Mock）
  "watch"    关注级（Mock）
  "warning"  警报级（Mock）

【对外契约】
  get_hazard_warning_rules(hazard_type) -> dict（无效灾种抛 UnsupportedHazardError）
  list_hazard_warning_capabilities() -> list
  提供给：hazard_warning_rule 节点（等级映射）、alert_builder（行动建议/时效）、测试。
"""

from aegis.agents_ext.hazard_registry import (
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

# 各灾种建议行动（Mock 文案；正式行动清单需业务方确认）
_SUGGESTED_ACTIONS = {
    "warning": [
        "（Mock）按预案启动该灾种应急响应流程",
        "（Mock）通知值守人员加密监测频次",
        "（Mock）对影响区开展巡查并准备人员转移方案",
    ],
    "watch": [
        "（Mock）加密监测与人工巡查",
        "（Mock）通报相关责任人与值守点",
    ],
    "advisory": [
        "（Mock）保持关注，例行记录",
    ],
    "none": [],
}


def _rules(r_warning: float, r_watch: float) -> dict:
    """等级映射参数快捷构造（R 阈值为 MOCK 测试假设）。"""
    return {
        "r_warning": r_warning,
        "r_watch": r_watch,
        "escalate_rule_hits": ["high"],  # 保护规则 level_suggest=high 命中 → 直接 warning
        "valid_duration_hours": 24,  # 预警有效时长（Mock）
    }


HAZARD_WARNING_RULES = {
    # 1. **R ≥ 65** → warning 警报级
    # 2. **40 ≤ R ＜ 65** → watch 关注级
    # 3. **0 ＜ R ＜ 40** → advisory 提示级
    # 4. **R=0 / 无数据** → none 不预警
    LANDSLIDE: {  # 滑坡
        "hazard_type": LANDSLIDE,
        "hazard_type_cn": HAZARD_CN_NAME[LANDSLIDE],
        "mapping_version": "MOCK-v0.1",
        "disclaimer": "滑坡预警等级映射为 MOCK 测试规则，非官方预警标准，禁止用于真实发布。",
        "params": _rules(r_warning=65.0, r_watch=40.0),
        "spec_reference": "等级映射框架自拟；因子依据 DZ/T 0438—2023（转引）",
    },
    ROCKFALL: {  # 崩塌危岩
        "hazard_type": ROCKFALL,
        "hazard_type_cn": HAZARD_CN_NAME[ROCKFALL],
        "mapping_version": "MOCK-v0.1",  # 标记为测试版本
        "disclaimer": "崩塌危岩预警等级映射为 MOCK 测试规则，非官方预警标准，禁止用于真实发布。",
        "params": _rules(r_warning=65.0, r_watch=40.0),
        "spec_reference": "等级映射框架自拟；因子依据 DZ/T 0438—2023（转引）",
    },
    DEBRIS_FLOW: {
        "hazard_type": DEBRIS_FLOW,
        "hazard_type_cn": HAZARD_CN_NAME[DEBRIS_FLOW],
        "mapping_version": "MOCK-v0.1",
        "disclaimer": "泥石流预警等级映射为 MOCK 测试规则，非官方预警标准，禁止用于真实发布。",
        "params": _rules(r_warning=65.0, r_watch=40.0),
        "spec_reference": "等级映射框架自拟；因子依据 DZ/T 0438—2023（转引）",
    },
    ICE_ROCK_SNOW_AVALANCHE: {
        "hazard_type": ICE_ROCK_SNOW_AVALANCHE,
        "hazard_type_cn": HAZARD_CN_NAME[ICE_ROCK_SNOW_AVALANCHE],
        "mapping_version": "MOCK-v0.1",
        "disclaimer": "冰岩雪崩预警等级映射为 MOCK 测试规则，非官方预警标准，禁止用于真实发布。",
        "params": _rules(r_warning=65.0, r_watch=40.0),
        "spec_reference": "等级映射框架自拟；因子依据 DZ/T 0438—2023 6.3.8（转引）",
    },
    GLACIAL_LAKE_OUTBURST: {
        "hazard_type": GLACIAL_LAKE_OUTBURST,
        "hazard_type_cn": HAZARD_CN_NAME[GLACIAL_LAKE_OUTBURST],
        "mapping_version": "MOCK-v0.1",
        "disclaimer": "冰湖溃决预警等级映射为 MOCK 测试规则，非官方预警标准，禁止用于真实发布。",
        "params": _rules(r_warning=65.0, r_watch=40.0),
        "spec_reference": "等级映射框架自拟；因子依据 DZ/T 0438—2023 6.3.8.2（转引）",
    },
}


def get_hazard_warning_rules(hazard_type: str) -> dict:
    """返回某灾种的预警规则声明副本。无效灾种抛 UnsupportedHazardError。"""
    key = normalize_hazard_type(hazard_type)
    if not key or key not in HAZARD_WARNING_RULES:
        raise UnsupportedHazardError(
            f"灾种'{hazard_type}'未提供或无法识别，Warning Agent 无法选择预警规则。当前支持：{supported_hazards_text()}"
        )
    return dict(HAZARD_WARNING_RULES[key])


def get_suggested_actions(hazard_type: str, warning_level: str) -> list:
    """返回某灾种某等级下的建议行动（Mock 文案）。"""
    return list(_SUGGESTED_ACTIONS.get(warning_level, []))


def list_hazard_warning_capabilities() -> list:
    """返回五灾种预警能力清单（全部 Mock 映射）。"""
    out = []
    for key, rules in HAZARD_WARNING_RULES.items():
        out.append(
            {
                "hazard_type": key,
                "hazard_type_cn": rules["hazard_type_cn"],  # hazard_type_cn灾种的中文名称
                "mapping_version": rules["mapping_version"],
                "is_mock": True,
                "params": dict(rules["params"]),
            }
        )
    return out
