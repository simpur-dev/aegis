"""§4.4 推演沙盒的角色侧包（方案的工程落点：`services/simulation.py` 编排 + `simulation/roles.py` 角色）。

这里放的是**角色与它的行动面**（画像、动作白名单、schema、规则档与 LLM 档两个 policy 实现）；
数值模型与 K×T 编排留在 `aegis.services.simulation`，因为那是既有导入面，
挪它会让"换角色内环"牵连一堆无关调用点。
"""

from aegis.simulation.roles import (
    ACTIONS,
    ROLE_PROFILES,
    ROLES_BY_KEY,
    LlmRolePolicy,
    RoleAction,
    RoleChooser,
    RoleProfile,
    RulePolicy,
)

__all__ = [
    "ACTIONS",
    "ROLES_BY_KEY",
    "ROLE_PROFILES",
    "LlmRolePolicy",
    "RoleAction",
    "RoleChooser",
    "RoleProfile",
    "RulePolicy",
]
