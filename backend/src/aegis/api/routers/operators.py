"""席位台账：谁在班上、什么角色。

三个端点只回答"人"的事实，不回答"能做什么动作"——动作权限在 P1 的 `authorize()` 里，
两处都判会漂移。`me` 不因缺席位头报错：前端要能区分"没选席位"与"席位无效"。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query

from aegis.api.deps import get_container
from aegis.container import PlatformContainer
from aegis.domain.operator import Operator, OperatorRole, role_at_least
from aegis.services.operators import OperatorConflictError, OperatorService, OperatorUnavailableError

OperatorHeader = Annotated[str | None, Header(alias="X-Aegis-Operator")]


def _service(ctn: PlatformContainer) -> OperatorService:
    if ctn.operators is None or not ctn.operators.available:
        raise HTTPException(
            status_code=503,
            detail={"code": "E_OPERATOR_UNAVAILABLE", "message": "席位台账不可用：当前存储后端没有 operators 面"},
        )
    return ctn.operators


async def _require(service: OperatorService, operator_id: str | None, *, role: OperatorRole) -> Operator:
    if not operator_id:
        raise HTTPException(
            status_code=401,
            detail={"code": "E_OPERATOR_REQUIRED", "message": "需要 X-Aegis-Operator 席位头"},
        )
    operator = await service.get(operator_id)
    if operator is None or not operator.active:
        raise HTTPException(
            status_code=403,
            detail={"code": "E_OPERATOR_INVALID", "message": f"席位不存在或已停用：{operator_id}"},
        )
    if not role_at_least(operator.role, role):
        raise HTTPException(
            status_code=403,
            detail={"code": "E_OPERATOR_FORBIDDEN", "message": f"需要 {role.value} 及以上角色"},
        )
    return operator


def build_router() -> APIRouter:
    router = APIRouter()

    @router.get("/api/v1/operators/me", tags=["operator"])
    async def me(
        ctn: PlatformContainer = Depends(get_container),
        x_operator: OperatorHeader = None,
    ) -> dict[str, Any]:
        service = _service(ctn)
        if not x_operator:
            return {"operator_id": None, "display_name": None, "role": None, "active": None}
        try:
            operator = await service.get(x_operator)
        except OperatorUnavailableError as exc:  # pragma: no cover - _service 已挡住
            raise HTTPException(status_code=503, detail={"code": "E_OPERATOR_UNAVAILABLE", "message": str(exc)}) from exc
        if operator is None:
            return {"operator_id": x_operator, "display_name": None, "role": None, "active": False}
        return operator.model_dump(mode="json")

    @router.get("/api/v1/operators", tags=["operator"])
    async def list_operators(
        ctn: PlatformContainer = Depends(get_container),
        x_operator: OperatorHeader = None,
        active_only: bool = Query(default=True),
        limit: int = Query(default=200, ge=1, le=500),
    ) -> dict[str, Any]:
        service = _service(ctn)
        await _require(service, x_operator, role=OperatorRole.LEAD)
        rows = await service.list_operators(active_only=active_only, limit=limit)
        return {"count": len(rows), "items": [row.model_dump(mode="json") for row in rows]}

    @router.post("/api/v1/operators", tags=["operator"], status_code=201)
    async def create_operator(
        payload: Operator,
        ctn: PlatformContainer = Depends(get_container),
        x_operator: OperatorHeader = None,
    ) -> dict[str, Any]:
        service = _service(ctn)
        await _require(service, x_operator, role=OperatorRole.ADMIN)
        try:
            operator = await service.create(payload)
        except OperatorConflictError as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": "E_OPERATOR_CONFLICT", "message": str(exc)},
            ) from exc
        return operator.model_dump(mode="json")

    return router
