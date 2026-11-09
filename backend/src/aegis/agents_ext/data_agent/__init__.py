"""
西藏山地灾害多智能体系统 - Data Agent 模块

提供基于 LangGraph 的数据管理智能体，面向五类典型山地灾害
（landslide / rockfall / debris_flow / ice_rock_snow_avalanche / glacial_lake_outburst），
负责灾种观测数据的获取（当前为 Mock 数据源）、清洗、标准化和质量校验。
灾种标准键与别名统一定义在根目录 hazard_registry.py（本项目不研究地震）。
"""

from aegis.agents_ext.data_agent.graph import build_data_agent_graph
from aegis.agents_ext.data_agent.state import DataAgentState

__all__ = ["DataAgentState", "build_data_agent_graph"]
