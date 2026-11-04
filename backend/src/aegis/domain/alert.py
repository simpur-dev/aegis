"""告警实例：一条已发布预警在交互面上的生命周期。

为什么不是 `WarningRecord` 的别名：预警本身会被改级、被撤回，而"谁在什么时候看到、
谁接的手、谁转给谁"是另一条独立的账。两者混用会让"确认"变成"改预警状态"，
审计上就分不清是平台改了预警，还是值班员接了单。

`ack_by`（确认人）与 `assigned_to`（当前处置人）刻意分开：转派只改后者，
否则一次转派就会抹掉"原先是誰确认的"这条事实。
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

_ALERT_ID = r"^alt_[0-9a-f]{8,24}$"

#: 告警还在值班员桌上（列表默认只查这些）。`merged`/`closed` 是终态，不再接受处置。
OPEN_STATUSES: frozenset[str] = frozenset({"raised", "delivered", "acked", "escalated"})
#: 可以"接手"的状态。已合并/已关闭的告警再点接手没有意义，服务端直接 409。
ACTIONABLE_STATUSES: frozenset[str] = frozenset({"raised", "delivered", "escalated"})


class AlertStatus(StrEnum):
    RAISED = "raised"
    DELIVERED = "delivered"
    ACKED = "acked"
    ESCALATED = "escalated"
    MERGED = "merged"
    CLOSED = "closed"


def new_alert_id() -> str:
    return f"alt_{secrets.token_hex(8)}"


def utc_now() -> datetime:
    return datetime.now(UTC)


def now_iso() -> str:
    return utc_now().isoformat()


def level_rank(level: int) -> int:
    """数值越小越严重（RED=1）。越界按最低等级处理，不抛——广播来自外部，形状要能容错。"""
    return level if 1 <= level <= 4 else 4


class Alert(BaseModel):
    """告警实例本体，字段与 `assistant_alerts` 表一一对应。

    不含 `title`/`hazard_type`：那是 `warnings` 的事实，复制一份到这里就会漂移。
    列表接口用 LEFT JOIN 现取；实时路径直接用广播 payload 里已有的标题。
    """

    model_config = ConfigDict(extra="forbid")

    alert_id: str = Field(default_factory=new_alert_id, pattern=_ALERT_ID)
    warning_id: str = Field(min_length=8, max_length=64)
    level: int = Field(ge=1, le=4)
    region_code: str = Field(min_length=6, max_length=24)
    status: AlertStatus = AlertStatus.RAISED
    ack_by: str | None = None
    ack_at: str | None = None
    assigned_to: str | None = None
    snooze_until: str | None = None
    escalated_at: str | None = None
    drill: bool = False
    created_at: str = Field(default_factory=now_iso)

    @property
    def open(self) -> bool:
        return self.status.value in OPEN_STATUSES
