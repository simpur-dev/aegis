"""站内通知：把一句话送到另一个值班席位。

边界（与 `AlertService` 同一取向）：不生成预警、不下发外部通道、不伪造 `WarningRecord`。
总线只用来"快"（`ops.assistant.notify`），持久记录才是真源；两者都失败时如实抛错，
助手不会把没送到的通知说成已送达。

为什么 `publish` 是注入的回调而不是直接 import `AgentGateway`：`services/` 不得依赖
`aegis.bus`（架构铁律 3 的 AST 门禁）。subject 也由装配点传入，本层不认识 NATS 主题。
"""

from __future__ import annotations

import secrets
from collections.abc import Awaitable, Callable
from typing import Any

from aegis.domain.enums import Action
from aegis.domain.messages import make_event
from aegis.domain.operator import OperatorRole
from aegis.services.operators import OperatorService
from aegis.storage.store import SupportsNotifications


class Notifier:
    def __init__(
        self,
        ledger: SupportsNotifications | None,
        *,
        publish: Callable[[str, Any], Awaitable[None]] | None,
        subject: str,
        operators: OperatorService | None,
    ) -> None:
        self._ledger = ledger
        self._publish = publish
        self._subject = subject
        self._operators = operators

    @property
    def available(self) -> bool:
        return self._ledger is not None

    def _require(self) -> SupportsNotifications:
        if self._ledger is None:
            raise RuntimeError("站内通知台账未装配")
        return self._ledger

    async def lead_ids(self) -> list[str]:
        """值班长席位：没给具体收件人时用它，避免助手自己编一个 operator_id。"""
        if self._operators is None or not self._operators.available:
            return []
        rows = await self._operators.list_operators(active_only=True, limit=200)
        return [row.operator_id for row in rows if row.role in (OperatorRole.LEAD, OperatorRole.ADMIN)]

    async def notify(
        self,
        *,
        recipient: str,
        message: str,
        kind: str,
        sender: str = "",
        space_id: str | None = None,
    ) -> dict[str, Any]:
        record = await self._require().insert_notification(
            recipient=recipient, message=message, kind=kind, sender=sender, space_id=space_id
        )
        await self._broadcast(record)
        return record

    async def notify_many(
        self,
        recipients: list[str],
        message: str,
        *,
        sender: str = "",
        space_id: str | None = None,
    ) -> list[dict[str, Any]]:
        # 同一席位在一条群发里出现两次只留一条：去重在写入前做，不靠前端去重。
        unique = list(dict.fromkeys(item.strip() for item in recipients if item.strip()))
        return [await self.notify(recipient=item, message=message, kind="group", sender=sender, space_id=space_id) for item in unique]

    async def list_for(self, recipient: str, *, unread_only: bool = False, limit: int = 50) -> list[dict[str, Any]]:
        return await self._require().list_notifications(recipient=recipient, unread_only=unread_only, limit=limit)

    async def mark_read(self, notification_id: int, recipient: str) -> dict[str, Any] | None:
        return await self._require().mark_read(notification_id, recipient)

    async def _broadcast(self, record: dict[str, Any]) -> None:
        if self._publish is None:
            return
        event = make_event(
            source="platform.assistant",
            action=Action.NOTIFY_SENT.value,
            trace_id=f"trc_{secrets.token_hex(8)}",
            target="platform.notify_sink",
            payload={
                "id": record["id"],
                "recipient": record["recipient"],
                "message": record["message"],
                "kind": record["kind"],
                "sender": record["sender"],
                "space_id": record["space_id"],
                "created_at": record["created_at"],
            },
        )
        await self._publish(self._subject, event)
