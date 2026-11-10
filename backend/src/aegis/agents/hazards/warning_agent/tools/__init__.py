"""
Warning 工具包导出

【导出约定】左侧=磁盘文件名，右侧=文件内函数名（项目踩坑约定）。
"""

from .cap_format_tool import (
    CAP_MAPPING_VERSION,
    internal_alert_to_cap,
    serialize_cap_xml,
    validate_cap_dict,
)
from .hazard_warning_rule_registry import (
    HAZARD_WARNING_RULES,
    get_hazard_warning_rules,
    get_suggested_actions,
    list_hazard_warning_capabilities,
)

__all__ = [
    # CAP 1.2 格式转换（阶段 7）
    "CAP_MAPPING_VERSION",
    "HAZARD_WARNING_RULES",
    "get_hazard_warning_rules",
    "get_suggested_actions",
    "internal_alert_to_cap",
    "list_hazard_warning_capabilities",
    "serialize_cap_xml",
    "validate_cap_dict",
]
