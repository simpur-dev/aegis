"""MCP 工具面装配（§4.2）：清单驱动、缺端口即不注册；`_mcp_*_source` 是各工具的“与 REST 同一句话”适配层。（P2 纯搬运：函数体逐字未改。）"""

from __future__ import annotations

from typing import Any, NamedTuple

from aegis.config import Settings
from aegis.integrations.state import IntegrationState


class McpAssembly(NamedTuple):
    """一次 MCP 装配的三件产出：能挂的 server、对外状态行、运行期台账。

    台账单独给一份引用是必要的：`IntegrationState.detail` 里的调用/失败/成因是装配时刻的快照，
    直接拿去回话就会把"这一小时有没有人调、调失败了几次"永远说成 0 与空串。
    状态面（`container.integration_status()`）据此每次重读，口径与 mqtt 腿同一形。
    """

    server: Any | None
    state: IntegrationState
    runtime: Any | None


def build_mcp(
    *,
    settings: Settings,
    store: object | None = None,
    retrieval: object | None = None,
    knowledge: object | None = None,
    memory: object | None = None,
    tracer: Any | None = None,
) -> McpAssembly:
    """装配 MCP 工具层（§4.2 / D4）：把平台既有能力按清单暴露成标准工具，本层不写业务。

    三件"没接上"分开记，因为运维要能分辨它们：
    - `disabled`：配置关着（默认）——不是故障，是没开；
    - `absent`：没装 `mcp` 可选依赖——装上就有；这期间智能体取数继续走 REST 兼容面（语义不变）；
    - 开着且装上，但某些端口没装配（例如没开检索）→ 那些工具【不注册】，缺席名单进 `detail.absent_tools`，
      而不是注册一个回空表的假工具。

    `mcp_tools` 里写了清单外的名字 → 装配期就 ValueError：一个不会被任何工具消费的旋钮，比启动失败贵得多
    （同 `store_backend=postgres` 而 DSN 空的那条口径）。这条校验在关闭状态下也做，避免"关着的时候配错、
    打开那天才炸"。

    `tracer`（装配点给的时延账本）让"工具即审计面（调用留 trace）"落到样本上：每次调用一条
    `mcp_tool_ms`，按工具的分位数在状态行里。不给 tracer 时工具面照常工作，只是那条线不进账本——
    记时延不是挂载工具面的前置条件，但没给它就别声称"调用留了 trace"。
    """
    from aegis.mcp_tools import exposed_names, mcp_available
    from aegis.mcp_tools.manifest import TOOL_MANIFEST

    requested = [name.strip() for name in settings.mcp_tools.split(",") if name.strip()] or None
    would_expose = exposed_names(requested, allow_write=settings.mcp_allow_write_tools)
    known = {spec.name for spec in TOOL_MANIFEST}
    if not settings.mcp_enabled:
        return McpAssembly(
            None,
            IntegrationState(
                name="mcp",
                enabled=False,
                driver="disabled",
                detail={"reason": "AEGIS_MCP_ENABLED=false：不挂载工具面，智能体取数走 REST 兼容面", "would_expose": would_expose},
            ),
            None,
        )
    if not mcp_available():
        return McpAssembly(
            None,
            IntegrationState(
                name="mcp",
                enabled=False,
                driver="absent",
                detail={
                    "reason": (
                        "未安装可选依赖 mcp（本机 `uv sync --extra dev --extra mcp`，"
                        "extras 是整表替换、已有的要列全；镜像见 deploy/Dockerfile）"
                    ),
                    "would_expose": would_expose,
                },
            ),
            None,
        )

    from aegis.mcp_tools.server import ToolSources, build_server

    sources = ToolSources(
        telemetry=_mcp_telemetry_source(store) if store is not None else None,
        retrieval=_mcp_retrieval_source(retrieval) if retrieval is not None else None,
        cases=_mcp_knowledge_source(knowledge, settings) if knowledge is not None else None,
        memory_search=_mcp_memory_search_source(memory) if memory is not None else None,
        memory_categories=_mcp_memory_categories_source(memory) if memory is not None else None,
        memory_write=_mcp_memory_write_source(memory) if memory is not None else None,
        simulation=_mcp_simulation_source(settings),
    )
    server, state = build_server(
        sources=sources,
        allowed_tools=requested,
        allow_write_tools=settings.mcp_allow_write_tools,
        server_name="aegis",
        tracer=tracer,
    )
    detail: dict[str, object] = {
        **state.as_dict(),
        "mount_path": settings.mcp_mount_path,
        "allow_write_tools": settings.mcp_allow_write_tools,
        "known_tools": sorted(known),
    }
    return McpAssembly(
        server,
        IntegrationState(name="mcp", enabled=True, driver="streamable-http", detail=detail),
        state,
    )


