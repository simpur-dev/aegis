"""共享记忆（§4.3）：门面 + 三类 provider 适配器（P2 纯搬运，函数体逐字未改）。

适配住在装配层：`services/memory.py` 只认 `CategoryProvider` 这个窄协议，不该知道 CaseMatch 与预算参数。"""

from __future__ import annotations

from typing import Any

from aegis.config import Settings, get_settings
from aegis.integrations.state import IntegrationState


class EpisodicFromCases:
    """情景记忆（§4.3 表里那一行"案例库"）：把知识腿的 `CaseMatch` 摊成可序列化条目。

    适配住在装配层而不是内核：`services/memory.py` 认的是 `CategoryProvider` 这个窄协议，
    它不该知道 `CaseMatch`、预算参数或降级链的存在。
    """

    name = "cases"

    def __init__(self, provider: Any, *, budget_ms: float) -> None:
        self._provider = provider
        self._budget_ms = budget_ms

    async def recall(
        self, query: str, *, region_code: str | None = None, hazard_type: str | None = None, limit: int = 3
    ) -> list[dict[str, object]]:
        matches = await self._provider.recall(
            query, hazard_type=hazard_type, region_code=region_code, limit=limit, budget_ms=self._budget_ms
        )
        return [match.planning_brief() for match in matches]


class KnowledgeFromGraphiti:
    """知识记忆（图谱那一类）：**只有真接上 Graphiti 才注入**。

    没配 `AEGIS_KNOWLEDGE_GRAPHITI_URI` 时装配层拿不到图谱对象，这一类就整个不注入——
    让内核回 `connected=false`。把内存案例库冒充成"图谱读到的结果"，等于给论文那条
    "知识记忆"造了一个永远为真的空壳，而它读起来与真的 Neo4j 命中一模一样。
    """

    name = "graphiti"

    def __init__(self, provider: Any, *, budget_ms: float) -> None:
        self._provider = provider
        self._budget_ms = budget_ms

    async def recall(
        self, query: str, *, region_code: str | None = None, hazard_type: str | None = None, limit: int = 3
    ) -> list[dict[str, object]]:
        matches = await self._provider.recall(
            query, hazard_type=hazard_type, region_code=region_code, limit=limit, budget_ms=self._budget_ms
        )
        return [match.planning_brief() for match in matches]


class SemanticFromRetrieval:
    """语义记忆（pgvector 那一类）：走混合检索，dense + 词法 + RRF，一次 LLM 都不进。"""

    name = "retrieval"

    def __init__(self, retrieval: Any, *, budget_ms: float) -> None:
        self._retrieval = retrieval
        self._budget_ms = budget_ms

    async def recall(
        self, query: str, *, region_code: str | None = None, hazard_type: str | None = None, limit: int = 3
    ) -> list[dict[str, object]]:
        from aegis.retrieval.service import RetrievalQuery  # 延迟导入：默认部署不必带检索腿

        outcome = await self._retrieval.retrieve(
            RetrievalQuery(text=query, k=limit, hazard_type=hazard_type, region_code=region_code, budget_ms=self._budget_ms)
        )
        return [doc.as_reference() for doc in outcome.docs]


def build_memory(
    *,
    store: object | None = None,
    retrieval: object | None = None,
    knowledge: object | None = None,
    settings: Settings | None = None,
) -> tuple[Any, IntegrationState]:
    """装配共享记忆（§4.3 / D5）：接得上库就落 `agent_memory`，接不上就如实留在进程内。

    这里决定的只是**这一条门面底下是什么**，不改变调用方的语义：assess 写研判、feedback 写教训、
    下一次同区域 assess 检索引用，两种后端走同一套过滤与排序。差别在持久性——进程内那份重启即零、
    多实例互不可见，"共享记忆"这句话只有 Postgres 侧撑得住，所以 `driver` 必须出现在状态面上，
    而不是等有人问"记忆为什么是空的"时才去翻代码。

    嵌入腿（`retrieval.embedder`）可选注入：没配权重时向量就是没算，行以 NULL 存着，
    读侧退到"过滤 + 时序"；这里不拿随机向量冒充语义命中。
    """
    from aegis.services.memory.memory import InMemoryAgentMemory, MemoryService  # 内核门面，延迟导入以保持装配层为唯一入口

    cfg = settings or get_settings()
    port: Any = None
    driver = "memory"
    detail: dict[str, object] = {}
    if store is not None and getattr(store, "acquire", None) is not None:
        from aegis.persistence.agent_memory import memory_store_port  # 延迟导入：默认部署不必带 asyncpg

        port = memory_store_port(store)
        if port is not None:
            driver = "postgres"

    backend = port if port is not None else InMemoryAgentMemory()
    embedder = getattr(retrieval, "embedder", None)
    embedded_model = str(getattr(embedder, "model_id", "") or "") if embedder is not None else ""
    if embedder is None:
        detail["embedding"] = "absent"  # 没嵌入腿：向量列一律 NULL，读侧退到过滤+时序，这件事要写得出来

    # 四类记忆的提供者在这里接线（§4.3 的"统一门面"要真有底下那四层，否则门面是个空壳）：
    # 情景=案例知识腿（含降级链）、语义=混合检索腿、知识=只在真接了 Graphiti 时给。
    # 缺的那一类由门面回 `connected=false`，而不是伪装成"读了，没结果"。
    providers: dict[str, Any] = {}
    if knowledge is not None:
        providers["episodic"] = EpisodicFromCases(knowledge, budget_ms=cfg.knowledge_recall_budget_ms)
        graphiti = getattr(knowledge, "primary", None)  # 只有降级链（即配了图谱 URI）才有这一头
        if graphiti is not None:
            providers["knowledge"] = KnowledgeFromGraphiti(graphiti, budget_ms=cfg.knowledge_recall_budget_ms)
    if retrieval is not None:
        providers["semantic"] = SemanticFromRetrieval(retrieval, budget_ms=cfg.retrieval_budget_ms)
    detail["categories"] = {name: ("connected" if name in providers else "absent") for name in ("episodic", "semantic", "knowledge")}

    service = MemoryService(
        backend,
        episodic=providers.get("episodic"),
        semantic=providers.get("semantic"),
        knowledge=providers.get("knowledge"),
        embedder=embedder,
        embedded_model=embedded_model,
        backend=driver,
    )
    capacity = getattr(backend, "capacity", None)
    if capacity is not None:
        detail["capacity"] = capacity
    return service, IntegrationState(name="memory", enabled=True, driver=driver, detail=detail)
