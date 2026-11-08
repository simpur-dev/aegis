"""站内通知台账：助手 `notify.lead/group` 的落库面。

通知不是告警、也不是预警：它不进 `assistant_alerts`（那条表按 warning_id 去重），
不碰 `DeliveryDispatcher`（外部触达是 P4），只做「谁给谁留了什么话」的持久记录。
SSE 广播负责"快"，这张表负责断线重连后还能查回来。
"""

from __future__ import annotations

from typing import Any

_COLUMNS = (
    "id, recipient, message, kind, sender, space_id, created_at, read_at, delivery_channel, delivery_status, delivery_error, delivered_at"
)


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def notification_from_row(row: Any) -> dict[str, Any]:
    # 投递三列用 `.get()` 而不是直接索引：迁移 013 之前的旧库里没有这几列，
    # 直接索引会 KeyError 让"读通知"整体 500——旧库读旧数据应当照常工作。
    return {
        "id": int(row["id"]),
        "recipient": row["recipient"],
        "message": row["message"],
        "kind": row["kind"],
        "sender": row["sender"],
        "space_id": row["space_id"],
        "created_at": _iso(row["created_at"]),
        "read_at": _iso(row["read_at"]),
        "delivery_channel": row.get("delivery_channel", "in_app"),
        "delivery_status": row.get("delivery_status", "in_app"),
        "delivery_error": row.get("delivery_error"),
        "delivered_at": _iso(row.get("delivered_at")),
    }


async def insert_notification(
    pool: Any,
    *,
    recipient: str,
    message: str,
    kind: str,
    sender: str = "",
    space_id: str | None = None,
) -> dict[str, Any]:
    row = await pool.fetchrow(
        f"""
        INSERT INTO assistant_notifications (recipient, message, kind, sender, space_id)
        VALUES ($1, $2, $3, $4, $5)
        RETURNING {_COLUMNS}
        """,
        recipient,
        message,
        kind,
        sender,
        space_id,
    )
    return notification_from_row(row)


async def list_notifications(pool: Any, *, recipient: str, unread_only: bool = False, limit: int = 50) -> list[dict[str, Any]]:
    unread = "AND read_at IS NULL" if unread_only else ""
    rows = await pool.fetch(
        f"""
        SELECT {_COLUMNS} FROM assistant_notifications
        WHERE recipient = $1 {unread}
        ORDER BY created_at DESC LIMIT $2
        """,
        recipient,
        max(1, min(int(limit), 200)),
    )
    return [notification_from_row(row) for row in rows]


async def mark_read(pool: Any, notification_id: int, recipient: str) -> dict[str, Any] | None:
    """只改本人名下那条：别人不能替你标已读。"""
    row = await pool.fetchrow(
        f"""
        UPDATE assistant_notifications SET read_at = now()
        WHERE id = $1 AND recipient = $2 AND read_at IS NULL
        RETURNING {_COLUMNS}
        """,
        int(notification_id),
        recipient,
    )
    return None if row is None else notification_from_row(row)


async def set_delivery(pool: Any, notification_id: int, *, status: str, error: str | None = None) -> dict[str, Any]:
    """写外部通道的投递结果（设计 §14.2）。

    站内那一条**不回退**：外发失败只把 `delivery_status` 写成 `failed`，
    `created_at`（站内送达时刻）与 `read_at` 都不动。
    """
    row = await pool.fetchrow(
        f"""
        UPDATE assistant_notifications
        SET delivery_status = $2,
            delivery_error = $3,
            delivered_at = now()
        WHERE id = $1
        RETURNING {_COLUMNS}
        """,
        int(notification_id),
        status,
        error,
    )
    return notification_from_row(row)
