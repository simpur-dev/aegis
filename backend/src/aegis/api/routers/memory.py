"""共享记忆面（§4.3）：四类门面的读出口、共享条目的读 / 写 / 删。

`connected=false`（这条腿没接）与『接了但没命中』必须不是一回事，所以每条响应都带 driver 与 order。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator

from aegis.api.deps import get_container
from aegis.container import PlatformContainer
from aegis.domain.enums import HazardType
from aegis.domain.messages import SharedKind, new_memory_record_id, now_iso


class MemoryIn(BaseModel):
    """人工/复盘侧写一条共享记忆（§4.3 记忆可治理的写入口）。

    `kind` 只允许三个值，`region_code` 允许小写（存储侧统一归成大写口径）；
    `valid_at` 缺省用当前时刻——它是"这条经验适用于哪个时刻"，不是"什么时候按了按钮"，
    所以复盘补录时必须能显式给，不能悄悄拿 now() 顶替现场时刻。
    """

    model_config = ConfigDict(extra="forbid")

    kind: SharedKind
    content: str = Field(min_length=2, max_length=2_000)
    agent_id: str = Field(default="", max_length=64)
    region_code: str | None = Field(default=None, max_length=24, pattern=r"^[0-9A-Za-z]{6,24}$")
    refs: list[str] = Field(default_factory=list, max_length=32)
    disagreement: bool = False
    valid_at: str | None = Field(default=None, max_length=64)

    @field_validator("content", mode="before")
    @classmethod
    def _trim(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


def build_router() -> APIRouter:
    router = APIRouter()

    @router.get("/api/v1/memory/categories", tags=["memory"])
    async def memory_categories(
        ctn: PlatformContainer = Depends(get_container),
        q: str = Query(default="", min_length=1, max_length=200, description="一句自然语言查询，三类各读各的"),
        region_code: str | None = Query(default=None, pattern=r"^[0-9A-Z]{6,24}$"),
        hazard_type: HazardType | None = None,
        limit: int = Query(default=3, ge=1, le=10),
    ) -> dict[str, Any]:
        """§4.3 四类记忆门面的读者：一次查询同时看见每一类"接没接、读到什么、为什么是空"。

        修前的形状是两头都空：装配点一个 provider 都没给，而 `read_categories()` 在仓里
        没有任何生产调用方——"四类记忆统一门面"这句话于是既没有底也没有出口。
        这里把它接成真的出口，并把 §4.3 自己定的纪律落到字段上：
        **`connected=false`（这条腿没接）与"接了但没命中"不是一回事**，都回空列表就等于在骗读的人。
        """
        memory = ctn.memory
        if memory is None:
            raise HTTPException(status_code=503, detail={"code": "E_MEMORY_UNAVAILABLE", "message": "记忆服务未装配（memory）"})
        categories = await memory.read_categories(
            q,
            region_code=region_code,
            hazard_type=None if hazard_type is None else hazard_type.value,
            limit=limit,
        )
        return {
            "query": q,
            "driver": memory.backend,
            "categories": categories,
            "shared": {"backend": memory.backend, "stats": dict(memory.stats)},
        }

    @router.get("/api/v1/memory/shared", tags=["memory"])
    async def memory_shared(
        ctn: PlatformContainer = Depends(get_container),
        kind: SharedKind | None = None,
        region_code: str | None = None,
        agent_id: str | None = None,
        q: str | None = Query(default=None, min_length=1, max_length=500),
        limit: int = Query(default=10, ge=1, le=100),
    ) -> dict[str, Any]:
        """共享记忆读出口：除了条目，必须同时说清"哪一档后端"和"按什么序给的"。

        这两个事实决定了空结果怎么读：`driver=memory` 的空是"这个进程还没人写过"，
        而 `driver=postgres` + `order=vector` 的空是"按语义找过、确实没有相近的"。
        只回 `items: []` 就是让调用方猜。
        """
        if ctn.memory is None:
            raise HTTPException(status_code=503, detail={"code": "E_MEMORY_UNAVAILABLE", "message": "记忆服务未装配（memory）"})
        # 序必须由"这一趟实际传了什么"决定，而不是由"配没配嵌入腿"决定：
        # 腿在但这一次算失败，存储侧退的是时序，此时声称 vector 就是骗人。
        query_embedding = ctn.memory.embed_query(q) if q else None
        records = await ctn.memory.read_shared(
            kind=kind, agent_id=agent_id, region_code=region_code, limit=limit, query_embedding=query_embedding
        )
        if query_embedding is not None:
            order = "vector"
        elif q:
            order = "valid_at_desc_no_embedding"
        else:
            order = "valid_at_desc"
        return {
            "count": len(records),
            "items": [record.model_dump() for record in records],
            "driver": ctn.memory.backend,
            "order": order,
            "stats": dict(ctn.memory.stats),
        }

    @router.post("/api/v1/memory/shared", tags=["memory"], status_code=201)
    async def memory_write(
        payload: MemoryIn,
        ctn: PlatformContainer = Depends(get_container),
    ) -> dict[str, Any]:
        """写一条共享记忆（复盘教训、人工纠正的经验）。挂在令牌面之后：这是写侧，不是查询。"""
        if ctn.memory is None:
            raise HTTPException(status_code=503, detail={"code": "E_MEMORY_UNAVAILABLE", "message": "记忆服务未装配（memory）"})
        record_id = new_memory_record_id()
        written = await ctn.memory.write_shared(
            kind=payload.kind,
            agent_id=payload.agent_id,
            content=payload.content,
            record_id=record_id,
            region_code=payload.region_code,
            refs=payload.refs,
            disagreement=payload.disagreement,
            valid_at=payload.valid_at or now_iso(),
        )
        return {"record_id": written, "driver": ctn.memory.backend, "embedded": ctn.memory.embedder_ready}

    @router.delete("/api/v1/memory/shared/{record_id}", tags=["memory"])
    async def memory_forget(
        record_id: str,
        ctn: PlatformContainer = Depends(get_container),
    ) -> dict[str, Any]:
        """删除一条记忆（§4.3「记忆可治理」）。没删到就是 404，不回一个 `removed: false` 的 200。

        后果区别很实在：运维脚本 `if resp.ok` 会把"这条 id 早就不存在"读成"清理完成了"。
        """
        if ctn.memory is None:
            raise HTTPException(status_code=503, detail={"code": "E_MEMORY_UNAVAILABLE", "message": "记忆服务未装配（memory）"})
        if not await ctn.memory.forget(record_id):
            raise HTTPException(status_code=404, detail=f"共享记忆里没有找到 {record_id}")
        return {"record_id": record_id, "removed": True, "driver": ctn.memory.backend}

    return router
