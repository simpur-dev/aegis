"""HTTP API（L4 服务层对外接口 + L5 Web 的数据源）。

约定：读接口占多数且不改状态；写接口只有三类——演练触发（/drill）、人工上报（/reports）、
案例入库（/knowledge/cases，图谱写入的唯一生产调用点，因此刻意不挂在任何自动链路上）。
智能体不通过 HTTP 交互，一律走总线契约（见 contracts/）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from collections.abc import AsyncIterator, Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import datetime
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Path, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from aegis import __version__
from aegis.api.assistant_api import build_router as build_assistant_router
from aegis.api.workflow_api import build_router as build_workflow_router
from aegis.bus import subjects
from aegis.config import Settings, get_settings
from aegis.container import PlatformContainer, create_container
from aegis.domain.enums import HazardType
from aegis.domain.messages import (
    AgentMessage,
    SharedKind,
    TelemetryReading,
    new_memory_record_id,
    now_iso,
    utc_now,
)
from aegis.errors import AegisError
from aegis.knowledge.cases import HazardCase
from aegis.observability import telemetry
from aegis.persistence import geo
from aegis.persistence.accuracy import (
    DEFAULT_WINDOW_SECONDS,
    MAX_WINDOW_SECONDS,
    accuracy_replay_port,
    check_lead_window,
    label_lookup_port,
    parse_moment,
)
from aegis.persistence.errors import AccuracyArgumentError, QueryArgumentError
from aegis.persistence.replay import MIN_FIELD_CASES, ReplayDataset, measure
from aegis.persistence.suggestions import SuggestionArgumentError, SuggestionConflictError
from aegis.services.calibration import RuleSample, calibration_report, collect_rule_hits
from aegis.services.review_queue import case_ids, pending_items
from aegis.services.threshold_suggestions import ThresholdSuggestionService
from aegis.storage.store import geo_query_port, rule_library_port

log = logging.getLogger("aegis.api")


class DrillRequest(BaseModel):
    scenario: str = Field(default="surge", pattern="^(surge|normal)$")
    ticks: int = Field(default=2, ge=1, le=50)
    region_code: str | None = Field(default=None, pattern=r"^[0-9A-Z]{6,24}$")


class SimulationRequest(BaseModel):
    """泥石流次生灾害推演入参：情景由调用方给（画布/演练），不臆造锚定遥测的默认。"""

    rain_base_mm: float = Field(default=40.0, ge=0, le=500)
    rain_spread_mm: float = Field(default=25.0, ge=0, le=200)
    scenarios: int = Field(default=8, ge=1, le=64)
    rounds: int = Field(default=5, ge=1, le=24)
    playbook: str = Field(default="转移", pattern="^(转移|封路|不行动)$")
    token_budget: int = Field(default=200_000, ge=1, le=10_000_000)
    seed: int = Field(default=20261011)


class SuggestionConfirm(BaseModel):
    """人工确认阈值建议的载荷：**数字由确认的人给**。

    反馈智能体交出来的是一条理由（`threshold_suggestion` 是一段文本），把它解析成阈值
    就等于让一次 LLM 输出直接改动判据，而 §3.5 明写着"只进建议、落地走人工确认"。
    `expected_version` 必填：确认要对着"我看见的那一版"下手，版本已经被人换过就该重新对照，
    而不是让三周前打开的那一屏覆盖掉别人刚改的判断。
    `decided_by` 不接受默认值——接口不替人编一个签字人（与 D9 核签那条同源）。
    """

    model_config = ConfigDict(extra="forbid")

    rule_id: str = Field(min_length=3, max_length=48)
    metric: str = Field(min_length=1, max_length=64)
    threshold: float = Field(ge=0, le=1_000_000)
    expected_version: int = Field(ge=1)
    decided_by: str = Field(min_length=1, max_length=64)
    note: str = Field(default="", max_length=500)


class SuggestionReject(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decided_by: str = Field(min_length=1, max_length=64)
    note: str = Field(default="", max_length=500)


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


class ReportIn(BaseModel):
    """群防群治/巡查上报（Web 表单）。文本由平台结构化后进规则引擎。"""

    reporter: str = Field(min_length=2, max_length=64)
    region_code: str = Field(pattern=r"^[0-9A-Z]{6,24}$")
    hazard_hint: str | None = Field(default=None, max_length=64)
    note: str = Field(min_length=4, max_length=2_000)
    lat: float | None = Field(default=None, ge=-90, le=90)
    lon: float | None = Field(default=None, ge=-180, le=180)

    @field_validator("reporter", "region_code", "hazard_hint", "note", mode="before")
    @classmethod
    def _trim(cls, value: object) -> object:
        """先裁首尾空白再判长度。

        不裁的话 `"    "`（4 个空格）能过 `min_length=4`，一路走到解析腿才被
        `semantic_parser.parse` 的 `ValueError("待解析文本为空")` 拦下——那是个裸 ValueError，
        FastAPI 不认，出口就成了 **HTTP 500 + Internal Server Error**。真机在表单里敲四个空格
        就是这么撞的：现场看到的是一句"服务器错误"，而事实是"你没写正文"。
        """
        return value.strip() if isinstance(value, str) else value


def get_container(request: Request) -> PlatformContainer:
    container: PlatformContainer | None = getattr(request.app.state, "container", None)
    if container is None:
        raise HTTPException(status_code=503, detail="平台尚未就绪")
    return container


def _presented_token(headers: Mapping[str, str]) -> str:
    """从请求头取服务令牌：接受 `X-Aegis-Token` 与 `Authorization: Bearer` 两种写法，取到一个算一个。

    为什么必须两个都认：只认一个头名时，"配了令牌"与"真的在拦"是两件事——本仓的智能体 REST 客户端
    与通用 MCP 客户端发的都是 Bearer，而服务端只读 X-Aegis-Token，结果是配置打开后合法客户端被一律 401、
    或者反过来（取决于谁先改）配了等于没配。两个面（数据 API 与 MCP 工具面）共用这一个函数，
    免得它们各自演化出第二套口径。
    """
    direct = headers.get("x-aegis-token") or ""
    if direct:
        return direct
    scheme, _, value = (headers.get("authorization") or "").partition(" ")
    return value.strip() if scheme.lower() == "bearer" else ""


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    # 链路追踪在每个进程各装配一次（uvicorn workers>1 时子进程也走这里）：
    # 端点由 OTEL_EXPORTER_OTLP_* 决定，未配置就是"只记本地不上报"，不阻断启动。
    telemetry.init_telemetry(deployment_environment=settings.env)
    container: PlatformContainer = getattr(app.state, "container", None) or create_container(settings)
    app.state.container = container
    await container.start(with_mock_agents=settings.env != "prod", with_ingest_loop=True)
    # MCP 会话管理器必须随进程跑起来：Starlette 的 Mount 不会替子应用执行 lifespan。
    # 两条本机实测出来的坑，决定了这里为什么长这样：
    # ① `session_manager` 只在 `streamable_http_app()` 被调用之后才存在，提前 .run() 直接 RuntimeError；
    # ② 自己去 `async with server.session_manager.run()` 会撞上 anyio 的
    #    "Attempted to exit a cancel scope that isn't the current task's current cancel scope"——
    #    那个会话管理器要在【它自己子应用的 router lifespan】里生灭。所以这里跑子应用的 lifespan_context，
    #    不去驱动它的内部对象。生命周期：跟着容器生，容器停完之后灭（在途工具调用不至于被抽走队列）。
    async with AsyncExitStack() as stack:
        if container.mcp_server is not None:
            app.state.mcp_inner = container.mcp_server.streamable_http_app(streamable_http_path="/")
            await stack.enter_async_context(app.state.mcp_inner.router.lifespan_context(app.state.mcp_inner))
        try:
            yield
        finally:
            await container.shutdown()
            # 先停产生跨度的主体，再刷跨度：反了会把仍在生成的跨度丢在关闭之后
            telemetry.shutdown_telemetry()


def _geo_unavailable() -> HTTPException:
    """几何查询不可用时的回答：说清缺什么，而不是留一句"不支持"让人猜。

    选 503 而不是 404：路由存在、能力也存在，只是当前存储后端不提供几何算子——
    调用方据此换配置就能修，按"没有这个接口"处理就会走错方向。
    """
    return HTTPException(
        status_code=503,
        detail={
            "code": "E_GEO_UNAVAILABLE",
            "message": "当前存储后端不提供几何查询（半径/轨迹面）",
            "requires": "AEGIS_STORE_BACKEND=postgres（PostGIS）",
        },
    )


def _accuracy_unavailable() -> HTTPException:
    """库侧回放不可用时的回答：算不出配对就不要给一份"看起来正常"的报表。

    准确率的配对要在库里做（真值表与 `warnings` 同一时区轴上比较）；内存 store 没有这条
    通路，若在 HTTP 层另写一份配对阵列，就有了第二套"预警准确率"口径。
    """
    return HTTPException(
        status_code=503,
        detail={
            "code": "E_ACCURACY_UNAVAILABLE",
            "message": "当前存储后端不提供库侧准确率回放（真值与已发布预警的配对在库里算）",
            "requires": "AEGIS_STORE_BACKEND=postgres（PostgreSQL + 真值表 warning_truth_labels）",
        },
    )


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

    @app.middleware("http")
    async def enforce_service_token(request: Request, call_next: Any) -> Any:
        """§4.8.1：配了 `service_token` 就整个业务 API 面都要带凭据（常量时间比对）；没配则照旧开放。

        为什么用中间件而不是逐条路由挂 `dependencies=`：那种写法已经证明会漏——29 条业务路由里
        只有 11 条挂了闸，`/api/v1/warnings`、`/api/v1/rules/*`、`/api/v1/reports`、
        `/api/v1/simulation/run` 与整条 workflow/assistant 路由在"配了令牌"的部署里其实谁都能读写，
        而文档说的"配了令牌就在拦"只在挂上的那几条上成立。一条口径只留一个执行点：
        `/api/` 之下默认全要凭据，新增端点不会因为忘了挂而裸奔。
        运维探针（`/healthz`、`/readyz`）、Prometheus 抓取（`/metrics`）与文档不在 `/api/` 下，
        天然开放；`/mcp/` 工具面在 ASGI 包层里自闸（同一把令牌、同一个 `_presented_token`）。
        CORS 预检不带凭据也不该带，按 `OPTIONS` 放行给 CORSMiddleware 处理。
        """
        if not cfg.service_token or request.method == "OPTIONS" or not request.url.path.startswith("/api/"):
            return await call_next(request)
        presented = _presented_token(request.headers)
        if not presented or not secrets.compare_digest(presented, cfg.service_token):
            return JSONResponse(
                status_code=401,
                content={
                    "detail": {
                        "code": "E_UNAUTHORIZED",
                        "message": "业务 API 需要有效服务令牌（X-Aegis-Token 或 Authorization: Bearer）",
                    }
                },
            )
        return await call_next(request)

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
        degraded = [state.name for state in rows if state.enabled and state.degradation_reason()]
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

    @app.post("/api/v1/knowledge/cases", tags=["knowledge"], responses={503: {"description": "知识层未装配"}})
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

    @app.get(
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

    @app.get(
        "/api/v1/accuracy/replay",
        tags=["metrics"],
        responses={503: {"description": "存储后端不支持库侧回放"}, 422: {"description": "回放条件不合法"}},
    )
    async def accuracy_replay(
        ctn: PlatformContainer = Depends(get_container),
        since: str = Query(min_length=4, max_length=40, description="回放窗起始时刻（必须带时区）"),
        until: str | None = Query(default=None, max_length=40, description="回放窗结束时刻，缺省到当前"),
        window_seconds: int = Query(default=DEFAULT_WINDOW_SECONDS, ge=1, le=MAX_WINDOW_SECONDS),
        region_code: str | None = Query(default=None, pattern=r"^[0-9A-Z]{6,24}$"),
        kind: Literal["field", "synthetic", "unspecified"] = Query(default="unspecified"),
        accuracy_target: float | None = Query(default=None, gt=0.0, le=1.0),
        min_field_cases: int = Query(default=MIN_FIELD_CASES, ge=1, le=10_000),
    ) -> dict[str, Any]:
        """预警准确率的库侧回放出口：与 `scripts/accuracy_replay` 同一份算式、同一个判据。

        这里**不做任何算术**。`status/indicator/official_accuracy` 全部来自
        `persistence/replay.measure`——准确率只允许有一处定义，HTTP 层再算一遍就是第二套口径。

        `kind` 默认 `unspecified`：数据集没自己声明"来自现场标注"时官方准确率恒为 None。
        合成数据集或样本不足也一样——报表里的 `provenance_warning` 会把原因带到界面上，
        免得一次算术演练被读成"≥80% 达成"。
        """
        store = accuracy_replay_port(ctn.store)
        if store is None:
            raise _accuracy_unavailable()
        since_moment = parse_moment(since, field="since")
        until_moment = None if until is None else parse_moment(until, field="until")
        if until_moment is not None and until_moment <= since_moment:
            raise AccuracyArgumentError("回放窗结束时刻必须晚于起始时刻", detail={"since": since, "until": until})
        window = check_lead_window(window_seconds)

        cases = await store.accuracy_replay_cases(
            since=since_moment,
            until=until_moment,
            window_seconds=window,
            region_code=region_code,
        )
        report = measure(
            ReplayDataset(
                cases=tuple(cases),
                kind=kind,
                source="store:warning_truth_labels",
                note="库侧配对回放：真值表与已落库 warnings 在同一时区轴上配对",
            ),
            target=accuracy_target,
            min_field_cases=min_field_cases,
        )
        return {
            "query": {
                "since": since_moment.isoformat(),
                "until": None if until_moment is None else until_moment.isoformat(),
                "region_code": region_code,
                "cases": len(cases),
            },
            "window_seconds": window,
            **report.as_dict(),
        }

    @app.get("/api/v1/stations", tags=["data"])
    async def list_stations(
        ctn: PlatformContainer = Depends(get_container),
        region_code: str | None = Query(default=None, pattern=r"^[0-9A-Z]{6,24}$"),
        limit: int = Query(default=200, ge=1, le=1_000),
    ) -> dict[str, Any]:
        """站点清单：维表里登记的站（带名称与坐标）+ 报过数但还没登记的站。

        坐标只有在维表真的写了才返回：地图宁可把该站列进"未定位"，也不按区划中心或站点编号
        猜一个经纬度——演示里看不出差别，验收时被当成实测坐标就是事故。
        `region_code` 的格式与 `monitoring_stations` 的 CHECK 同口径，写错了直接 422，
        免得一次笔误变成"这个区一个站都没有"。
        """
        rows = await ctn.store.list_stations(region_code=region_code, limit=limit)
        return {"count": len(rows), "items": rows}

    @app.get("/api/v1/geo/stations-within", tags=["data"])
    async def geo_stations_within(
        ctn: PlatformContainer = Depends(get_container),
        lon: float = Query(..., ge=-180.0, le=180.0),
        lat: float = Query(..., ge=-90.0, le=90.0),
        radius_m: float = Query(..., gt=0.0, le=float(geo.MAX_RADIUS_M)),
        limit: int = Query(default=50, ge=1, le=geo.MAX_LIMIT),
    ) -> dict[str, Any]:
        """半径内的站点，按距离升序。距离由 PostGIS 的 geography 口径算，单位米。

        这一路不做内存近似：同一句"这个站在不在范围内"若存在两套口径，
        看不出差别的是验收方，付代价的是被漏掉的站点指挥员。
        """
        port = geo_query_port(ctn.store)
        if port is None:
            raise _geo_unavailable()
        lon, lat = geo.check_point(lon, lat)
        radius = geo.check_radius(radius_m)
        items = await port.stations_within(lon=lon, lat=lat, radius_m=radius, limit=limit)
        query = {"lon": lon, "lat": lat, "radius_m": radius, "limit": limit}
        return {"driver": "postgis", "query": query, "count": len(items), "items": items}

    @app.get("/api/v1/geo/stations-in-polygon", tags=["data"])
    async def geo_stations_in_polygon(
        ctn: PlatformContainer = Depends(get_container),
        polygon: str = Query(..., min_length=14, description="POLYGON/MULTIPOLYGON 的 WKT（SRID=4326，经度在前）"),
        region_code: str | None = Query(default=None, pattern=r"^[0-9A-Z]{6,24}$"),
    ) -> dict[str, Any]:
        """轨迹面内的站点：一张灾害面罩住哪些站，决定靶向发布给谁。"""
        port = geo_query_port(ctn.store)
        if port is None:
            raise _geo_unavailable()
        # 参数在进入存储之前就按 geo 的唯一口径判掉：`QueryArgumentError` 的语义就是
        # "调用方的错，尚未触达数据库"，交给数据库报错会把 4xx 变成一次真实查询。
        wkt = geo.check_polygon(polygon)
        items = await port.stations_in_polygon(polygon_wkt=wkt, region_code=region_code)
        return {
            "driver": "postgis",
            "query": {"polygon": wkt, "region_code": region_code},
            "count": len(items),
            "items": items,
        }

    @app.get("/api/v1/geo/hazard-trace", tags=["data"])
    async def geo_hazard_trace(
        ctn: PlatformContainer = Depends(get_container),
        polygon: str = Query(..., min_length=14),
        since: datetime = Query(..., description="时间窗起点，必须带时区"),
        until: datetime | None = Query(default=None),
        hazard_type: str | None = None,
    ) -> dict[str, Any]:
        """灾害轨迹面汇总：面内站数、面内读数条数、面内预警条数（含预警编号）。

        `since`/`until` 的裸时间与逆序窗由 `persistence.geo` 直接拒绝——
        少写时区会被按本地时区猜一遍，轨迹统计就整体平移几小时，这种错最难发现。
        """
        port = geo_query_port(ctn.store)
        if port is None:
            raise _geo_unavailable()
        wkt = geo.check_polygon(polygon)
        geo.check_window(since, until)
        summary = await port.hazard_trace_summary(polygon_wkt=wkt, since=since, until=until, hazard_type=hazard_type)
        return {
            "driver": "postgis",
            "query": {"polygon": wkt, "since": since, "until": until, "hazard_type": hazard_type},
            "summary": summary,
        }

    @app.get("/api/v1/rules", tags=["rules"])
    async def list_rules(ctn: PlatformContainer = Depends(get_container)) -> dict[str, Any]:
        """当前生效的触发条件集与它的出处。

        考核指标 1（识别 ≥5 类触发条件）的证据就是这一份：条数、灾种覆盖、版本号、
        标定依据与审核人都在，未标定的那些如实写着"未经现场标定"。
        `provenance.source` 区分"内置种子"与"规则库版本"，`error`/`rejected` 说明有没有换版失败。
        """
        engine = ctn.rule_engine
        rules = [rule.as_row() for rule in engine.rules]
        return {
            "provenance": engine.describe(),
            "load_error": ctn.rulebook_error,
            "rejected_rows": ctn.rulebook_rejected,
            "trigger_condition_kinds": len({condition.metric for rule in engine.rules for condition in rule.conditions}),
            "hazards_covered": sorted({rule.hazard_type.value for rule in engine.rules}),
            "items": rules,
        }

    @app.get("/api/v1/rules/versions", tags=["rules"], responses={503: {"description": "存储后端不提供规则库"}})
    async def list_rule_versions(
        ctn: PlatformContainer = Depends(get_container),
        status: Literal["draft", "active", "retired", "all"] = Query(default="all"),
        hazard_type: HazardType | None = None,
        limit: int = Query(default=200, ge=1, le=500),
    ) -> dict[str, Any]:
        """规则库的版本历史（含草稿与退役）：只有 PostgreSQL 形态有这一面。

        内存读视图没有"版本化阈值"这件事，内置种子就是唯一版本——所以这里如实 503，
        而不是把当前生效集当作"v1 版本清单"再报一遍。
        """
        port = rule_library_port(ctn.store)
        if port is None:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "E_RULEBOOK_UNAVAILABLE",
                    "message": "当前存储后端不提供规则库版本面",
                    "requires": "AEGIS_STORE_BACKEND=postgres（表 trigger_rules）",
                },
            )
        rows = await port.trigger_rules(status=status, hazard_type=None if hazard_type is None else hazard_type.value, limit=limit)
        return {"status": status, "count": len(rows), "items": rows}

    @app.get("/api/v1/rules/calibration", tags=["rules"])
    async def rules_calibration(
        ctn: PlatformContainer = Depends(get_container),
        limit: int = Query(default=2_000, ge=1, le=10_000, description="回看多少条链路台账样本"),
    ) -> dict[str, Any]:
        """分规则命中与误报对照（批次 C3）。**只出建议，不改阈值。**

        命中次数取自平台链路台账（实测）；精度需要现场真值配对（批次 E1），缺就写
        `not_measured`。报表里 `auto_applied` 恒为 false——阈值生效的唯一路径是
        人工确认（`POST /api/v1/feedback/suggestions/{id}/confirm`）把新版本写进
        `trigger_rules`，历史读 `/api/v1/rules/versions`。
        """
        rows = list(ctn.store.chains.latest(int(limit)))
        hits = collect_rule_hits(rows)
        samples = {
            rule_id: RuleSample(rule_id=rule_id, hits=count)
            for rule_id, count in hits.items()
            if ctn.rule_engine.rule_by_id(rule_id) is not None
        }
        report = calibration_report(ctn.rule_engine.rules, samples, dataset_kind="unspecified", dataset_cases=0)
        return {
            **report.as_dict(),
            "ledger_rows": len(rows),
            "report_rulebook": ctn.rule_engine.describe(),
            "off_bookrule_hits": {key: value for key, value in hits.items() if ctn.rule_engine.rule_by_id(key) is None},
        }

    def _suggestion_service(ctn: PlatformContainer) -> ThresholdSuggestionService:
        """三个建议端点共用这一句"这条腿装了没有"，因为漏装的表现是一样的静默。"""
        service = ctn.suggestions
        if service is None or not service.available:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "E_SUGGESTION_LEDGER_UNAVAILABLE",
                    "message": "当前装配没有阈值建议台账",
                    "requires": "AEGIS_STORE_BACKEND=postgres（表 threshold_suggestions 与 trigger_rules 版本面）",
                },
            )
        return service

    @app.get(
        "/api/v1/feedback/suggestions",
        tags=["rules"],
        responses={503: {"description": "存储后端没有建议台账"}},
    )
    async def list_threshold_suggestions(
        ctn: PlatformContainer = Depends(get_container),
        status: Literal["pending_human_review", "confirmed", "rejected", "all"] = Query(default="pending_human_review"),
        limit: int = Query(default=100, ge=1, le=500),
    ) -> dict[str, Any]:
        """§3.5 的后半截从此有了读者：建议不再只是段注里的一段话，而是能列出来待办的一行。

        默认只看待确认的。`all` 是给审计看的（谁在什么时候把哪条驳回成了什么）。
        """
        service = _suggestion_service(ctn)
        rows = await service.list(status=status, limit=limit)
        return {"status": status, "count": len(rows), "items": rows}

    @app.post(
        "/api/v1/feedback/suggestions/{suggestion_id}/confirm",
        tags=["rules"],
        responses={409: {"description": "这条建议已被裁决，或规则不是提交时看到的那一版"}, 503: {"description": "存储后端没有建议台账"}},
    )
    async def confirm_threshold_suggestion(
        suggestion_id: str,
        payload: SuggestionConfirm,
        ctn: PlatformContainer = Depends(get_container),
    ) -> dict[str, Any]:
        """人工确认落地：规则库长出一个新版本，建议同时销账，两件事在同一笔事务里。

        数字由确认的人给，不是由模型给——反馈智能体交出来的是一条**理由**（`threshold_suggestion`
        是一段文本），把它解析成阈值就等于让一次 LLM 输出直接改动判据，而 §3.5 明写着"只进建议"。
        `expected_version` 是防"凭三周前看到的 v2 覆盖掉别人刚改的 v5"。
        `engine_loaded` 单独说：库里版本已经落，而内存里那台引擎有没有换上，是两件事。
        """
        service = _suggestion_service(ctn)
        try:
            return await service.confirm(
                suggestion_id,
                rule_id=payload.rule_id,
                metric=payload.metric,
                threshold=payload.threshold,
                decided_by=payload.decided_by,
                note=payload.note,
                expected_version=payload.expected_version,
            )
        except SuggestionConflictError as exc:
            raise HTTPException(status_code=409, detail={"code": "E_SUGGESTION_DECIDED", "message": exc.message, **exc.detail}) from exc
        except SuggestionArgumentError as exc:
            raise HTTPException(status_code=422, detail={"code": "E_SUGGESTION_ARG", "message": str(exc), **exc.detail}) from exc

    @app.post(
        "/api/v1/feedback/suggestions/{suggestion_id}/reject",
        tags=["rules"],
        responses={409: {"description": "这条建议已被裁决"}, 503: {"description": "存储后端没有建议台账"}},
    )
    async def reject_threshold_suggestion(
        suggestion_id: str,
        payload: SuggestionReject,
        ctn: PlatformContainer = Depends(get_container),
    ) -> dict[str, Any]:
        """驳回也要留痕：一条被驳回的建议如果只从队列里消失，下一个人无法知道它被看过想过。"""
        service = _suggestion_service(ctn)
        try:
            return await service.reject(suggestion_id, decided_by=payload.decided_by, note=payload.note)
        except SuggestionConflictError as exc:
            raise HTTPException(status_code=409, detail={"code": "E_SUGGESTION_DECIDED", "message": exc.message, **exc.detail}) from exc
        except SuggestionArgumentError as exc:
            raise HTTPException(status_code=422, detail={"code": "E_SUGGESTION_ARG", "message": str(exc), **exc.detail}) from exc

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

    @app.post("/api/v1/reports", tags=["ingest"], responses={503: {"description": "解析服务未装配"}})
    async def submit_report(
        payload: ReportIn,
        ctn: PlatformContainer = Depends(get_container),
    ) -> dict[str, Any]:
        """群防群治人工上报：文本 → 三路融合解析 → **与监测事件同一条链路**。

        路由只做出口翻译。解析、定级、预警生成、触达与量测全在
        `PlatformContainer.submit_report` 一处——助手页"帮我上报"这里也调同一个入口，
        两条路径给出不同等级就是第二个"预警准确率"口径。

        上报坐标不改判据，只进证据链：`location` 原样回显，并作为一条 evidence 挂在触发命中上。
        """
        if ctn.parser is None:
            raise HTTPException(status_code=503, detail={"code": "E_PARSER_UNAVAILABLE", "message": "任务解析服务未装配"})
        location = None if payload.lat is None or payload.lon is None else (payload.lon, payload.lat)
        outcome = await ctn.submit_report(
            note=payload.note,
            region_code=payload.region_code,
            reporter=payload.reporter,
            hazard_hint=payload.hazard_hint,
            location=location,
        )
        return {"report": {"reporter": payload.reporter, "region_code": payload.region_code, "location": location}, **outcome}

    @app.get("/api/v1/review/pending", tags=["warning"])
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

    @app.get(
        "/api/v1/audit/chain/{trace_id}",
        tags=["ops"],
        responses={503: {"description": "持久层不是 postgres：台账直读面只在真库形态提供"}},
    )
    async def audit_chain(
        ctn: PlatformContainer = Depends(get_container),
        trace_id: str = Path(pattern=r"^trc_[0-9a-f]{16}$"),
    ) -> dict[str, Any]:
        """一条 trace 的链路台账原文（只追加、直读库），含各段的模型与 Prompt 版本凭证。

        与 `/api/v1/events` 的分工要说清：那边读的是**进程内**的链路读视图，重启后从空开始
        （待修缺陷"读视图不回填"）；这里读的是 `chain_runs` 表，所以事后仍能回答
        "这条预警当时是哪个模型、哪版 prompt 产的"。内存形态下如实 503——那份台账本来就随进程结束，
        回一份空清单会被读成"这条 trace 没发生过"。
        """
        load = getattr(ctn.store, "load_chain_audit", None)
        if load is None:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "E_AUDIT_UNAVAILABLE",
                    "message": "链路台账直读面只在 AEGIS_STORE_BACKEND=postgres 时提供；内存形态的台账随进程结束",
                },
            )
        events = await load(trace_id)
        return {"trace_id": trace_id, "store": ctn.settings.store_backend, "found": bool(events), "events": events}

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

    @app.get("/api/v1/agents/model-cards", tags=["agent"])
    async def model_cards(ctn: PlatformContainer = Depends(get_container)) -> dict[str, Any]:
        from aegis.services.model_cards import build_model_cards
        from aegis.services.prompt_registry import PromptRegistry

        cards = build_model_cards(ctn.settings, PromptRegistry.builtin())
        return {
            "cards": cards,
            "measured_columns": [],
            "llm_configured": ctn.llm_gateway is not None and ctn.llm_gateway.available,
            "note": "模型/版本来自真实路由配置与已迁入的版本化 prompt；训练集与评测分未做即 None，不冒充达标。",
        }

    @app.get("/api/v1/memory/shared", tags=["memory"])
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

    @app.post("/api/v1/memory/shared", tags=["memory"], status_code=201)
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

    @app.delete("/api/v1/memory/shared/{record_id}", tags=["memory"])
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

    @app.post("/api/v1/simulation/run", tags=["simulation"])
    async def simulation_run(payload: SimulationRequest) -> dict[str, Any]:
        """泥石流次生灾害推演沙盒（§3.6/§4.4）：确定性数值模型 → 概率矩阵/预案对比/时间线。

        演示/演练口径，不进任何 SLA 热路径；情景参数由调用方给定（画布/演练），概率/缓解系数为未实测初值，
        只保证模型单调与可复现，不声称现场达标。
        """
        from aegis.services.simulation import Sandbox, SimConfig

        cfg = SimConfig(
            scenarios=payload.scenarios,
            rounds=payload.rounds,
            rain_base_mm=payload.rain_base_mm,
            rain_spread_mm=payload.rain_spread_mm,
            playbook=payload.playbook,
            token_budget=payload.token_budget,
            seed=payload.seed,
        )
        out = Sandbox().run(cfg).as_dict()
        out["mode"] = "demo_sandbox"
        out["note"] = "概率/暴露/缓解系数为未实测初值；情景参数由调用方给定，不声称现场达标。"
        return out

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
            # 一开流就冲一帧注释：浏览器 EventSource 要收到第一个字节才触发 open，
            # 而空闲流原本要等 `_SSE_KEEPALIVE_SECONDS`（15s）才有第一个字节。
            # 真机量到 open 落在 15.27s——也就是说每次进页面都有 15 秒顶着一句
            # "事件流重连中"，可它其实连得好好的。假告警比没告警更坏。
            yield ": open\n\n"
            idle = 0.0
            try:
                while not ctn.stopping and not await request.is_disconnected():
                    try:
                        item = await asyncio.wait_for(queue.get(), timeout=_DISCONNECT_POLL_SECONDS)
                    except TimeoutError:
                        idle += _DISCONNECT_POLL_SECONDS
                        if idle >= _SSE_KEEPALIVE_SECONDS:
                            idle = 0.0
                            # 心跳必须是**看得见的一帧**，不能是注释帧。
                            # 真机把后端进程杀掉之后：/healthz 立刻不可达、取数一直失败，
                            # 而页面上的 EventSource 仍然认为自己"已连接"——浏览器不知道上游断了，
                            # 注释帧 JS 也永远看不到，于是那个徽标在最需要它说话的时候撒谎。
                            yield f"data: {json.dumps({'type': 'heartbeat', 'ts': utc_now().isoformat()}, ensure_ascii=False)}\n\n"
                        continue
                    yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
            finally:
                for sub in subs:
                    await sub.cancel()

        return StreamingResponse(_gen(), media_type="text/event-stream")

    app.include_router(build_workflow_router())
    if cfg.assistant_enabled:
        # 关掉的语义是"这个出口不存在"（404），而不是"存在但一律 503"：
        # 配置面与路由面必须给外部同一个事实，否则 `/healthz` 说活着、路由说没有。
        app.include_router(build_assistant_router())

    @app.exception_handler(AegisError)
    async def _aegis_error_handler(_: Request, exc: AegisError) -> Response:
        return Response(
            status_code=422,
            content=json.dumps({"error": exc.code.value, "message": exc.message, "detail": exc.detail}),
            media_type="application/json",
        )

    @app.exception_handler(QueryArgumentError)
    async def _query_argument_handler(_: Request, exc: QueryArgumentError) -> Response:
        """持久层的"参数被拒，尚未触达数据库"与 `AegisError` 不同源，所以单独收口。

        不接这一条的话，几何/向量查询的非法参数会以 500 出去：调用方以为服务端坏了，
        实际该改的是自己传的那个半径/时间窗。
        """
        return Response(
            status_code=422,
            content=json.dumps({"error": "E_QUERY_ARGUMENT", "message": exc.message, "detail": exc.detail}),
            media_type="application/json",
        )

    # MCP 工具面（§4.2 / D4）：配置关着就【不挂这条路径】（404 = 这条能力在此进程里不存在），
    # 挂着但内层应用没建起来则 503 并说清缺什么——与几何查询同一套"路由在、能力不在"的口径。
    # 内层 ASGI 应用由 lifespan 建（SDK 的会话管理器只能在 `streamable_http_app()` 之后运行），
    # 所以这里只做分发与鉴权，不在请求路径上惰性造它。
    if cfg.mcp_enabled:
        # 挂载点必须是个非空的绝对路径：配成 "/" 会把整张 API 表挡在工具面后面，而表现是
        # "所有端点突然要令牌"——这种错在启动期就能判死，不留到运行期靠人猜。
        if not cfg.mcp_mount_path.startswith("/") or cfg.mcp_mount_path.strip("/") == "":
            raise ValueError(f"AEGIS_MCP_MOUNT_PATH 要是一个非空的绝对路径（如 /mcp），收到 {cfg.mcp_mount_path!r}")

        async def mcp_asgi(scope: Any, receive: Any, send: Any) -> None:
            def _reject(status: int, code: str, message: str) -> Response:
                return Response(
                    status_code=status,
                    content=json.dumps({"error": code, "message": message}, ensure_ascii=False),
                    media_type="application/json",
                )

            if cfg.service_token:
                headers = {str(key.decode("latin-1")).lower(): value.decode("latin-1") for key, value in scope.get("headers") or []}
                presented = _presented_token(headers)
                if not presented or not secrets.compare_digest(presented, cfg.service_token):
                    await _reject(
                        401,
                        "E_UNAUTHORIZED",
                        "MCP 工具面需要有效服务令牌（X-Aegis-Token 或 Authorization: Bearer），与数据 API 同一鉴权面",
                    )(scope, receive, send)
                    return
            inner = getattr(app.state, "mcp_inner", None)
            if inner is None:
                await _reject(
                    503,
                    "E_MCP_UNAVAILABLE",
                    "MCP 已启用但工具面没装配起来：需要可选依赖 mcp（本机 `uv sync --extra dev --extra mcp`，"
                    "extras 是整表替换所以要列全；镜像里已含），且进程要走过 lifespan；未装期间智能体取数仍走 REST 兼容面",
                )(scope, receive, send)
                return
            await inner(scope, receive, send)

        app.mount(cfg.mcp_mount_path, mcp_asgi)

    return app


#: 断连轮询间隔：ASGI 不推送"客户端已断开"事件，只能轮询；0.25s 足够快且不必空转。
_DISCONNECT_POLL_SECONDS = 0.25
#: 空闲多久补一帧注释帧，防止中间设备掐掉长连接（不是业务事件，客户端会忽略）。
_SSE_KEEPALIVE_SECONDS = 15.0
