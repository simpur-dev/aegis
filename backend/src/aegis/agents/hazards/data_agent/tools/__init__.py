"""工具模块：数据源工具（五灾种 Mock）、灾种数据注册表与通用清洗/标准化/校验工具的统一入口

【导出约定】左侧=磁盘文件名，右侧=文件内函数名。
  二者同名时务必用 `from .文件 import 函数` 形式，
  否则 `from 包 import 名字` 会拿到 module 而非 function（项目踩坑约定）。
【诚实声明】五个数据源工具全部是 Mock 构造器，不含地震（本项目不研究地震）。
"""

from .avalanche_data_tool import avalanche_data_tool
from .data_clean_tool import data_clean_tool
from .data_normalize_tool import data_normalize_tool
from .data_validate_tool import data_validate_tool
from .debris_flow_data_tool import debris_flow_data_tool
from .glacial_lake_data_tool import glacial_lake_data_tool
from .hazard_data_registry import (
    COMMON_FIELDS,
    COMMON_NORMALIZE_PRECISION,
    COMMON_REQUIRED_FIELDS,
    COMMON_VALID_RANGES,
    HAZARD_DATA_REQUIREMENTS,
    build_query_param_model,
    get_data_source_tool,
    get_hazard_data_requirements,
    list_hazard_data_capabilities,
)
from .landslide_data_tool import landslide_data_tool
from .rockfall_data_tool import rockfall_data_tool

__all__ = [
    # 灾种数据注册表
    "COMMON_FIELDS",
    "COMMON_NORMALIZE_PRECISION",
    "COMMON_REQUIRED_FIELDS",
    "COMMON_VALID_RANGES",
    "HAZARD_DATA_REQUIREMENTS",
    "avalanche_data_tool",
    "build_query_param_model",
    # 通用数据处理工具
    "data_clean_tool",
    "data_normalize_tool",
    "data_validate_tool",
    "debris_flow_data_tool",
    "get_data_source_tool",
    "get_hazard_data_requirements",
    "glacial_lake_data_tool",
    # 五灾种数据源工具（Mock）
    "landslide_data_tool",
    "list_hazard_data_capabilities",
    "rockfall_data_tool",
]
