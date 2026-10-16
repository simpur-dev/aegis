"""预警与任务读面：人工核签的待办清单（review/pending）、预警列表与详情、任务单元、链路事件。

待办清单读的是链路台账里的一列事实，不是某段 `rationale` 文本——文本不能查询、不能计数、不能变成待办。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from aegis.api.deps import get_container
from aegis.container import PlatformContainer
from aegis.persistence.accuracy import label_lookup_port
from aegis.services.memory.review_queue import case_ids, pending_items


def build_router() -> APIRouter:
    router = APIRouter()

    @router.get("/api/v1/review/pending", tags=["warning"])
    async def review_pending(
        ctn: PlatformContainer = Depends(get_container),
        limit: int = Query(default=200, ge=1, le=2_000, description="回看多少条链路台账样本"),
        region_code: str | None = Query(default=None, pattern=r"^[0-9A-Z]{6,24}$"),
    ) -> dict[str, Any]:
        """§3.5 低置信护栏的读者：智能体说"这条要人看"，这里就是那张待办清单。

        修前这一格只活在 `rationale` 的一句文本里——文本不能查询、不能计数、不能变成待办，
        于是护栏报了没人接。现在它是 `chain_runs.risk` 里的一列事实，与台账同源。
        `already_reviewed` 看的是 D9 那张真表（`warning_truth_labels`，`case_id` 即 `event_id`）：
        没接标注面时它是 **null**，不是 false——把"查不到"说成"没签过"，值班员会重签一遍已经签过的。
        """
        rows = list(ctn.store.chains.latest(int(limit)))
        items = pending_items(rows, region_code=region_code)
        port = label_lookup_port(ctn.store)
        if port is None:
            return {
                "count": len(items),
                "ledger_rows": len(rows),
                "label_state": "unavailable",
                "label_state_reason": "当前存储后端不提供真值标注面（内存读视图没有 warning_truth_labels）",
                "items": items,
            }
        signed = await port.labelled_case_ids(case_ids(items))
        for item in items:
            item["already_reviewed"] = str(item["event_id"]) in signed
        return {"count": len(items), "ledger_rows": len(rows), "label_state": "ok", "items": items}

    @router.get("/api/v1/warnings", tags=["warning"])
    async def list_warnings(
        ctn: PlatformContainer = Depends(get_container),
        limit: int = Query(default=50, ge=1, le=1_000),
        region_code: str | None = None,
    ) -> dict[str, Any]:
        rows = ctn.store.warnings.list(limit=limit, region_code=region_code)
        return {"count": len(rows), "items": [r.model_dump() for r in rows]}

    @router.get("/api/v1/warnings/{warning_id}", tags=["warning"])
    async def get_warning(warning_id: str, ctn: PlatformContainer = Depends(get_container)) -> dict[str, Any]:
        record = ctn.store.warnings.get(warning_id)
        if record is None:
            raise HTTPException(status_code=404, detail="预警不存在")
        return record.model_dump()

    @router.get("/api/v1/tasks/{task_unit_id}", tags=["task"])
    async def get_task(task_unit_id: str, ctn: PlatformContainer = Depends(get_container)) -> dict[str, Any]:
        unit = ctn.store.tasks.get(task_unit_id)
        if unit is None:
            raise HTTPException(status_code=404, detail="任务单元不存在")
        return unit.model_dump()

    @router.get("/api/v1/events", tags=["task"])
    async def list_events(
        ctn: PlatformContainer = Depends(get_container),
        limit: int = Query(default=20, ge=1, le=200),
    ) -> dict[str, Any]:
        return {"items": [r.as_dict() for r in ctn.store.chains.latest(limit)]}

    return router
