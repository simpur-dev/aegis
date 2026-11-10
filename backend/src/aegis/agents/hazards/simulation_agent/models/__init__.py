"""
Simulation Agent 模型包

【本文件角色】统一导出模型接口基类与五个灾种 MOCK 模型。
【导出约定】左侧=磁盘文件名，右侧=文件内类名，务必用 from .文件 import 类名，
  否则会拿到 module 而非 class（项目踩坑约定，见 risk_agent/tools/__init__.py）。
【诚实声明】当前五个模型全部是 MOCK 玩具模型（MOCK-v0.1），输出非真实预测。
"""

from aegis.agents.hazards.simulation_agent.models.avalanche_model import AvalancheMockModel
from aegis.agents.hazards.simulation_agent.models.base_model import (
    MOCK_DISCLAIMER,
    SEVERITY_MULTIPLIER,
    VALID_SEVERITY_LEVELS,
    BaseHazardSimulationModel,
    ScenarioConfig,
    clip01,
)
from aegis.agents.hazards.simulation_agent.models.debris_flow_model import DebrisFlowMockModel
from aegis.agents.hazards.simulation_agent.models.glacial_lake_model import GlacialLakeOutburstMockModel
from aegis.agents.hazards.simulation_agent.models.landslide_model import LandslideMockModel
from aegis.agents.hazards.simulation_agent.models.rockfall_model import RockfallMockModel

__all__ = [
    "MOCK_DISCLAIMER",
    "SEVERITY_MULTIPLIER",
    "VALID_SEVERITY_LEVELS",
    "AvalancheMockModel",
    "BaseHazardSimulationModel",
    "DebrisFlowMockModel",
    "GlacialLakeOutburstMockModel",
    "LandslideMockModel",
    "RockfallMockModel",
    "ScenarioConfig",
    "clip01",
]
