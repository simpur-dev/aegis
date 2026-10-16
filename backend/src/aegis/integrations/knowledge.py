"""案例知识提供者与图谱预热/关停：启动与关停都带墙钟上限，超了就记降级，不被一条可选腿拖住整台进程。（P2 纯搬运：函数体逐字未改。）"""

from __future__ import annotations

import asyncio

from aegis.config import Settings, get_settings
from aegis.integrations.shared import log, target_of
from aegis.integrations.state import IntegrationState
from aegis.knowledge.cases import DATASET_PROVENANCE, load_builtin_cases
from aegis.knowledge.provider import KnowledgeProvider, SupportsSchemaPreparation, SupportsShutdown
from aegis.observability.tracer import Tracer

# 启动期索引初始化的墙钟上限：Neo4j 驱动默认连接超时约 30s，那是"库在哪儿"的时间，
# 不是平台启动该等的时间。超了就记降级，召回自己走内存兜底。
KNOWLEDGE_SCHEMA_TIMEOUT_SECONDS = 20.0

# 关停比启动更不留情：索引没建成还能靠内存兜底继续服务，连接关不上则会把整个停服卡住。
KNOWLEDGE_CLOSE_TIMEOUT_SECONDS = 10.0


def build_knowledge(settings: Settings | None = None, tracer: Tracer | None = None) -> tuple[KnowledgeProvider | None, IntegrationState]:
    """案例知识提供者：预案生成前的历史案例召回（读路径，结构上不含 LLM）。

    未配置 graphiti URI 时是纯内存提供者：零外部依赖、内置预案模板，所以"图谱没起"
    从来不该让预案变慢或失败——降级链在 `FallbackKnowledgeProvider` 内部，装配层不复制。
    这里只报告事实：驱动是什么、预算多少、兜底库有多少条、这些条目是什么性质的数据。
    """
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
            # 案例库是自编预案模板还是真实事件复盘，决定预警依据链的可信度口径，必须外显
            "dataset": DATASET_PROVENANCE,
            # URI 只报 host:port——连接串里的凭据绝不进状态接口
            "graphiti": target_of(cfg.knowledge_graphiti_uri),
        },
    )


async def warm_knowledge(provider: KnowledgeProvider | None) -> str | None:
    """启动期把图谱索引建好：索引缺失时 cosine 与 bm25 两路检索都会报错，等于图谱腿白装。

    纯内存装配不实现 `SupportsSchemaPreparation`，这里就整段跳过——不制造"预热了个空"的假事实。
    失败与超时都不阻断启动：返回原因，由 `integration_status()` 作为 knowledge 这条腿的降级事实外显。
    超时上限是必要的：Neo4j 不可达时驱动会按自己的连接超时干等，而"启动"不该被一条可选腿拖住。
    """
    if provider is None or not isinstance(provider, SupportsSchemaPreparation):
        return None
    try:
        await asyncio.wait_for(provider.prepare_schema(), timeout=KNOWLEDGE_SCHEMA_TIMEOUT_SECONDS)
    except TimeoutError:
        log.warning("图谱索引初始化超时，召回将在降级链上运行", extra={"timeout_seconds": KNOWLEDGE_SCHEMA_TIMEOUT_SECONDS})
        return f"schema_timeout:{KNOWLEDGE_SCHEMA_TIMEOUT_SECONDS:g}s"
    except Exception as exc:  # 连接失败/权限不足/依赖缺失都要变成一行可见事实，而不是让进程起不来
        log.warning("图谱索引初始化失败，召回将在降级链上运行", extra={"err": type(exc).__name__})
        return f"{type(exc).__name__}: {str(exc)[:200]}"
    return None


async def close_knowledge(provider: KnowledgeProvider | None) -> str | None:
    """关停期把知识腿的连接关掉，失败只变成一行原因，不打断后面的停服步骤。

    图谱实例里揣着 Neo4j 异步驱动和 LLM 客户端：没人关它们，进程要么在退出时干等连接超时，
    要么留下"服务已停但图谱还连着"的现场。纯内存装配不实现 `SupportsShutdown`，整段跳过。
    超时同样要有：驱动在自己那侧卡住时，"停服"不该被一条可选腿拖住（上限比启动期更短）。
    """
    if provider is None or not isinstance(provider, SupportsShutdown):
        return None
    try:
        await asyncio.wait_for(provider.close(), timeout=KNOWLEDGE_CLOSE_TIMEOUT_SECONDS)
    except TimeoutError:
        log.warning("知识层关闭超时", extra={"timeout_seconds": KNOWLEDGE_CLOSE_TIMEOUT_SECONDS})
        return f"close_timeout:{KNOWLEDGE_CLOSE_TIMEOUT_SECONDS:g}s"
    except Exception as exc:  # 关停的失败要能被读到，而不是把后面的停服步骤打断
        log.warning("知识层关闭失败", extra={"err": type(exc).__name__})
        return f"{type(exc).__name__}: {str(exc)[:200]}"
    return None
