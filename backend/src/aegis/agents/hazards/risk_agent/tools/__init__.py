"""
Risk 工具包导出（西藏五类山地地质灾害，因子化口径，不含地震）

【⚠️ 规避重名坑】左边=磁盘文件名，右边=文件内函数名。
  二者同名时务必用 `from .文件 import 函数` 形式，
  否则 `from 包 import 名字` 会拿到 module 而非 function（项目踩坑约定）。

【诚实声明】五灾种因子权重/规则全部为 MOCK-v0.1 原型测试假设；
  R 公式为项目原型自拟候选组合公式（非规范条款、非任务书公式）。
"""

from .avalanche_risk_tool import avalanche_factor_assessment_tool
from .debris_flow_risk_tool import debris_flow_factor_assessment_tool
from .factor_engine import (
    FACTOR_ENGINE_VERSION,
    R_FORMULA_EXPRESSION,
    R_FORMULA_NOTE,
    R_FORMULA_STATUS,
    compute_factor_assessment,
    normalize_value,
)
from .glacial_lake_risk_tool import glacial_lake_factor_assessment_tool
from .hazard_risk_registry import (
    HAZARD_RISK_PROFILES,
    get_hazard_risk_profile,
    list_hazard_risk_capabilities,
)
from .landslide_risk_tool import landslide_factor_assessment_tool
from .risk_evidence_tool import risk_evidence_tool
from .rockfall_risk_tool import rockfall_factor_assessment_tool

__all__ = [
    # 因子引擎
    "FACTOR_ENGINE_VERSION",
    # 灾种风险注册表
    "HAZARD_RISK_PROFILES",
    "R_FORMULA_EXPRESSION",
    "R_FORMULA_NOTE",
    "R_FORMULA_STATUS",
    "avalanche_factor_assessment_tool",
    "compute_factor_assessment",
    "debris_flow_factor_assessment_tool",
    "get_hazard_risk_profile",
    "glacial_lake_factor_assessment_tool",
    # 五灾种因子评估工具
    "landslide_factor_assessment_tool",
    "list_hazard_risk_capabilities",
    "normalize_value",
    # 通用证据工具
    "risk_evidence_tool",
    "rockfall_factor_assessment_tool",
]
