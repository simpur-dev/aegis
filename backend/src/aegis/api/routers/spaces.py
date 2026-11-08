"""处置空间：`/spaces` 读面 + 三种任务处置写面（设计文档 §5.1 / FR-4.2–4.4）。

一条预警一个空间，空间里每个 STU 一条任务；`space_id = warning_id` 是应用层约定
（`container.on_result` 里由 `CollabService.ensure_from_chain` 幂等建立）。

三条口径：
- **权限与席位表同一处**（`operators.require_operator`）：认领/回执 `duty`，移交 `lead`；
  路由层不自己比角色，避免与助手动作的 `authorize()` 漂移成两套规则。
- **乐观锁由客户端带版本**：`expected_version` 是客户端看到的那版，服务端不替它改主意；
  命中 0 行就是 409，前端据此提示"已被他人处置"并重新对账。
- **台账是真源，SSE 只负责快**：状态迁移成功后由 `CollabService` 广播
  `handling.task.changed`，断线重连一律重新拉本读面。
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
from aegis.services.collab.tasks import (
    CollabService,
    SpaceNotFoundError,
    TaskConflictError,
    TaskNotFoundError,
    TaskNotInSpaceError,
)
from aegis.services.operators import OperatorService

OperatorHeader = Annotated[str | None, Header(alias="X-Aegis-Operator")]

_OPERATOR_ID = r"^[A-Za-z0-9_-]{4,32}$"


class TaskRequest(BaseModel):
    """`expected_version` 不填 = 不指定版本（对话里的助手动作走这条路）。"""

    model_config = ConfigDict(extra="forbid")

    expected_version: int | None = Field(default=None, ge=1)


class TransferRequest(TaskRequest):
    assignee: str = Field(pattern=_OPERATOR_ID)


def _collab(ctn: PlatformContainer) -> CollabService:
    if ctn.collab is None or not ctn.collab.available:
        raise HTTPException(
            status_code=503,
            detail={"code": "E_COLLAB_UNAVAILABLE", "message": "处置空间台账不可用：当前装配没有 collab 面"},
        )
    return ctn.collab


def _operators(ctn: PlatformContainer) -> OperatorService:
    if ctn.operators is None or not ctn.operators.available:
        raise HTTPException(
            status_code=503,
            detail={"code": "E_OPERATOR_UNAVAILABLE", "message": "席位台账不可用：写动作无法确定操作者"},
        )
    return ctn.operators


async def _translated(call: Awaitable[Any]) -> Any:
    """服务层的四种失败翻成 HTTP：404 是"没有/指错"，409 是"被人抢先改过"。"""
    try:
        return await call
    except (SpaceNotFoundError, TaskNotFoundError, TaskNotInSpaceError) as exc:
        raise HTTPException(status_code=404, detail={"code": "E_TARGET_NOT_FOUND", "message": str(exc)}) from exc
    except TaskConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "E_TASK_STATE_CONFLICT", "message": str(exc), "status": exc.status},
        ) from exc


def build_router() -> APIRouter:
    router = APIRouter()

    @router.get("/api/v1/spaces", tags=["space"])
    async def list_spaces(
        ctn: PlatformContainer = Depends(get_container),
        open_only: bool = Query(default=True),
        limit: int = Query(default=20, ge=1, le=200),
    ) -> dict[str, Any]:
        """空间列表。设计稿里只列了详情端点，但界面要能"选一个空间"，故补这一条读面。"""
        collab = _collab(ctn)
        rows = await collab.list_spaces(open_only=open_only, limit=limit)
        return {"count": len(rows), "items": rows, "persistent": collab.persistent}

    @router.get("/api/v1/spaces/{space_id}", tags=["space"])
    async def space_detail(
        space_id: str,
        ctn: PlatformContainer = Depends(get_container),
        limit: int = Query(default=200, ge=1, le=200),
    ) -> dict[str, Any]:
        collab = _collab(ctn)
        detail = await _translated(collab.space_detail(space_id, limit=limit))
        return {**detail, "persistent": collab.persistent}

    async def _write(
        ctn: PlatformContainer,
        x_operator: str | None,
        space_id: str,
        task_id: str,
        *,
        role: OperatorRole,
        run: Any,
    ) -> dict[str, Any]:
        collab = _collab(ctn)
        operator = await require_operator(_operators(ctn), x_operator, role=role)
        await _translated(collab.get_task_in_space(space_id, task_id))
        task = await _translated(await run(collab, operator.operator_id))
        return {"task": task, "space_id": space_id}

    @router.post("/api/v1/spaces/{space_id}/tasks/{task_id}/claim", tags=["space"])
    async def claim_task(
        space_id: str,
        task_id: str,
        payload: TaskRequest | None = None,
        ctn: PlatformContainer = Depends(get_container),
        x_operator: OperatorHeader = None,
    ) -> dict[str, Any]:
        version = None if payload is None else payload.expected_version

        async def run(collab: CollabService, operator_id: str) -> Any:
            return collab.claim(task_id, operator_id, expected_version=version)

        return await _write(ctn, x_operator, space_id, task_id, role=OperatorRole.DUTY, run=run)

    @router.post("/api/v1/spaces/{space_id}/tasks/{task_id}/ack", tags=["space"])
    async def ack_task(
        space_id: str,
        task_id: str,
        payload: TaskRequest | None = None,
        ctn: PlatformContainer = Depends(get_container),
        x_operator: OperatorHeader = None,
    ) -> dict[str, Any]:
        version = None if payload is None else payload.expected_version

        async def run(collab: CollabService, operator_id: str) -> Any:
            return collab.ack(task_id, operator_id, expected_version=version)

        return await _write(ctn, x_operator, space_id, task_id, role=OperatorRole.DUTY, run=run)

    @router.post("/api/v1/spaces/{space_id}/tasks/{task_id}/transfer", tags=["space"])
    async def transfer_task(
        space_id: str,
        task_id: str,
        payload: TransferRequest,
        ctn: PlatformContainer = Depends(get_container),
        x_operator: OperatorHeader = None,
    ) -> dict[str, Any]:
        operators = _operators(ctn)
        assignee = await operators.get(payload.assignee)
        if assignee is None or not assignee.active:
            raise HTTPException(
                status_code=422,
                detail={"code": "E_OPERATOR_INVALID", "message": f"受理席位不存在或已停用：{payload.assignee}"},
            )

        async def run(collab: CollabService, _operator_id: str) -> Any:
            return collab.transfer(task_id, assignee.operator_id, expected_version=payload.expected_version)

        return await _write(ctn, x_operator, space_id, task_id, role=OperatorRole.LEAD, run=run)

    return router
