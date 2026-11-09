"""
Simulation Agent 灾种模型路由工具

定位：**Simulation Agent 的灾种中央注册、名称归一化、模型分发、参数元信息查询中心**
它是整个仿真智能体里**所有节点查找灾害模型的唯一入口**。
核心一句话：接收用户 / 上游传来的灾种名称（中文、英文别名），翻译成系统内部统一标准键，
然后找到对应的灾害仿真模型，或者明确抛出不支持异常；
**绝不擅自猜测、替换成相近灾种**。

【本文件角色】
  灾种常量与注册表、中文名称表、别名归一化表、模型路由与参数需求声明。
  按 tasks/四 的要求：根据已确认的 hazard_type 选择对应模型；
  输入无法明确对应灾种或模型不支持时，返回明确状态与原因，绝不擅自换模型。

【诚实声明 / 重要红线】
  当前注册的五个模型全部是 MOCK 玩具模型（MOCK-v0.1）。
  归一化失败（返回空串）或 get_model 抛 UnsupportedHazardError 时，
  调用方必须把明确原因传给输出节点，【不得】退而选择相近灾种的模型。

【对外契约】
  提供给：nodes/scenario_design_node.py（前置检查 + 参数菜单）；
          nodes/hazard_model_router_node.py（权威归一化与路由判定）；
          nodes/simulation_execute_node.py（取模型）；
          nodes/results_analysis_node.py / simulation_output_node.py（取需求声明）；
          tools/scenario_validate_tool.py、tools/default_scenario_tool.py。
  不提供给：Data Agent / Risk Agent。

【灾种标准键（全项目统一定义在根目录 hazard_registry.py，本文件复用同一份；
  2026-10-18 起 Data/Risk Agent 亦使用该定义，别名映射不再各自维护）】
  landslide                滑坡
  rockfall                 崩塌危岩
  debris_flow              泥石流
  ice_rock_snow_avalanche  冰岩雪崩
  glacial_lake_outburst    冰湖溃决
"""


# 项目根目录入 sys.path，保证 "import hazard_registry" 在任何运行方式下可用

# ★ 灾种常量/中文名/别名/归一化统一来自根目录 hazard_registry.py（单一事实来源）
from aegis.agents_ext.hazard_registry import (  # noqa: F401
    DEBRIS_FLOW,
    GLACIAL_LAKE_OUTBURST,
    HAZARD_ALIAS,
    HAZARD_CN_NAME,
    ICE_ROCK_SNOW_AVALANCHE,
    LANDSLIDE,
    ROCKFALL,
    SUPPORTED_HAZARDS,
    UnsupportedHazardError,
    is_supported_hazard,
    normalize_hazard_type,
    supported_hazards_text,
)
from aegis.agents_ext.simulation_agent.models import (
    AvalancheMockModel,
    BaseHazardSimulationModel,
    DebrisFlowMockModel,
    GlacialLakeOutburstMockModel,
    LandslideMockModel,
    RockfallMockModel,
)

# 模型注册表：键 = 规范灾种键；值 = 模型类（get_model 每次 get() 新实例，保证无状态）
_MODEL_REGISTRY = {
    LANDSLIDE: LandslideMockModel,
    ROCKFALL: RockfallMockModel,
    DEBRIS_FLOW: DebrisFlowMockModel,
    ICE_ROCK_SNOW_AVALANCHE: AvalancheMockModel,
    GLACIAL_LAKE_OUTBURST: GlacialLakeOutburstMockModel,
}


# ============================================================
# 路由函数（归一化/别名/支持清单复用根注册表；模型分发为本文件职责）
# ============================================================


def get_model(hazard_type: str) -> BaseHazardSimulationModel:
    """返回该灾种模型的新实例（无状态）。

    ★ 红线：查不到就抛 UnsupportedHazardError，绝不返回相近灾种的模型替代。
    """
    key = normalize_hazard_type(hazard_type)
    if not key or key not in _MODEL_REGISTRY:
        raise UnsupportedHazardError(f"不支持的灾种'{hazard_type}'。当前支持：{supported_hazards_text()}")
    return _MODEL_REGISTRY[key]()


def get_model_requirements(hazard_type: str) -> dict:
    """返回该灾种模型的参数需求声明（供情景设计拼参数菜单、校验、输出组装）。

    不支持 → raise UnsupportedHazardError。
    """
    key = normalize_hazard_type(hazard_type)
    if not key or key not in _MODEL_REGISTRY:
        raise UnsupportedHazardError(f"不支持的灾种'{hazard_type}'。当前支持：{supported_hazards_text()}")
    model = _MODEL_REGISTRY[key]()
    param_menu = []
    for name, (lo, hi, desc) in model.param_constraints.items():
        param_menu.append(
            {
                "name": name,
                "range": [lo, hi],
                "default": model.optional_inputs.get(name),
                "required": name in model.required_inputs,
                "description": desc,
            }
        )
    return {
        "hazard_type": key,
        "hazard_type_cn": HAZARD_CN_NAME[key],
        "required_inputs": list(model.required_inputs),
        "optional_inputs": dict(model.optional_inputs),
        "param_menu": param_menu,  # 情景设计 LLM 的"参数菜单"（只能从中选键取值）
        "required_metrics": list(model.required_metrics),
        "n_steps": model.n_steps,
        "model_version": model.MODEL_VERSION,
        "simulation_mode": model.simulation_mode,
        "is_real_prediction": model.is_real_prediction,
        "disclaimer": model.disclaimer,
        "real_model_note": model.real_model_note,
    }


def list_supported_hazards() -> list:
    """返回支持灾种清单：[{hazard_type, hazard_type_cn, model_version, simulation_mode, is_real_prediction}]。"""
    out = []
    for key in SUPPORTED_HAZARDS:
        cls = _MODEL_REGISTRY[key]
        out.append(
            {
                "hazard_type": key,
                "hazard_type_cn": HAZARD_CN_NAME[key],
                "model_version": cls.MODEL_VERSION,
                "simulation_mode": cls.simulation_mode,
                "is_real_prediction": cls.is_real_prediction,
            }
        )
    return out


# 【并入主线时的改动】原文件在这里**又定义了一遍** `supported_hazards_text`，
# 与上面从 hazard_registry 导入的那份实现逐字相同——覆盖它无害，但违反该模块自己的
# "灾种定义只有一处"的单一真源原则（两份实现一旦各自演化就漂移）。
# 现在只保留导入那一份：灾种清单的唯一定义源永远是 hazard_registry。
