"""席位台账：谁在班上、什么角色。

三个端点只回答"人"的事实，不回答"能做什么动作"——动作权限在 P1 的 `authorize()` 里，
两处都判会漂移。`me` 不因缺席位头报错：前端要能区分"没选席位"与"席位无效"。

P4 强认证（设计 §14.1/§14.5）另加三条令牌端点：**明文只在签发响应里出现一次**，
之后任何读接口都不返回它（连摘要也不返回）。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query

from aegis.api.deps import get_container
from aegis.container import PlatformContainer
from aegis.domain.operator import Operator, OperatorRole, role_at_least
from aegis.services.operator_auth import (
    OperatorAuthService,
    OperatorKeyError,
    OperatorKeyUnavailableError,
)
from aegis.services.operators import OperatorConflictError, OperatorService, OperatorUnavailableError

OperatorHeader = Annotated[str | None, Header(alias="X-Aegis-Operator")]


def _service(ctn: PlatformContainer) -> OperatorService:
    if ctn.operators is None or not ctn.operators.available:
        raise HTTPException(
            status_code=503,
            detail={"code": "E_OPERATOR_UNAVAILABLE", "message": "席位台账不可用：当前存储后端没有 operators 面"},
        )
    return ctn.operators


def _auth(ctn: PlatformContainer) -> OperatorAuthService:
    """按人令牌服务。缺台账时 503——令牌台账只有持久层有（重启即失等于"钥匙还有效但门锁忘了"）。"""
    service = ctn.operator_auth
    if service is None or not service.available:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "E_OPERATOR_KEY_UNAVAILABLE",
                "message": "按人令牌台账不可用：当前存储后端没有 operator_keys 面",
            },
        )
    return service


async def require_operator(service: OperatorService, operator_id: str | None, *, role: OperatorRole) -> Operator:
    """席位校验的唯一实现：告警、协同等写动作都走这一处，避免每个路由各写一套口径。

    401 = 没带席位头（前端要能提示"先在页脚选席位"）；403 = 席位不存在/停用/角色不够。
    """
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
        await require_operator(service, x_operator, role=OperatorRole.LEAD)
        rows = await service.list_operators(active_only=active_only, limit=limit)
        return {"count": len(rows), "items": [row.model_dump(mode="json") for row in rows]}

    @router.post("/api/v1/operators", tags=["operator"], status_code=201)
    async def create_operator(
        payload: Operator,
        ctn: PlatformContainer = Depends(get_container),
        x_operator: OperatorHeader = None,
    ) -> dict[str, Any]:
        service = _service(ctn)
        await require_operator(service, x_operator, role=OperatorRole.ADMIN)
        try:
            operator = await service.create(payload)
        except OperatorConflictError as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": "E_OPERATOR_CONFLICT", "message": str(exc)},
            ) from exc
        return operator.model_dump(mode="json")

    # ---------- P4 强认证：按人令牌（设计 §14.1/§14.5） ----------

    @router.post("/api/v1/operators/{operator_id}/keys", tags=["operator"], status_code=201)
    async def issue_key(
        operator_id: str,
        ctn: PlatformContainer = Depends(get_container),
        x_operator: OperatorHeader = None,
    ) -> dict[str, Any]:
        """签发一把按人令牌。**明文只在这条响应里出现一次**，之后谁也拿不回去。

        签发凭据比"建席位"更高一档：只有 admin 能给**别人**签发；
        本人给自己签发也要 admin 席位——否则任何席位都能给自己配一把钥匙。
        """
        service = _service(ctn)
        await require_operator(service, x_operator, role=OperatorRole.ADMIN)
        auth = _auth(ctn)
        try:
            issued = await auth.issue_key(operator_id)
        except OperatorKeyUnavailableError as exc:
            raise HTTPException(status_code=503, detail={"code": "E_OPERATOR_KEY_UNAVAILABLE", "message": str(exc)}) from exc
        except OperatorKeyError as exc:
            raise HTTPException(status_code=404, detail={"code": exc.reason, "message": exc.message}) from exc
        return {
            "key": issued.key.model_dump(mode="json"),
            "plaintext": issued.plaintext,
            "notice": "明文只出现这一次，请立即交给持有人；平台不保存也无法找回。",
        }

    @router.get("/api/v1/operators/{operator_id}/keys", tags=["operator"])
    async def list_keys(
        operator_id: str,
        ctn: PlatformContainer = Depends(get_container),
        x_operator: OperatorHeader = None,
    ) -> dict[str, Any]:
        """列某席位的令牌元数据：**没有摘要、没有明文**。"""
        service = _service(ctn)
        requester = await require_operator(service, x_operator, role=OperatorRole.ADMIN)
        auth = _auth(ctn)
        items = await auth.list_keys(operator_id)
        return {"operator_id": operator_id, "count": len(items), "items": items, "requested_by": requester.operator_id}

    @router.delete("/api/v1/operators/{operator_id}/keys/{key_id}", tags=["operator"])
    async def revoke_key(
        operator_id: str,
        key_id: str,
        ctn: PlatformContainer = Depends(get_container),
        x_operator: OperatorHeader = None,
    ) -> dict[str, Any]:
        """吊销一把令牌。本人可撤自己的（丢了要先撤），别人的要 admin。"""
        service = _service(ctn)
        requester = await require_operator(service, x_operator, role=OperatorRole.DUTY)
        if requester.operator_id != operator_id and not role_at_least(requester.role, OperatorRole.ADMIN):
            raise HTTPException(
                status_code=403,
                detail={"code": "E_OPERATOR_FORBIDDEN", "message": "只能吊销自己的令牌；别人的令牌需要 admin"},
            )
        auth = _auth(ctn)
        revoked = await auth.revoke_key(operator_id, key_id)
        if not revoked:
            raise HTTPException(status_code=404, detail={"code": "E_OPERATOR_KEY_NOT_FOUND", "message": f"令牌不存在或已吊销：{key_id}"})
        return {"operator_id": operator_id, "key_id": key_id, "revoked": True}

    return router
