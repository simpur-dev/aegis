"""站内通知读面：断线重连后的对账入口，以及"标为已读"。

写入不在这里：通知只能由助手的 `notify.lead/group` 经 `Notifier` 产生，
开一个任意 POST 端点等于给"谁发的"开了第二个答案。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query

from aegis.api.deps import get_container
from aegis.api.routers.operators import require_operator
from aegis.container import PlatformContainer
from aegis.domain.operator import OperatorRole
from aegis.services.alerting.notifier import Notifier
from aegis.services.operators import OperatorService

OperatorHeader = Annotated[str | None, Header(alias="X-Aegis-Operator")]


def _notifier(ctn: PlatformContainer) -> Notifier:
    service = ctn.notifier
    if service is None or not service.available:
        raise HTTPException(status_code=503, detail={"code": "E_NOTIFIER_UNAVAILABLE", "message": "站内通知台账不可用"})
    return service


def _operators(ctn: PlatformContainer) -> OperatorService:
    service = ctn.operators
    if service is None or not service.available:
        raise HTTPException(status_code=503, detail={"code": "E_OPERATOR_UNAVAILABLE", "message": "席位台账不可用"})
    return service


def build_router() -> APIRouter:
    router = APIRouter()

    @router.get("/api/v1/notifications", tags=["notification"])
    async def list_notifications(
        ctn: PlatformContainer = Depends(get_container),
        x_operator: OperatorHeader = None,
        unread_only: bool = Query(default=False),
        limit: int = Query(default=50, ge=1, le=200),
    ) -> dict[str, Any]:
        notifier = _notifier(ctn)
        operator = await require_operator(_operators(ctn), x_operator, role=OperatorRole.DUTY)
        items = await notifier.list_for(operator.operator_id, unread_only=unread_only, limit=limit)
        return {"items": items, "count": len(items)}

    @router.post("/api/v1/notifications/{notification_id}/read", tags=["notification"])
    async def read_notification(
        notification_id: int,
        ctn: PlatformContainer = Depends(get_container),
        x_operator: OperatorHeader = None,
    ) -> dict[str, Any]:
        notifier = _notifier(ctn)
        operator = await require_operator(_operators(ctn), x_operator, role=OperatorRole.DUTY)
        record = await notifier.mark_read(notification_id, operator.operator_id)
        if record is None:
            raise HTTPException(status_code=404, detail={"code": "E_NOTIFICATION_NOT_FOUND", "message": "通知不存在或已读"})
        return {"notification": record}

    return router
