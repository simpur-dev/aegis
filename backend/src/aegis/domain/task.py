"""处置任务的状态口径（与 `010_assistant_control.sql::handling_tasks` 逐字对齐）。

状态迁移不放在这里：`domain/` 只给枚举与常量，原子迁移在持久层 SQL 与内存台账各一份，
二者语义逐条对齐（与 `domain/alert.py` 同一取向）。
"""

from __future__ import annotations

from enum import StrEnum


class TaskStatus(StrEnum):
    PENDING = "pending"
    CLAIMED = "claimed"
    IN_PROGRESS = "in_progress"
    ACKED = "acked"
    CLOSED = "closed"


#: 仍在处置中的状态：关闭后一切迁移都拒绝。
OPEN_TASK_STATUSES: frozenset[str] = frozenset({"pending", "claimed", "in_progress", "acked"})
#: 只有没人认领的任务可以被认领。
CLAIMABLE_STATUSES: frozenset[str] = frozenset({"pending"})
#: 认领/执行中才能确认完成。
ACKABLE_STATUSES: frozenset[str] = frozenset({"claimed", "in_progress"})
