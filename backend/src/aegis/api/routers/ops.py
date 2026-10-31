"""状态与量测出口：进程活着吗（healthz / readyz）、哪条腿是瘸的（integrations）、
量测三格（时延账本 / Prometheus / 准确率回放），以及一条 trace 的链路台账原文（audit）。

这些端点的共同点是"只读平台自身的运行事实"，都不产生业务状态。
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response

from aegis import __version__
from aegis.api.deps import get_container
from aegis.container import PlatformContainer
from aegis.persistence.accuracy import (
    DEFAULT_WINDOW_SECONDS,
    MAX_WINDOW_SECONDS,
    accuracy_replay_port,
    check_lead_window,
    parse_moment,
)
from aegis.persistence.errors import AccuracyArgumentError
from aegis.persistence.replay import MIN_FIELD_CASES, ReplayDataset, measure


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


def build_router() -> APIRouter:
    router = APIRouter()

    @router.get("/healthz", tags=["ops"])
    async def healthz() -> dict[str, Any]:
        return {"status": "ok", "version": __version__, "contract": "agent_message.v1"}

    @router.get("/readyz", tags=["ops"], responses={503: {"description": "总线未连接，平台尚未就绪"}})
    async def readyz(ctn: PlatformContainer = Depends(get_container)) -> dict[str, Any]:
        ready = ctn.transport.connected
        if not ready:
            raise HTTPException(status_code=503, detail="总线未连接")
        online = ctn.registry.online_count
        return {
            "status": "ready",
            "bus": ctn.transport.name,
            # 生产环境默认不拉本进程内的智能体（mock 只在非 prod 启动，真智能体可能外部部署）。
            # 0 在线不代表总线不健康，但所有协同事务都会以 no_agent 降级——这两个事实必须一起读到，
            # 否则探针给的是"一切正常"，值班要到第一条预警卡住才发现没人接单。
            "agents_online": online,
            "agent_runtime": ctn.agent_runtime_state,
            "agents_degraded": online == 0,
            "store": ctn.store.snapshot(),
            # D7 收口后链路只有一条编排路径，它的进程内资源与持久态恢复事实都得能被看见：
            # 开着的盘面（漏收盘就是内存泄漏）、留在内存里的实例（有上界，挤掉多少要记账）、
            # 以及启动恢复失败的原因（恢复计数在 instance_stats 的 recovered_* 里）。
            "chain": {
                **ctn.chain.stations.boards.snapshot(),
                **ctn.workflow.instance_stats,
                "recovery_error": ctn.workflow_recovery_error,
            },
        }

    @router.get("/api/v1/integrations", tags=["ops"])
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

    @router.get(
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

    @router.get(
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

    @router.get("/api/v1/metrics/latency", tags=["metrics"])
    async def latency(ctn: PlatformContainer = Depends(get_container)) -> dict[str, Any]:
        return ctn.latency_report()

    @router.get("/metrics", tags=["metrics"], response_class=Response)
    async def prometheus(ctn: PlatformContainer = Depends(get_container)) -> Response:
        return Response(content=ctn.exporter.collect(), media_type="text/plain; version=0.0.4")

    return router