def _mcp_telemetry_source(store: object):
    async def _call(args: dict[str, Any]) -> dict[str, Any]:
        rows = store.telemetry.query(  # type: ignore[attr-defined]
            station_id=args.get("station_id") or None,
            metric=args.get("metric") or None,
            region_code=args.get("region_code") or None,
            since=args.get("since") or None,
            # 上下界与 `GET /api/v1/telemetry` 的 `limit: ge=1, le=5_000` 对齐：
            # 这里另写一个更小的天花板（曾写 2_000）= 客户端要 3000 行而平台静默给 2000 行，
            # 表现是"两张门面读数不一致"，而调用方看不出谁改了它的参数。
            limit=max(1, min(int(args.get("limit") or 200), 5_000)),
        )
        return {"count": len(rows), "items": [row.model_dump(mode="json") for row in rows]}

    return _call


def _mcp_retrieval_source(retrieval: object):
    async def _call(args: dict[str, Any]) -> dict[str, Any]:
        from aegis.retrieval.service import RetrievalQuery  # 延迟导入：默认部署不必带检索实现

        query = str(args.get("query") or "").strip()
        if not query:
            raise ValueError("retrieval.search 需要非空 query")
        k = max(1, min(int(args.get("k") or 5), 20))
        rerank = bool(args.get("rerank", True))
        outcome = await retrieval.retrieve(RetrievalQuery(text=query, k=k, rerank=rerank))  # type: ignore[attr-defined]
        return {"query": query, **outcome.as_dict(), "items": [doc.as_reference() for doc in outcome.docs]}

    return _call


def _mcp_knowledge_source(knowledge: object, settings: Settings):
    async def _call(args: dict[str, Any]) -> dict[str, Any]:
        from aegis.domain.enums import HazardType

        query = str(args.get("query") or "")
        hazard = args.get("hazard_type") or None
        if hazard is not None:
            try:
                hazard = HazardType(hazard).value
            except ValueError:
                raise ValueError(f"hazard_type 不在冻结枚举里，收到 {hazard!r}") from None
            if not query:
                # 与 `GET /api/v1/cases/recall` 同一句回退（那里是 `q or hazard_type.cn`）：
                # 两条门面问的是同一个 provider，查询词不能一套有、一套空。
                query = HazardType(hazard).cn
        matches = await knowledge.recall(  # type: ignore[attr-defined]
            query,
            hazard_type=hazard,
            region_code=args.get("region_code") or None,
            limit=max(1, min(int(args.get("limit") or 3), 20)),
            budget_ms=settings.knowledge_recall_budget_ms,
        )
        return {
            "count": len(matches),
            "degraded_count": sum(1 for match in matches if getattr(match, "degraded", False)),
            "budget_ms": settings.knowledge_recall_budget_ms,
            "items": [match.planning_brief() for match in matches],
        }

    return _call


def _mcp_memory_search_source(memory: object):
    async def _call(args: dict[str, Any]) -> dict[str, Any]:
        kind = args.get("kind") or None
        if kind is not None and kind not in ("assessment", "disagreement", "lesson"):
            raise ValueError(f"kind 只允许 assessment|disagreement|lesson，收到 {kind!r}")
        rows = await memory.read_shared(  # type: ignore[attr-defined]
            kind=kind,
            agent_id=args.get("agent_id") or None,
            region_code=args.get("region_code") or None,
            # 上界与 `GET /api/v1/memory/shared` 的 `limit: le=100` 对齐（默认值这里给 5 是取数门面的常态，
            # 但客户端要点更多时不许被静默截到一半——两张门面读同一个库，条数不一致没法解释）
            limit=max(1, min(int(args.get("limit") or 5), 100)),
        )
        return {"count": len(rows), "items": [row.model_dump(mode="json") for row in rows]}

    return _call


