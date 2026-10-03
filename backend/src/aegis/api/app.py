"""HTTP API（L4 服务层对外接口 + L5 Web 的数据源）。

约定：读接口占多数且不改状态；写接口只有三类——演练触发（/drill）、人工上报（/reports）、
案例入库（/knowledge/cases，图谱写入的唯一生产调用点，因此刻意不挂在任何自动链路上）。
智能体不通过 HTTP 交互，一律走总线契约（见 contracts/）。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from aegis import __version__
from aegis.api.assistant_api import build_router as build_assistant_router
from aegis.api.workflow_api import build_router as build_workflow_router
from aegis.bus import subjects
from aegis.config import Settings, get_settings
from aegis.container import PlatformContainer, create_container
from aegis.domain.enums import HazardType
from aegis.domain.messages import AgentMessage, TelemetryReading
from aegis.errors import AegisError
from aegis.knowledge.cases import HazardCase
from aegis.observability import telemetry
from aegis.persistence import geo
from aegis.persistence.accuracy import (
    DEFAULT_WINDOW_SECONDS,
    MAX_WINDOW_SECONDS,
    accuracy_replay_port,
    check_lead_window,
    parse_moment,
)
from aegis.persistence.errors import AccuracyArgumentError, QueryArgumentError
from aegis.persistence.replay import MIN_FIELD_CASES, ReplayDataset, measure
from aegis.services.calibration import RuleSample, calibration_report, collect_rule_hits
from aegis.storage.store import geo_query_port, rule_library_port

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
    # 链路追踪在每个进程各装配一次（uvicorn workers>1 时子进程也走这里）：
    # 端点由 OTEL_EXPORTER_OTLP_* 决定，未配置就是"只记本地不上报"，不阻断启动。
    telemetry.init_telemetry(deployment_environment=settings.env)
    container: PlatformContainer = getattr(app.state, "container", None) or create_container(settings)
    app.state.container = container
    await container.start(with_mock_agents=settings.env != "prod", with_ingest_loop=True)
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
        人工审核后把新版本写进 `trigger_rules`（见 `/api/v1/rules/versions`）。
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

    return app


#: 断连轮询间隔：ASGI 不推送"客户端已断开"事件，只能轮询；0.25s 足够快且不必空转。
_DISCONNECT_POLL_SECONDS = 0.25
#: 空闲多久补一帧注释帧，防止中间设备掐掉长连接（不是业务事件，客户端会忽略）。
_SSE_KEEPALIVE_SECONDS = 15.0
