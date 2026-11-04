"""值班席位：声明身份，不是强认证。

为什么单独建模型而不是复用 `OwnerRole`：`OwnerRole` 是 STU 的**责任标签**（谁该管这类任务），
席位是**具体的人**（谁在班上、谁点了确认）。两者混用会让"移交"变成"改责任标签"，
审计上追不到人。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

_OPERATOR_ID = r"^[A-Za-z0-9_-]{4,32}$"


class OperatorRole(StrEnum):
    DUTY = "duty"
    LEAD = "lead"
    ADMIN = "admin"


#: 角色强弱：`authorize()` 只比较这个序，不在各处写 if/elif。
ROLE_RANK: dict[OperatorRole, int] = {
    OperatorRole.DUTY: 1,
    OperatorRole.LEAD: 2,
    OperatorRole.ADMIN: 3,
}


class Operator(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operator_id: str = Field(pattern=_OPERATOR_ID)
    display_name: str = Field(min_length=1, max_length=32)
    role: OperatorRole
    active: bool = True


def role_at_least(actual: OperatorRole, required: OperatorRole) -> bool:
    return ROLE_RANK[actual] >= ROLE_RANK[required]