def _mcp_memory_categories_source(memory: object):
    """§4.3 四类记忆门面（情景/语义/知识 + 那条不外泄的工作记忆）经工具面读：三类各回独立状态。

    存在的理由是 §4.3 那句"绝不把『没接上』读成『没有相关记忆』"目前只有 REST 一个读者：
    外部客户端与智能体只能拿到 `memory.search` 那三个 shared kind，够不到 provider 面。
    字段口径与 `GET /api/v1/memory/categories` 一致（`connected` / `items` / `note` 原样带出），
    这里不折叠成一张 items 清单——一折叠，三种现场就又成了一种。
    """

    async def _call(args: dict[str, Any]) -> dict[str, Any]:
        from aegis.domain.enums import HazardType

        query = str(args.get("query") or "")
        if not query:
            # 与那条 GET 的 `min_length=1` 同口径：空查询在三条 provider 上各回一次"没命中"，
            # 看着像读到了东西而实际上谁也没被问；协议层拒掉比让它跑一趟更诚实。
            raise ValueError("query 不能为空（与 GET /api/v1/memory/categories 的 min_length=1 同一句）")
        if len(query) > 200:
            raise ValueError(f"query 长度上限 200，收到 {len(query)}")
        hazard = args.get("hazard_type") or None
        if hazard is not None:
            try:
                hazard = HazardType(hazard).value
            except ValueError:
                raise ValueError(f"hazard_type 不在冻结枚举里，收到 {hazard!r}") from None
        result = await memory.read_categories(  # type: ignore[attr-defined]
            query,
            region_code=args.get("region_code") or None,
            hazard_type=hazard,
            # 上界与该端点的 `limit: le=10` 对齐：两条门面读同一个 provider 同一个预算，条数不该差
            limit=max(1, min(int(args.get("limit") or 3), 10)),
        )
        return {
            "query": query,
            "driver": getattr(memory, "backend", "unknown"),
            "categories": result,
            "disconnected": sorted(name for name, entry in result.items() if not entry.get("connected")),
        }

    return _call


def _mcp_memory_write_source(memory: object):
    """唯一的写工具：record_id 与 valid_at 由服务端生成，客户端不能指定（不然它能伪造时间线）。"""

    async def _call(args: dict[str, Any]) -> dict[str, Any]:
        from aegis.domain.messages import new_memory_record_id, now_iso

        kind = args.get("kind") or ""
        if kind not in ("assessment", "disagreement", "lesson"):
            raise ValueError(f"kind 只允许 assessment|disagreement|lesson，收到 {kind!r}")
        record_id = new_memory_record_id()
        written = await memory.write_shared(  # type: ignore[attr-defined]
            kind=kind,
            agent_id=str(args.get("agent_id") or "mcp.client"),
            content=str(args.get("content") or ""),
            record_id=record_id,
            region_code=args.get("region_code") or None,
            disagreement=bool(args.get("disagreement")),
            valid_at=now_iso(),
        )
        return {"record_id": written or record_id, "kind": kind}

    return _call


def _mcp_simulation_source(settings: Settings):
    async def _call(args: dict[str, Any]) -> dict[str, Any]:
        from aegis.services.simulation import SimConfig, make_sandbox

        defaults = SimConfig()
        rain = args.get("rain_base_mm")
        seed = args.get("seed")
        cfg = SimConfig(
            scenarios=max(1, min(int(args.get("scenarios") or defaults.scenarios), 24)),
            rounds=max(1, min(int(args.get("rounds") or defaults.rounds), 24)),
            rain_base_mm=float(defaults.rain_base_mm if rain is None else rain),
            playbook=str(args.get("playbook") or defaults.playbook),
            token_budget=max(1_000, int(args.get("token_budget") or defaults.token_budget)),
            seed=int(defaults.seed if seed is None else seed),
        )
        # 工具面不接 LLM 网关（`build_mcp` 的实参里没有它），所以这一份永远是 `rule` 档；
        # 报告里的 `roles.policy` 说的就是这件事，不是配置读漏了。接网关属于 §4.2 那批消费方切换。
        out = (await make_sandbox(settings).run(cfg)).as_dict()
        out["mode"] = "demo_sandbox"
        out["note"] = "概率/暴露/缓解系数为未实测初值；情景参数由调用方给定，不声称现场达标。"
        return out

    return _call
