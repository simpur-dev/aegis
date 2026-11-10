"""站内通知：把一句话送到另一个值班席位。

边界（与 `AlertService` 同一取向）：不生成预警、不下发外部通道、不伪造 `WarningRecord`。
总线只用来"快"（`ops.assistant.notify`），持久记录才是真源；只有站内写失败才如实抛错，
外发与广播失败都不抛（站内真源已落，抛错会逼出重发、造出重复通知），
助手不会把没送到的通知说成已送达。

P4 外部通知（设计 §14.2）：**站内先行、外发失败不装送达**。
顺序固定为"先落站内 → 再试外部通道"，理由是站内是真源（断线重连还能查回来）、
外发只是加速器；站内写失败就不该再发外发——那会造出"外面收到了、平台里查不到"的幽灵通知。
外发失败**不抛异常**（炸了会让"站内已落"看起来像失败，值班员于是重发一遍），
而是把结果写进通知行的 `delivery_status`（`in_app` / `delivered` / `failed`），
让"发没发出去"成为可查的事实而不是一句断言。

为什么 `publish` 是注入的回调而不是直接 import `AgentGateway`：`services/` 不得依赖
`aegis.bus`（架构铁律 3 的 AST 门禁）。subject 也由装配点传入，本层不认识 NATS 主题。
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets
from collections.abc import Awaitable, Callable
from typing import Any

from aegis.domain.enums import Action
from aegis.domain.messages import make_event
from aegis.domain.operator import OperatorRole
from aegis.services.alerting.webhook import NotificationWebhook
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
        webhook: NotificationWebhook | None = None,
    ) -> None:
        self._ledger = ledger
        self._publish = publish
        self._subject = subject
        self._operators = operators
        self._webhook = webhook

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
        """落站内 → 再试外部通道。

        返回的通知行里 `delivery_status` 如实说明外发结果：
        `in_app`（只在站内，默认）/ `delivered`（外发也成功）/ `failed`（外发失败）。
        **站内那一条在任何情况下都成立**——外发失败不改 `created_at`、不清 `delivered_at`。
        """
        record = await self._require().insert_notification(
            recipient=recipient, message=message, kind=kind, sender=sender, space_id=space_id
        )
        record = await self._deliver(record)
        await self._broadcast(record)
        return record

    async def _deliver(self, record: dict[str, Any]) -> dict[str, Any]:
        """尝试外部通道并把结果写回通知行。失败**不抛**（见模块头）。"""
        if self._webhook is None or not self._webhook.enabled:
            return record
        outcome = await self._webhook.deliver(record)
        setter = getattr(self._ledger, "set_notification_delivery", None)
        if setter is None:
            return record
        return await setter(
            int(record["id"]),
            status=str(outcome["status"]),
            error=outcome.get("error"),
        )

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
        # 各收件人相互独立，并发投递而不是逐个串行等待；gather 按 unique 顺序返回。
        return await asyncio.gather(
            *(self.notify(recipient=item, message=message, kind="group", sender=sender, space_id=space_id) for item in unique)
        )

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
                # 外发结果随帧出去：前端据此说"站内已送达/外部通道失败"，
                # 而不是笼统地说"已送达"（设计 §14.2 的口径在前端也成立）。
                "delivery_status": record.get("delivery_status", "in_app"),
                "delivery_error": record.get("delivery_error"),
            },
        )
        # 总线只是加速器：站内真源已落，广播失败不抛——否则值班员看到失败重发，会造出重复通知。
        with contextlib.suppress(Exception):
            await self._publish(self._subject, event)
