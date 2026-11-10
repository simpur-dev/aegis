"""
Simulation Agent 工具包

【本文件角色】统一导出路由 / 执行器 / 校验 / 默认情景 / 对比 / 完整性检查六个工具。
【导出约定】左侧=磁盘文件名，右侧=文件内函数名，务必用 from .文件 import 函数名，
  否则会拿到 module 而非 function（项目踩坑约定）。
"""

from aegis.agents.hazards.simulation_agent.tools.completeness_check_tool import check_results_completeness_tool
from aegis.agents.hazards.simulation_agent.tools.default_scenario_tool import build_default_scenarios_tool
from aegis.agents.hazards.simulation_agent.tools.hazard_model_router import (
    DEBRIS_FLOW,
    GLACIAL_LAKE_OUTBURST,
    HAZARD_ALIAS,
    HAZARD_CN_NAME,
    ICE_ROCK_SNOW_AVALANCHE,
    LANDSLIDE,
    ROCKFALL,
    SUPPORTED_HAZARDS,
    UnsupportedHazardError,
    get_model,
    get_model_requirements,
    is_supported_hazard,
    list_supported_hazards,
    normalize_hazard_type,
    supported_hazards_text,
)
from aegis.agents.hazards.simulation_agent.tools.scenario_comparison_tool import compare_scenarios_tool
from aegis.agents.hazards.simulation_agent.tools.scenario_validate_tool import validate_scenarios_tool
from aegis.agents.hazards.simulation_agent.tools.simulation_executor import (
    SimulationExecutionError,
    SimulationTimeoutError,
    execute_scenario_with_timeout,
)

__all__ = [
    "DEBRIS_FLOW",
    "GLACIAL_LAKE_OUTBURST",
    "HAZARD_ALIAS",
    "HAZARD_CN_NAME",
    "ICE_ROCK_SNOW_AVALANCHE",
    # 灾种常量
    "LANDSLIDE",
    "ROCKFALL",
    "SUPPORTED_HAZARDS",
    # 执行器
    "SimulationExecutionError",
    "SimulationTimeoutError",
    # 路由
    "UnsupportedHazardError",
    "build_default_scenarios_tool",
    "check_results_completeness_tool",
    "compare_scenarios_tool",
    "execute_scenario_with_timeout",
    "get_model",
    "get_model_requirements",
    "is_supported_hazard",
    "list_supported_hazards",
    "normalize_hazard_type",
    "supported_hazards_text",
    # 校验 / 兜底 / 对比 / 完整性
    "validate_scenarios_tool",
]
