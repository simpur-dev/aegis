"""HTTP API（L4 服务层对外接口 + L5 Web 的数据源）。

约定：全部只读接口不改状态；写接口仅两类——演练触发（/drill）与人工上报（/reports）。
智能体不通过 HTTP 交互，一律走总线契约（见 contracts/）。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from aegis import __version__
from aegis.api.workflow_api import build_router as build_workflow_router
from aegis.bus import subjects
from aegis.config import Settings, get_settings
from aegis.container import PlatformContainer, create_container
from aegis.domain.enums import HazardType
from aegis.domain.messages import AgentMessage, TelemetryReading
from aegis.errors import AegisError

log = logging.getLogger("aegis.api")


class DrillRequest(BaseModel):
    scenario: str = Field(default="surge", pattern="^(surge|normal)$")
    ticks: int = Field(default=2, ge=1, le=50)
    region_code: str | None = Field(default=None, pattern=r"^[0-9A-Z]{6,24}$")


class ReportIn(BaseModel):
    """群防群治/巡查上报（Web 表单）。文本由平台结构化后进规则引擎。"""

    reporter: str = Field(min_length=2, max_length=64)
    region_code: str = Field(pattern=r"^[0-9A-Z]{6,24}$")
    hazard_hint: str | None = Field(default=None, max_length=64)
    note: str = Field(min_length=4, max_length=2_000)
    lat: float | None = Field(default=None, ge=-90, le=90)
    lon: float | None = Field(default=None, ge=-180, le=180)


def get_container(request: Request) -> PlatformContainer:
    container: PlatformContainer | None = getattr(request.app.state, "container", None)
    if container is None:
        raise HTTPException(status_code=503, detail="平台尚未就绪")
    return container


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    container: PlatformContainer = getattr(app.state, "container", None) or create_container(settings)
    app.state.container = container
    await container.start(with_mock_agents=settings.env != "prod", with_ingest_loop=True)
    try:
        yield
    finally:
        await container.shutdown()


def create_app(settings: Settings | None = None, *, container: PlatformContainer | None = None) -> FastAPI:
    cfg = settings or get_settings()
    app = FastAPI(
        title="AEGIS 山地灾害多智能体协同调控平台",
        version=__version__,
        description="Adaptive Emergency Geo-hazard Intelligence System — 平台侧 API",
        lifespan=lifespan,
    )
    app.state.settings = cfg
    if container is not None:
        app.state.container = container
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"] if cfg.env != "prod" else [],
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    @app.get("/healthz", tags=["ops"])
    async def healthz() -> dict[str, Any]:
        return {"status": "ok", "version": __version__, "contract": "agent_message.v1"}

    @app.get("/readyz", tags=["ops"], responses={503: {"description": "总线未连接，平台尚未就绪"}})
    async def readyz(ctn: PlatformContainer = Depends(get_container)) -> dict[str, Any]:
        ready = ctn.transport.connected
        if not ready:
            raise HTTPException(status_code=503, detail="总线未连接")
        return {
            "status": "ready",
            "bus": ctn.transport.name,
            "agents_online": ctn.registry.online_count,
            "store": ctn.store.snapshot(),
        }

    @app.get("/api/v1/integrations", tags=["ops"])
    async def integrations(ctn: PlatformContainer = Depends(get_container)) -> dict[str, Any]:
        """可选子系统的装配事实：未启用 / 已启用 / 启用了但降级，三者必须能被外部区分。

        这不是健康检查的重复：`/readyz` 只答"总线通不通"，这里答"哪条腿是瘸的"。
        """
        rows = ctn.integration_status()
        degraded = [state.name for state in rows if state.enabled and "degraded" in state.detail]
        return {
            "items": [state.as_dict() for state in rows],
            "degraded": degraded,
            "all_enabled": all(state.enabled for state in rows),
        }

    @app.get("/api/v1/cases/recall", tags=["knowledge"])
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

    @app.get("/api/v1/retrieval/search", tags=["knowledge"], responses={503: {"description": "检索层未启用"}})
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

    @app.get("/api/v1/telemetry", tags=["data"])
    async def list_telemetry(
        ctn: PlatformContainer = Depends(get_container),
        station_id: str | None = None,
        metric: str | None = None,
        region_code: str | None = None,
        limit: int = Query(default=200, ge=1, le=5_000),
    ) -> dict[str, Any]:
        rows = ctn.store.telemetry.query(station_id=station_id, metric=metric, region_code=region_code, limit=limit)
        return {"count": len(rows), "items": [r.model_dump() for r in rows]}

    @app.post("/api/v1/drill/run", tags=["drill"])
    async def run_drill(
        payload: DrillRequest,
        ctn: PlatformContainer = Depends(get_container),
    ) -> dict[str, Any]:
        """演练：注入一轮模拟监测数据并跑通全链路，返回链路结果与指标快照。"""
        if ctn.simulator is None:
            raise HTTPException(status_code=503, detail="模拟器未启用")
        ctn.simulator.scenario = payload.scenario
        report = await ctn.ingest.ingest_once()
        readings = ctn.store.telemetry.query(region_code=payload.region_code, limit=1_000)
        if not readings:
            return {"ingest": report.as_dict(), "chains": [], "note": "本轮无可用读数"}

        grouped: dict[str, list[TelemetryReading]] = {}
        for reading in readings:
            grouped.setdefault(reading.region_code, []).append(reading)
        results = await ctn.chain.process_many(grouped)
        return {
            "ingest": report.as_dict(),
            "chains": [r.as_dict() for r in results],
            "regions": len(grouped),
        }

    @app.get("/api/v1/warnings", tags=["warning"])
    async def list_warnings(
        ctn: PlatformContainer = Depends(get_container),
        limit: int = Query(default=50, ge=1, le=1_000),
        region_code: str | None = None,
    ) -> dict[str, Any]:
        rows = ctn.store.warnings.list(limit=limit, region_code=region_code)
        return {"count": len(rows), "items": [r.model_dump() for r in rows]}

    @app.get("/api/v1/warnings/{warning_id}", tags=["warning"])
    async def get_warning(warning_id: str, ctn: PlatformContainer = Depends(get_container)) -> dict[str, Any]:
        record = ctn.store.warnings.get(warning_id)
        if record is None:
            raise HTTPException(status_code=404, detail="预警不存在")
        return record.model_dump()

    @app.get("/api/v1/tasks/{task_unit_id}", tags=["task"])
    async def get_task(task_unit_id: str, ctn: PlatformContainer = Depends(get_container)) -> dict[str, Any]:
        unit = ctn.store.tasks.get(task_unit_id)
        if unit is None:
            raise HTTPException(status_code=404, detail="任务单元不存在")
        return unit.model_dump()

    @app.get("/api/v1/events", tags=["task"])
    async def list_events(
        ctn: PlatformContainer = Depends(get_container),
        limit: int = Query(default=20, ge=1, le=200),
    ) -> dict[str, Any]:
        return {"items": [r.as_dict() for r in ctn.store.chains.latest(limit)]}

    @app.get("/api/v1/agents", tags=["agent"])
    async def list_agents(ctn: PlatformContainer = Depends(get_container)) -> dict[str, Any]:
        return {
            "online": ctn.registry.online_count,
            "items": ctn.registry.snapshot(),
            "gateway_counters": dict(ctn.gateway.counters),
        }

    @app.get("/api/v1/collaboration", tags=["agent"])
    async def collaboration(ctn: PlatformContainer = Depends(get_container)) -> dict[str, Any]:
        return {
            "success_rate": ctn.gateway.success_rate(),
            "transactions": [r.as_dict() for r in ctn.gateway.transactions[-200:]],
        }

    @app.get("/api/v1/metrics/latency", tags=["metrics"])
    async def latency(ctn: PlatformContainer = Depends(get_container)) -> dict[str, Any]:
        return ctn.latency_report()

    @app.get("/metrics", tags=["metrics"], response_class=Response)
    async def prometheus(ctn: PlatformContainer = Depends(get_container)) -> Response:
        return Response(content=ctn.exporter.collect(), media_type="text/plain; version=0.0.4")

    @app.get("/api/v1/events/stream", tags=["stream"])
    async def stream(request: Request, ctn: PlatformContainer = Depends(get_container)) -> Response:
        from fastapi.responses import StreamingResponse

        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=512)

        async def _on_message(message: AgentMessage) -> None:
            item = {
                "subject": message.action,
                "trace_id": message.trace_id,
                "payload": message.payload,
                "ts": message.ts,
            }
            if queue.full():
                queue.get_nowait()  # 慢消费者丢最旧事件，保证不阻塞总线
            await queue.put(item)

        subs = [
            await ctn.transport.subscribe(subjects.alert(1, ">"), _on_message),
            await ctn.transport.subscribe("platform.alert.>", _on_message),
            await ctn.transport.subscribe(subjects.ops("feedback", "status"), _on_message),
        ]

        async def _gen() -> AsyncIterator[str]:
            """长连接生命周期严格短于关停/断连窗口：否则 uvicorn 默认无限等待优雅退出。"""
            idle = 0.0
            try:
                while not ctn.stopping and not await request.is_disconnected():
                    try:
                        item = await asyncio.wait_for(queue.get(), timeout=_DISCONNECT_POLL_SECONDS)
                    except TimeoutError:
                        idle += _DISCONNECT_POLL_SECONDS
                        if idle >= _SSE_KEEPALIVE_SECONDS:
                            idle = 0.0
                            yield ": keep-alive\n\n"
                        continue
                    yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
            finally:
                for sub in subs:
                    await sub.cancel()

        return StreamingResponse(_gen(), media_type="text/event-stream")

    app.include_router(build_workflow_router())

    @app.exception_handler(AegisError)
    async def _aegis_error_handler(_: Request, exc: AegisError) -> Response:
        return Response(
            status_code=422,
            content=json.dumps({"error": exc.code.value, "message": exc.message, "detail": exc.detail}),
            media_type="application/json",
        )

    return app


#: 断连轮询间隔：ASGI 不推送"客户端已断开"事件，只能轮询；0.25s 足够快且不必空转。
_DISCONNECT_POLL_SECONDS = 0.25
#: 空闲多久补一帧注释帧，防止中间设备掐掉长连接（不是业务事件，客户端会忽略）。
_SSE_KEEPALIVE_SECONDS = 15.0
