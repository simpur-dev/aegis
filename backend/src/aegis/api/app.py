"""HTTP API（L4 服务层对外接口 + L5 Web 的数据源）。

约定：读接口占多数且不改状态；写接口只有三类——演练触发（/drill）、人工上报（/reports）、
案例入库（/knowledge/cases，图谱写入的唯一生产调用点，因此刻意不挂在任何自动链路上）。
智能体不通过 HTTP 交互，一律走总线契约（见 contracts/）。

结构重构 P3 起，这个文件只做装配：CORS、服务令牌闸、异常收口、lifespan、MCP 挂载，
以及把 `routers/` 里按资源拆开的路由表挂上来。37 条端点本体一条不在这儿——
它们原本全挤在 `create_app` 的千行里，改一条要在无关的九百里翻定位。
"""

from __future__ import annotations

import json
import logging
import secrets
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from aegis import __version__
from aegis.api.assistant_api import build_router as build_assistant_router
from aegis.api.deps import presented_token
from aegis.api.routers import agents, data, drill, knowledge, memory, ops, rules, stream, warnings
from aegis.api.workflow_api import build_router as build_workflow_router
from aegis.config import Settings, get_settings
from aegis.container import PlatformContainer, create_container
from aegis.errors import AegisError
from aegis.observability import telemetry
from aegis.persistence.errors import QueryArgumentError
from aegis.workflow.errors import WorkflowAppError

log = logging.getLogger("aegis.api")


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
        # 草稿保存走 PUT、重命名走 PATCH：只放 GET/POST 时浏览器预检会先拦下，
        # 表现为"后端端点明明存在，页面上却只有 CORS 报错"。
        allow_methods=["GET", "POST", "PUT", "PATCH"],
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
        天然开放；`/mcp/` 工具面在 ASGI 包层里自闸（同一把令牌、同一个 `presented_token`）。
        CORS 预检不带凭据也不该带，按 `OPTIONS` 放行给 CORSMiddleware 处理。
        """
        if not cfg.service_token or request.method == "OPTIONS" or not request.url.path.startswith("/api/"):
            return await call_next(request)
        presented = presented_token(request.headers)
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

    # 端点本体在 `routers/` 的各资源模块里；这里的顺序就是 OpenAPI 的分组顺序。
    # 闸不挂在任何一条路由上：上面那道中间件对 `/api/` 整面生效，"新端点漏挂"结构上不可能发生。
    app.include_router(ops.build_router())
    app.include_router(data.build_router())
    app.include_router(knowledge.build_router())
    app.include_router(rules.build_router())
    app.include_router(drill.build_router())
    app.include_router(warnings.build_router())
    app.include_router(agents.build_router())
    app.include_router(memory.build_router())
    app.include_router(stream.build_router())
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

    @app.exception_handler(WorkflowAppError)
    async def _workflow_app_error_handler(_: Request, exc: WorkflowAppError) -> Response:
        """文档生命周期端点的统一错误体：`detail: {code, message, details}`。"""
        return JSONResponse(status_code=exc.status, content={"detail": exc.as_detail()})

    @app.exception_handler(RequestValidationError)
    async def _request_validation_handler(request: Request, exc: RequestValidationError) -> Response:
        """新工作流端点用统一错误体，旧端点保持 FastAPI 默认的 `detail: [...]`。

        旧端点的既有客户端与测试都按列表形状解析；统一错误模型不能靠改全局 422 形状来达成。
        """
        errors = jsonable_encoder(exc.errors())
        path = request.url.path
        if (
            path == "/api/v1/workflow/lineages"
            or path.startswith("/api/v1/workflow/lineages/")
            or path.startswith("/api/v1/workflow/versions/")
        ):
            code = (
                "invalid_schema_version" if any(error.get("type") == "invalid_schema_version" for error in errors) else "validation_failed"
            )
            return JSONResponse(
                status_code=422,
                content={"detail": {"code": code, "message": "请求校验失败", "details": {"errors": errors}}},
            )
        return JSONResponse(status_code=422, content={"detail": errors})

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
                presented = presented_token(headers)
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
