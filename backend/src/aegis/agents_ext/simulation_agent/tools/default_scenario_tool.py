"""
Simulation Agent 确定性默认情景生成工具

【本文件角色】
  LLM 情景设计不可用时的确定性兜底：按 severity 阶梯（low/medium/high）
  生成参数取自模型声明约束区间分位点的情景，保证三情景指标单调可对比。

【诚实声明 / 重要红线】
  默认情景的参数取值是"约束区间的固定分位点"（低 25% / 中 50% / 高 75%），
  属于确定性模板，【不是】专家设计，也不是任何科学推荐值；
  生成的情景假设必须在 assumptions 中显式标注为 MOCK 兜底。

【对外契约】
  提供给：nodes/scenario_design_node.py（LLM 异常兜底）。
  前置条件：hazard_type 必须已归一化为支持键（否则 get_model_requirements 抛异常）。

**当 LLM 无法工作（没 API 密钥、调用报错、输出乱掉）的时候，
自动生成 3 套确定性的基准情景 low / medium / high，
保证流程还能继续往下跑，不需要人工手动写情景。**
"""

from aegis.agents_ext.simulation_agent.tools.hazard_model_router import (
    get_model_requirements,
    normalize_hazard_type,
)

# severity → 约束区间分位点（确定性模板约定）
SEVERITY_FILL_FACTOR = {"low": 0.25, "medium": 0.5, "high": 0.75}

# 各灾种默认触发因素描述（演示用文本）
DEFAULT_TRIGGERS = {
    "landslide": "持续强降雨",
    "rockfall": "岩体风化与机械扰动",
    "debris_flow": "短时强降雨",
    "ice_rock_snow_avalanche": "持续升温叠加新增降雪",
    "glacial_lake_outburst": "冰崩涌浪与水位上涨",
}


def build_default_scenarios_tool(hazard_type: str) -> list:
    """生成确定性默认情景列表（low/medium/high 三档）。

    参数:
        hazard_type: 灾种（支持规范键或别名，内部归一化）

    返回:
        情景 dict 列表（扁平结构，每条附 assumptions 标注 MOCK 兜底来源）；
        灾种不支持时抛 UnsupportedHazardError（由调用方保证已前置检查）。
    """
    key = normalize_hazard_type(hazard_type)
    req = get_model_requirements(key)  # 不支持 → 抛异常

    scenarios = []
    for i, (sev, factor) in enumerate(SEVERITY_FILL_FACTOR.items(), start=1):
        params = {}
        for entry in req["param_menu"]:
            name, (lo, hi) = entry["name"], entry["range"]
            if name in req["optional_inputs"]:
                # 可选参数取模型声明的默认值
                params[name] = req["optional_inputs"][name]
            else:
                # 必需参数取约束区间的固定分位点（保证三情景单调可对比）
                params[name] = round(lo + (hi - lo) * factor, 4)

        sc = {
            "scenario_id": f"SC-{i:02d}",
            "hazard_type": key,
            "severity_level": sev,
            "trigger": DEFAULT_TRIGGERS.get(key, "未声明触发因素"),
            "trigger_source_tag": "assumption",
            "duration_hours": 6.0,
            "notes": "确定性默认情景（LLM 不可用时的 MOCK 兜底模板，非专家设计）",
            "test_hooks": {},
            "assumptions": [
                "情景参数为确定性默认模板取值（约束区间分位点），非专家设计，非观测数据",
            ],
        }
        sc.update(params)
        scenarios.append(sc)

    return scenarios
