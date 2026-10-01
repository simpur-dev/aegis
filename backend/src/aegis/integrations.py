"""装配层的可选子系统：按配置组装、失败即降级、降级事实对外可见。

平台内核（总线/网关/链路/工作流）不 import 持久层与分析层；这条边界由本模块守住——
只有装配容器经由这里拿到实现体。于是"没装 extra / 连不上后端 / 配置关闭"三种情况
都不改变链路语义，只改变 `state()` 里的一行事实。

降级口径（与"智能体优先·平台降级"同构）：
- 存储：`memory` 只有内存读视图；`postgres` 在同一读视图前挂 PostGIS/pgvector 落库。
  连接失败**不换实现**——读视图照常服务，写侧交给有界缓冲重试并按计数暴露故障。
- 分析：`off` 时链路里根本不出现 OLAP 代码路径；启用时只做 write-behind 入队，
  摄取与研判热路径永不等 OLAP。
- 知识：图谱缺位时召回自动落到内存案例库，预案生成照旧完成；召回失败只记一条降级，
  绝不把异常抛进预警路径。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from aegis.config import Settings, get_settings
from aegis.domain.messages import TelemetryReading, utc_now
from aegis.knowledge.provider import KnowledgeProvider
from aegis.observability.tracer import Tracer
from aegis.pipeline.chain import ChainResult
from aegis.storage.store import PlatformStore, StoreProtocol

log = logging.getLogger("aegis.integrations")


@dataclass(frozen=True, slots=True)
class IntegrationState:
    """一条装配事实：是否启用、用什么驱动、以及可观测的降级细节。"""

    name: str
    enabled: bool
    driver: str
    detail: dict[str, object] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {"name": self.name, "enabled": self.enabled, "driver": self.driver, "detail": dict(self.detail)}


@dataclass(slots=True)
class StoreBundle:
    """存储面 + 需要生命周期管理的实现体（内存后端为 None）+ 一条初始事实。"""

    store: StoreProtocol
    state: IntegrationState
    durability: Any | None = None


def build_store(settings: Settings | None = None, *, read_model: PlatformStore | None = None) -> StoreBundle:
    """按配置组装存储面。

    构造 `PostgresStore` 不建连接（惰性建池），因此没装 `[postgres]` extra 或库不可达时
    这里也不抛错——连通性判定留给 `start_store`。
    """
    cfg = settings or get_settings()
    read = read_model or PlatformStore()

    if cfg.store_backend != "postgres":
        return StoreBundle(store=read, state=IntegrationState(name="store", enabled=True, driver="memory"))

    from aegis.persistence.postgres import PostgresStore  # 延迟导入：默认部署不必带 asyncpg

    store = PostgresStore(
        dsn=cfg.pg_dsn or None,
        settings=cfg,
        read_model=read,
        pool_min_size=cfg.pg_pool_min_size,
        pool_max_size=cfg.pg_pool_max_size,
    )
    return StoreBundle(
        store=store,
        durability=store,
        state=IntegrationState(
            name="store",
            enabled=True,
            driver="postgres",
            # 只暴露脱敏后的目标：DSN 里的口令绝不进状态接口与日志。
            detail={"target": store.dsn_target, "migrations_on_start": cfg.pg_apply_migrations_on_start},
        ),
    )


async def start_store(bundle: StoreBundle, settings: Settings | None = None) -> IntegrationState:
    """建池并按需应用迁移；失败时保留读视图继续运行，把故障写进状态而不是让进程起不来。"""
    cfg = settings or get_settings()
    if bundle.durability is None:
        return bundle.state

    store = bundle.durability
    detail: dict[str, object] = dict(bundle.state.detail)
    try:
        await store.connect()
    except Exception as exc:
        detail["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
        detail["degraded"] = "read_model_only"
        log.warning("持久层连接失败，降级为内存读视图 + 缓冲重试", extra={"target": detail.get("target"), "err": detail["error"]})
        return IntegrationState(name="store", enabled=True, driver="postgres", detail=detail)

    if cfg.pg_apply_migrations_on_start:
        try:
            detail["migrations_applied"] = len(await store.migrate())
        except Exception as exc:
            # 迁移没跑成意味着表结构不可信：继续写会静默丢列，所以退回"读视图照常"的口径，
            # 写侧的失败与丢弃由缓冲自己计数（bounded + drop-oldest），不在这里越权拆它的内部状态。
            detail["error"] = f"MigrationError: {str(exc)[:200]}"
            detail["degraded"] = "read_model_only"
            log.warning("迁移未应用，落库路径不可信", extra={"err": detail["error"]})
            return IntegrationState(name="store", enabled=True, driver="postgres", detail=detail)

    detail["connected"] = True
    log.info("持久层已接入", extra={"target": detail.get("target")})
    return IntegrationState(name="store", enabled=True, driver="postgres", detail=detail)


async def stop_store(bundle: StoreBundle, *, grace_ms: float = 2_000.0) -> None:
    if bundle.durability is None:
        return
    try:
        await bundle.durability.close(grace_ms=grace_ms)
    except Exception as exc:  # 关停尽力而为：这里抛错会让整条优雅退出半途而废
        log.warning("持久层关停异常", extra={"err": type(exc).__name__})


# --------------------------------------------------------------------- 分析旁路


def build_analytics(settings: Settings | None = None) -> tuple[Any | None, IntegrationState]:
    """按配置建分析 sink；未启用或后端不可达时返回 (None, 状态)，调用方据此不挂任何旁路。"""
    cfg = settings or get_settings()

    if cfg.analytics_backend == "off":
        return None, IntegrationState(name="analytics", enabled=False, driver="off")

    if cfg.analytics_backend == "duckdb":
        from aegis.analytics.duckdb_warehouse import DuckDbWarehouse

        sink = DuckDbWarehouse(cfg.duckdb_path)
        return sink, IntegrationState(
            name="analytics",
            enabled=True,
            driver="duckdb",
            detail={"path": cfg.duckdb_path, "spatial_ready": sink.spatial_ready},
        )

    from aegis.analytics.clickhouse_sink import ClickHouseSink, connect_clickhouse
    from aegis.analytics.port import AnalyticsSinkError

    try:
        client = connect_clickhouse(
            host=cfg.clickhouse_host,
            port=cfg.clickhouse_port,
            database=cfg.clickhouse_database,
            username=cfg.clickhouse_username,
            password=cfg.clickhouse_password,
            secure=cfg.clickhouse_secure,
        )
    except AnalyticsSinkError as exc:
        log.warning("ClickHouse 不可达，分析旁路关闭", extra={"err": str(exc)[:200]})
        return None, IntegrationState(name="analytics", enabled=False, driver="clickhouse", detail={"error": str(exc)[:200]})

    return (
        ClickHouseSink(client, database=cfg.clickhouse_database),
        IntegrationState(
            name="analytics",
            enabled=True,
            driver="clickhouse",
            detail={"target": f"{cfg.clickhouse_host}:{cfg.clickhouse_port}/{cfg.clickhouse_database}"},
        ),
    )


def chain_facts(result: ChainResult) -> list[Any]:
    """链路结果 → 事实行：灾害事实按受影响区域展开，各段耗时作为时延事实入账。

    时延指标的 metric 名沿用 observability 台账的 `stage_*_ms`，分析侧不自造第二套口径。
    """
    from aegis.analytics.port import FactRow

    rows: list[Any] = []
    if result.warning is not None:
        rows.extend(FactRow.from_warning(result.warning))
    moment = utc_now()
    for stage in result.stages:
        rows.append(
            FactRow.from_latency(
                metric=f"stage_{stage.name}_ms",
                latency_ms=stage.latency_ms,
                observed_at=moment,
                trace_id=result.trace_id,
            )
        )
    return rows


def reading_facts(readings: Sequence[TelemetryReading]) -> list[Any]:
    from aegis.analytics.port import FactRow

    return [FactRow.from_reading(reading) for reading in readings]


class AnalyticsRecorder:
    """链路/摄取结果 → 事实行 → sink 入队。热路径只入队，异常一律不外抛。

    计数留在自己身上：分析旁路坏了不能污染 SLA 量测，但必须能被状态接口看见。
    """

    def __init__(self, sink: Any, *, driver: str, settings: Settings | None = None) -> None:
        self._sink = sink
        self._driver = driver
        self._settings = settings or get_settings()
        self.accepted = 0
        self.short_accepted = 0
        self.errors = 0
        self.last_error: str | None = None
        self.schema_error: str | None = None

    async def open(self) -> None:
        """启动期建表（可选）：把 DDL 留在启动，运行期热路径就不掺任何 I/O。"""
        if not self._settings.analytics_apply_schema or not hasattr(self._sink, "apply_schema"):
            return
        try:
            await self._sink.apply_schema()
        except Exception as exc:
            self.schema_error = f"{type(exc).__name__}: {str(exc)[:200]}"
            log.warning("分析后端建表失败，旁路保持惰性", extra={"err": self.schema_error})

    async def record_chain(self, result: ChainResult) -> None:
        await self._submit(chain_facts(result))

    async def record_readings(self, readings: Sequence[TelemetryReading]) -> None:
        await self._submit(reading_facts(readings))

    async def _submit(self, rows: list[Any]) -> None:
        if not rows:
            return
        try:
            accepted = int(await self._sink.ingest(rows))
        except Exception as exc:  # 分析是消费者：坏了就计数，绝不让链路崩溃
            self.errors += 1
            self.last_error = f"{type(exc).__name__}: {str(exc)[:200]}"
            log.warning("分析事实入队失败，已丢弃本批", extra={"rows": len(rows), "err": self.last_error})
            return
        self.accepted += accepted
        self.short_accepted += max(len(rows) - accepted, 0)

    def state(self) -> IntegrationState:
        detail: dict[str, object] = {"accepted": self.accepted, "not_accepted": self.short_accepted, "errors": self.errors}
        if self.last_error is not None:
            detail["last_error"] = self.last_error
        if self.schema_error is not None:
            detail["schema_error"] = self.schema_error
        try:
            stats = self._sink.stats()
        except Exception as exc:  # 读状态失败不该升级成故障，但也不能谎报健康
            detail["stats_error"] = type(exc).__name__
            return IntegrationState(name="analytics", enabled=True, driver=self._driver, detail=detail)
        if isinstance(stats, dict):
            detail["buffered"] = stats.get("buffered")
            detail["inserted"] = stats.get("inserted")
            detail["dropped"] = int(stats.get("dropped_overflow", 0)) + int(stats.get("dropped_closed", 0))
            detail["loss_rate"] = stats.get("loss_rate")
            if self._driver == "duckdb":
                detail["spatial_ready"] = stats.get("spatial_ready")
        return IntegrationState(name="analytics", enabled=True, driver=self._driver, detail=detail)

    async def close(self) -> None:
        """关停前显式落尽缓冲：分析数据可以晚到，但不能因为进程退出就凭空消失。"""
        grace_ms = int(self._settings.analytics_close_grace_ms)
        try:
            await self._sink.flush()
        except Exception as exc:
            self.errors += 1
            self.last_error = f"flush:{type(exc).__name__}: {str(exc)[:200]}"
            log.warning("分析旁路排空失败", extra={"err": self.last_error})
        try:
            await self._sink.close(grace_ms)
        except Exception as exc:
            log.warning("分析 sink 关停异常", extra={"err": type(exc).__name__})


# --------------------------------------------------------------------- 知识与检索


def build_knowledge(settings: Settings | None = None, tracer: Tracer | None = None) -> tuple[KnowledgeProvider | None, IntegrationState]:
    """案例知识提供者：预案生成前的历史案例召回（读路径，结构上不含 LLM）。

    未配置 graphiti URI 时是纯内存提供者：零外部依赖、内置西藏案例，所以"图谱没起"
    从来不该让预案变慢或失败——降级链在 `FallbackKnowledgeProvider` 内部，装配层不复制。
    这里只报告事实：驱动是什么、预算多少、兜底库有多少条。
    """
    from aegis.knowledge.cases import load_builtin_cases
    from aegis.knowledge.provider import build_knowledge_provider

    cfg = settings or get_settings()
    provider = build_knowledge_provider(
        cfg,
        graphiti_uri=cfg.knowledge_graphiti_uri or None,
        recall_budget_ms=cfg.knowledge_recall_budget_ms,
        tracer=tracer,
    )
    return provider, IntegrationState(
        name="knowledge",
        enabled=True,
        driver="graphiti" if cfg.knowledge_graphiti_uri else "in_memory",
        detail={
            "recall_budget_ms": cfg.knowledge_recall_budget_ms,
            # 兜底库存量：图谱不可用时召回还能给出多少条案例，这是降级后的真实能力上限
            "fallback_cases": len(load_builtin_cases()),
            # URI 只报 host:port——连接串里的凭据绝不进状态接口
            "graphiti": target_of(cfg.knowledge_graphiti_uri),
        },
    )


def target_of(uri: str) -> str:
    """连接串的可公开目标段（host[:port]）：scheme、凭据、路径与参数一律丢弃。

    用 `urlsplit` 而不是按 '@' 切串：手写切分很容易把凭据段当成主机名（这里曾错过一次），
    而状态接口是匿名可读的。主机名取不到就返回空串——宁可少报，不猜。
    """
    if not uri:
        return ""
    parts = urlsplit(uri if "://" in uri else f"aegis://{uri}")
    try:
        port = parts.port
    except ValueError:
        port = None
    host = parts.hostname or ""
    return f"{host}:{port}" if host and port else host


__all__ = [
    "AnalyticsRecorder",
    "IntegrationState",
    "StoreBundle",
    "build_analytics",
    "build_knowledge",
    "build_store",
    "chain_facts",
    "reading_facts",
    "start_store",
    "stop_store",
    "target_of",
]
