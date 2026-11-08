"""审计读面（P4 读面需求，设计 §14.3）：让 §4.9 写下来的账真能被读出来。

一条口径只留一处：**读权限的判定在 `require_operator` 与本模块的 `scope` 参数里**，
路由层不另写一套角色判断（与助手 `authorize()` 同一取向——两处都判会漂移）。

三条设计取舍：
1. **`duty` 只能读自己的**：`operator_id` 被**强制改写**成请求者本人，
   传入别人的值也不生效——否则"查我的"这个参数就是个越权开关；
2. **读接口不写审计**：审计读面自己不再产生审计行，否则每次查账都多一行，
   账本会被读操作本身淹没；
3. **不给总数**：按时间倒序翻页时"总条数"要扫全表，而这张表按月分区、越往后越大。
   给 `count`（本次返回条数）与 `items`，翻页用 `offset`。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query

from aegis.api.deps import get_container
from aegis.api.routers.operators import require_operator
from aegis.container import PlatformContainer
from aegis.domain.operator import OperatorRole, role_at_least
from aegis.services.operators import OperatorService
from aegis.storage.store import SupportsAuditOps

OperatorHeader = Annotated[str | None, Header(alias="X-Aegis-Operator")]


def _operators(ctn: PlatformContainer) -> OperatorService:
    service = ctn.operators
    if service is None or not service.available:
        raise HTTPException(status_code=503, detail={"code": "E_OPERATOR_UNAVAILABLE", "message": "席位台账不可用"})
    return service


def _audit_ledger(ctn: PlatformContainer) -> SupportsAuditOps:
    store = getattr(ctn, "store", None)
    ledger = store if isinstance(store, SupportsAuditOps) else None
    if ledger is None:
        raise HTTPException(
            status_code=503, detail={"code": "E_AUDIT_UNAVAILABLE", "message": "审计读面不可用：当前存储后端没有 audit_ops 面"}
        )
    return ledger


def build_router() -> APIRouter:
    router = APIRouter()

    @router.get("/api/v1/audit/ops", tags=["audit"])
    async def read_audit(
        ctn: PlatformContainer = Depends(get_container),
        x_operator: OperatorHeader = None,
        operator_id: str | None = Query(default=None, description="按操作者过滤；duty 只能看自己"),
        action: str | None = Query(default=None),
        phase: str | None = Query(default=None, description="intent（想做）/ result（做完）"),
        space_id: str | None = Query(default=None),
        action_id: str | None = Query(default=None),
        since: str | None = Query(default=None, description="ISO 时间下界（含）"),
        until: str | None = Query(default=None, description="ISO 时间上界（含）"),
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, Any]:
        """按人/动作/阶段/空间/时间读审计流水，最近在前。"""
        operators = _operators(ctn)
        requester = await require_operator(operators, x_operator, role=OperatorRole.DUTY)
        ledger = _audit_ledger(ctn)

        # `duty` 的范围被强制收成自己：传入别人的 operator_id 不生效（不是报错，是"就看你自己的"）。
        scope = operator_id
        if not role_at_least(requester.role, OperatorRole.LEAD):
            scope = requester.operator_id

        items = await ledger.read_audit(
            operator_id=scope,
            action=action,
            phase=phase,
            space_id=space_id,
            action_id=action_id,
            since=since,
            until=until,
            limit=limit,
            offset=offset,
        )
        return {
            "items": items,
            "count": len(items),
            "limit": limit,
            "offset": offset,
            "scope": scope,
            "requested_by": requester.operator_id,
        }

    return router
