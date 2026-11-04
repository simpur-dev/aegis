"""告警面：未确认列表与三种处置（接手 / 转派 / 稍后）。

权限口径与席位台账**同一处**（`operators.require_operator`），不在这里另判一遍：
- 接手、稍后 = `duty` 及以上：确认自己看到并接手，是值班员的日常动作；
- 转派 = `lead` 及以上：它会把一条告警放进别人的队列，不是单方面能做的决定。

重复点"接手"不报错（200 + `already=true`）：值班员双击、开两个标签页都是现场常态。
告警不存在 → 404；已合并/已关闭 → 409，两句话术不能混。
"""

from __future__ import annotations

from collections.abc import Awaitable
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from aegis.api.deps import get_container
from aegis.api.routers.operators import require_operator
from aegis.container import PlatformContainer
from aegis.domain.operator import OperatorRole
from aegis.services.alerting import AlertNotFoundError, AlertService, AlertStateConflictError
from aegis.services.operators import OperatorService

OperatorHeader = Annotated[str | None, Header(alias="X-Aegis-Operator")]

_OPERATOR_ID = r"^[A-Za-z0-9_-]{4,32}$"


class TransferRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assignee: str = Field(pattern=_OPERATOR_ID)


class SnoozeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    minutes: int = Field(default=15, ge=1, le=240)


def _service(ctn: PlatformContainer) -> AlertService:
    if ctn.alerts is None or not ctn.alerts.available:
        raise HTTPException(
            status_code=503,
            detail={"code": "E_ALERT_UNAVAILABLE", "message": "告警台账不可用：当前装配没有 alerts 面"},
        )
    return ctn.alerts


def _operator_service(ctn: PlatformContainer) -> OperatorService:
    if ctn.operators is None or not ctn.operators.available:
        raise HTTPException(
            status_code=503,
            detail={"code": "E_OPERATOR_UNAVAILABLE", "message": "席位台账不可用：写动作无法确定操作者"},
        )
    return ctn.operators


async def _translated(call: Awaitable[Any]) -> Any:
    """把服务层的两种失败翻成 HTTP：404 是"没有这条"，409 是"状态不允许"。"""
    try:
        return await call
    except AlertNotFoundError as exc:
        raise HTTPException(status_code=404, detail={"code": "E_ALERT_NOT_FOUND", "message": str(exc)}) from exc
    except AlertStateConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "E_ALERT_STATE_CONFLICT", "message": str(exc), "status": exc.status},
        ) from exc


def build_router() -> APIRouter:
    router = APIRouter()

    @router.get("/api/v1/alerts", tags=["alert"])
    async def list_alerts(
        ctn: PlatformContainer = Depends(get_container),
        open_only: bool = Query(default=True),
        status: str | None = Query(default=None, pattern="^(raised|delivered|acked|escalated|merged|closed)$"),
        level: int | None = Query(default=None, ge=1, le=4),
        drill: bool | None = Query(default=None),
        limit: int = Query(default=50, ge=1, le=200),
    ) -> dict[str, Any]:
        """告警列表。默认只给"还在桌上"的（未合并、未关闭）。

        断线重连后前端拉这个接口对账：以库为准，不拿本地缓存当事实。
        """
        service = _service(ctn)
        rows = await service.list_alerts(open_only=open_only, status=status, level=level, drill=drill, limit=limit)
        return {
            "count": len(rows),
            "items": rows,
            "persistent": service.persistent,
            "escalation_minutes": service.escalation_minutes,
        }

    @router.post("/api/v1/alerts/{alert_id}/ack", tags=["alert"])
    async def ack_alert(
        alert_id: str,
        ctn: PlatformContainer = Depends(get_container),
        x_operator: OperatorHeader = None,
    ) -> dict[str, Any]:
        service = _service(ctn)
        operator = await require_operator(_operator_service(ctn), x_operator, role=OperatorRole.DUTY)
        record, already = await _translated(service.ack(alert_id, operator.operator_id))
        return {"alert": record, "already": already}

    @router.post("/api/v1/alerts/{alert_id}/transfer", tags=["alert"])
    async def transfer_alert(
        alert_id: str,
        payload: TransferRequest,
        ctn: PlatformContainer = Depends(get_container),
        x_operator: OperatorHeader = None,
    ) -> dict[str, Any]:
        service = _service(ctn)
        operators = _operator_service(ctn)
        await require_operator(operators, x_operator, role=OperatorRole.LEAD)
        assignee = await operators.get(payload.assignee)
        if assignee is None or not assignee.active:
            raise HTTPException(
                status_code=422,
                detail={"code": "E_OPERATOR_INVALID", "message": f"受理席位不存在或已停用：{payload.assignee}"},
            )
        record = await _translated(service.transfer(alert_id, assignee.operator_id))
        return {"alert": record}

    @router.post("/api/v1/alerts/{alert_id}/snooze", tags=["alert"])
    async def snooze_alert(
        alert_id: str,
        payload: SnoozeRequest,
        ctn: PlatformContainer = Depends(get_container),
        x_operator: OperatorHeader = None,
    ) -> dict[str, Any]:
        service = _service(ctn)
        await require_operator(_operator_service(ctn), x_operator, role=OperatorRole.DUTY)
        record = await _translated(service.snooze(alert_id, payload.minutes))
        return {"alert": record}

    return router
