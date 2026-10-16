"""知识面：案例召回 / 案例入库 / 混合检索。都是审计出口，不是链路的替代入口。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from aegis.api.deps import get_container
from aegis.container import PlatformContainer
from aegis.domain.enums import HazardType
from aegis.knowledge.cases import HazardCase


def build_router() -> APIRouter:
    router = APIRouter()

    @router.get("/api/v1/cases/recall", tags=["knowledge"])
    async def recall_cases(
        ctn: PlatformContainer = Depends(get_container),
        q: str = Query(default="", max_length=300),
        hazard_type: HazardType | None = None,
        region_code: str | None = Query(default=None, pattern=r"^[0-9A-Z]{4,24}$"),
        limit: int = Query(default=3, ge=1, le=20),
    ) -> dict[str, Any]:
        """案例召回：与预案生成链路走同一个 provider、同一个预算，因此这里看到的就是链路看到的。

        只读、不触发 LLM（图谱的 LLM 调用只在写路径 `learn`）。`degraded` 逐条标注本条
        是否来自降级兜底——前端要能区分"图谱命中"与"图谱挂了、内存案例顶上"。
        """
        if ctn.knowledge is None:
            raise HTTPException(status_code=503, detail="知识层未装配")
        matches = await ctn.knowledge.recall(
            q or (hazard_type.cn if hazard_type else ""),
            hazard_type=hazard_type.value if hazard_type else None,
            region_code=region_code,
            limit=limit,
            budget_ms=ctn.settings.knowledge_recall_budget_ms,
        )
        return {
            "query": q,
            "count": len(matches),
            "degraded_count": sum(1 for m in matches if m.degraded),
            "budget_ms": ctn.settings.knowledge_recall_budget_ms,
            "items": [m.planning_brief() for m in matches],
        }

    @router.post("/api/v1/knowledge/cases", tags=["knowledge"], responses={503: {"description": "知识层未装配"}})
    async def learn_case(
        case: HazardCase,
        ctn: PlatformContainer = Depends(get_container),
    ) -> dict[str, Any]:
        """案例入库：知识层唯一的写入口（复盘/经验修正后把案例喂给图谱与内存库）。

        响应必须回答"这条案例落在了哪一侧"：图谱写失败时降级链仍会把它收进进程内库，
        只看 HTTP 200 会读成"案例时序知识已更新"，而图其实是空的。`degraded`/`reason`
        就是为这件事准备的——审计口径要能分辨"图谱里有这条案例"与"只有模板库里有"。

        只有显式入库才走这里：Graphiti 的一次 `add_episode` ≈ 4—7 次 LLM 调用，
        挂在预警/研判路径上会直接把 ≤3min 指标打成不可用，所以这条腿不进任何自动链路。
        """
        if ctn.knowledge is None:
            raise HTTPException(status_code=503, detail="知识层未装配（knowledge）")
        outcome = await ctn.knowledge.learn_case(case)
        return {"title": case.title, **outcome.model_dump()}

    @router.get(
        "/api/v1/retrieval/search",
        tags=["knowledge"],
        responses={503: {"description": "检索层未启用"}},
    )
    async def search_context(
        ctn: PlatformContainer = Depends(get_container),
        q: str = Query(min_length=1, max_length=500),
        k: int = Query(default=5, ge=1, le=20),
        rerank: bool = True,
    ) -> dict[str, Any]:
        """混合检索的审计出口：同一条查询给出排序结果、命中的腿、各腿分数与降级记录。

        读路径不含 LLM；`provenance` 原样返回，是为了让"这条为什么排在前面"能被第三方复核——
        只给名次不给分的检索结果不能进预警正文。
        """
        if ctn.retrieval is None:
            raise HTTPException(status_code=503, detail="检索层未启用")
        from aegis.retrieval.service import RetrievalQuery  # 延迟导入：HTTP 层不因此依赖检索实现

        outcome = await ctn.retrieval.retrieve(RetrievalQuery(text=q, k=k, rerank=rerank))
        return {"query": q, **outcome.as_dict(), "items": [doc.as_reference() for doc in outcome.docs]}

    return router
