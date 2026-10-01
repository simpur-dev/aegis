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

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from aegis.config import Settings, get_settings
from aegis.connectors.mqtt import AiomqttClient, MqttSource  # MQTT 只在此装配层被 import；aiomqtt 本身延迟到建连时导入
from aegis.domain.messages import TelemetryReading, utc_now
from aegis.knowledge.cases import HazardCase, load_builtin_cases
from aegis.knowledge.provider import KnowledgeProvider
from aegis.observability.tracer import Tracer
from aegis.pipeline.chain import ChainResult
from aegis.storage.store import PlatformStore, StoreProtocol

if TYPE_CHECKING:
    from aegis.retrieval.embedder import Embedder
    from aegis.retrieval.reranker import Reranker
    from aegis.retrieval.service import HybridRetrievalService

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

    构造 `PostgresStore` 不建连接（惰性建池），所以没装 `[postgres]` extra 或库不可达时这里不抛错
    ——连通性判定留给 `start_store`。但缺 DSN 是配置错误，构造阶段就报：
    让它静默退化成"只有内存视图"的部署，比开不起来难查一个量级。
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


def build_mqtt(settings: Settings | None = None) -> tuple[MqttSource | None, IntegrationState]:
    """装配 MQTT 推送腿（站端主动 publish、平台 subscribe）——接入腿里唯一不需要轮询的一条。

    未开启时链路里完全不出现这一层；开了但 `[iot]` extra 没装，则给出 `mqtt-unavailable`
    这一行事实。运维必须看到"这条腿没起来 + 为什么"，而不是"站端一直在发、平台这边查不到数"。
    凭据只进连接参数，绝不进状态面。
    """
    cfg = settings or get_settings()
    if not cfg.mqtt_enabled:
        return None, IntegrationState(name="mqtt", enabled=False, driver="off")

    try:
        import aiomqtt  # noqa: F401  只做能力探测：真正建连发生在后台订阅任务里
    except ImportError as exc:
        return None, IntegrationState(
            name="mqtt",
            enabled=False,
            driver="mqtt-unavailable",
            detail={
                "broker": f"{cfg.mqtt_host}:{cfg.mqtt_port}",
                "reason": f"缺少 aiomqtt（[iot] extra）: {type(exc).__name__}",
            },
        )

    client = AiomqttClient(
        host=cfg.mqtt_host,
        port=cfg.mqtt_port,
        topic_prefix=cfg.mqtt_topic_prefix,
        username=cfg.mqtt_username,
        password=cfg.mqtt_password,
        qos=cfg.mqtt_qos,
        keepalive_seconds=cfg.mqtt_keepalive_seconds,
        client_id=cfg.mqtt_client_id,
        incoming_queue_limit=cfg.mqtt_buffer_limit,
    )
    detail: dict[str, object] = {**client.status(), "buffer_limit": cfg.mqtt_buffer_limit}
    source = MqttSource(client, prefix=cfg.mqtt_topic_prefix, buffer_limit=cfg.mqtt_buffer_limit)
    return source, IntegrationState(name="mqtt", enabled=True, driver="mqtt", detail=detail)


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


# --------------------------------------------------------------------- 检索


# 重排候选上限：与链路实际取用的上下文块数（_CONTEXT_DOC_LIMIT=5）对齐。
# 本机实测真实语料正文（~305 字）5 对 4201ms、10 对 8361ms——多排的候选会被 k 截断丢掉，
# 等于花一倍时延去给"注定不注入的块"排序。改这个数字必须同步改 retrieval_budget_ms。
_RERANK_TOP_N = 5


def build_retrieval(
    settings: Settings | None = None,
    tracer: Tracer | None = None,
    *,
    store: object | None = None,
    cases: Sequence[HazardCase] | None = None,
) -> tuple[HybridRetrievalService | None, IntegrationState]:
    """混合检索装配：词法腿零外部依赖，密集腿要 pgvector 连接，重排腿要 ONNX 权重。

    三条腿的可用性在这里一次说清并写进状态，而不是等到第一次检索时才在 degradation 里露头：
    现场调"≤3min 预警生成"的人需要知道这台盒子上检索到底是全功能还是降级件。
    权重缺失时**不报错**：切 `HashingEmbedder`（确定性词面近似）并在状态里标 degraded——
    它的分数不得用于任何准确率口径，这一点由 `driver`（模型身份）与 `degraded`（降级原因）共同守门。
    """
    from aegis.retrieval.corpus import docs_from_cases
    from aegis.retrieval.lexical import build_lexical_index
    from aegis.retrieval.service import HybridRetrievalService

    cfg = settings or get_settings()
    if not cfg.retrieval_enabled:
        return None, IntegrationState(name="retrieval", enabled=False, driver="off")

    corpus = docs_from_cases(cases if cases is not None else load_builtin_cases())
    embedder, embedder_note = _select_embedder(cfg)
    reranker, reranker_note = _select_reranker(cfg)
    acquire = getattr(store, "acquire", None)
    notes = [note for note in (embedder_note, reranker_note) if note]

    service = HybridRetrievalService(
        embedder=embedder,
        lexicon=build_lexical_index(corpus),
        acquire=acquire,
        reranker=reranker,
        tracer=tracer,
        default_budget_ms=cfg.retrieval_budget_ms,
        # 重排候选上限由实测反推：10 对中位 1514ms、20 对 3005ms（本机 int8/CPU）。
        # 买不起的候选数量不会让结果更对，只会让重排腿在预算内必然超时——那等于白装这根腿。
        rerank_top_n=_RERANK_TOP_N,
    )
    return service, IntegrationState(
        name="retrieval",
        enabled=True,
        driver=embedder.model_id,
        detail={
            "budget_ms": cfg.retrieval_budget_ms,
            "corpus_docs": len(corpus),
            # 密集腿是 pgvector 上的历史任务单元，没有连接时服务会把它记成"跳过"而不是失败
            "dense_leg": bool(acquire is not None),
            "rerank_leg": reranker.enabled,
            "model_dir": cfg.retrieval_model_dir,
            "degraded": ";".join(notes),
        },
    )


async def warm_retrieval(service: HybridRetrievalService | None) -> str | None:
    """把 ONNX 会话装载挪出第一条预警的路径：本机冷启动 embed 2102ms、rerank 4732ms。

    惰性装载是必要的（装配阶段不许阻塞事件循环），但它把装载费转给了第一次检索——
    对"≤3min 预警生成"来说，让第一条预警替后面所有请求付这笔钱不划算，所以放到启动期。
    装载失败不抛异常：返回失败原因交给调用方记账，运行期该腿会自己记降级。
    """
    if service is None:
        return None
    try:
        await asyncio.to_thread(service.embedder.embed, ["预警检索预热"])
        reranker = service.reranker
        if reranker is not None and reranker.enabled:
            await asyncio.to_thread(reranker.rerank, "预热", [("warm", "预警检索预热")])
    except Exception as exc:
        log.warning("检索预热失败，对应腿将在运行期降级", extra={"err": type(exc).__name__})
        return type(exc).__name__
    return None


def _select_embedder(cfg: Settings) -> tuple[Embedder, str | None]:
    from aegis.retrieval.embedder import HashingEmbedder, OnnxEmbedder
    from aegis.retrieval.onnx_io import EMBEDDER_MODEL_DIR

    root = Path(cfg.retrieval_model_dir) / EMBEDDER_MODEL_DIR
    if _weights_present(root):
        return OnnxEmbedder.from_dir(root), None
    return HashingEmbedder(), f"bge-m3 权重缺失（{root}），密集嵌入降级为确定性哈希（分数不得用于准确率口径）"


def _select_reranker(cfg: Settings) -> tuple[Reranker, str | None]:
    from aegis.retrieval.onnx_io import RERANKER_MODEL_DIR
    from aegis.retrieval.reranker import NoopReranker, OnnxReranker

    root = Path(cfg.retrieval_model_dir) / RERANKER_MODEL_DIR
    if _weights_present(root):
        return OnnxReranker.from_dir(root), None
    return NoopReranker(), f"bge-reranker 权重缺失（{root}），重排腿关闭"


def _weights_present(root: Path) -> bool:
    """只查文件在不在：装载 568MB 权重必须留在第一次检索之前、启动线程里完成。"""
    from aegis.retrieval.onnx_io import MODEL_TOKENIZER_FILE, MODEL_WEIGHTS_FILE

    return (root / MODEL_WEIGHTS_FILE).is_file() and (root / MODEL_TOKENIZER_FILE).is_file()


# --------------------------------------------------------------------- 知识


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
    "build_mqtt",
    "build_retrieval",
    "build_store",
    "chain_facts",
    "reading_facts",
    "start_store",
    "stop_store",
    "target_of",
]
