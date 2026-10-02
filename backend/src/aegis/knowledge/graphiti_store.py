"""Graphiti 时序知识图谱存储：AEGIS 案例库的读写实现（asyncio 原生，无线程桥）。

与 NexusMind `backend/app/utils/graphiti_client.py` 的关系是"移植 + 拆解"：
- 保留：DashScope 批量上限的 `BatchedOpenAIEmbedder`、按角色缓存实例、LLM/Embedder 显式注入；
- 丢弃：`run_async`/`_ensure_loop` 后台事件循环桥与 `threading.Lock`（那是同步 Flask 的补丁，
  AEGIS 全异步，直接 `await graphiti.add_episode(...)`）；
- 丢弃：写 `os.environ['OPENAI_API_KEY']` 的隐式传参（改为 `GraphitiConfig` 显式传参）。

图谱命名（group_id）规则——写入与召回共用同一组函数，规则只有一处：
    aegis_case_<hazard>_<region4|all>
  （分隔符只能下划线：Graphiti 用 `^[a-zA-Z0-9_-]+$` 校验 group_id，点号会被拒。）
  * `<hazard>`：单个灾种 token（写入取 `case.hazard_types[0]`，召回取查询给的 hazard_type），缺省 `all`；
  * `<region4>`：行政区划码前 4 位（地级市），不足 4 位取整码，缺省 `all`；
  * 写入只用**最具体**的那个图（一条案例一个 group_id）；召回按"具体 → 泛化"四元扇出查询，
    于是只知灾种或只知区域时都能命中，且不会跨区串案例。

预算与降级不在本类：见 `provider.FallbackKnowledgeProvider`。本类只保证
`recall()` 结构上不可能触发 LLM 调用——读实例的 llm_client 是 `make_read_guards()` 造出的抛错守卫。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from aegis.domain.messages import now_iso
from aegis.errors import AegisError, ErrorCode
from aegis.knowledge.cases import HazardCase, load_builtin_cases
from aegis.knowledge.prompts import CASE_EXTRACTION_INSTRUCTIONS, CASE_SOURCE_DESCRIPTION
from aegis.knowledge.provider import CaseMatch, KnowledgeConfigError, run_with_budget

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

GROUP_PREFIX = "aegis_case"
HAZARD_ALL = "all"
REGION_ALL = "all"
REGION_SCOPE_LENGTH = 4
DASHSCOPE_EMBEDDING_BATCH_SIZE = 10
DEFAULT_RECALL_BUDGET_MS = 1_500.0
GRAPH_FACT_LIMIT = 6
EPISODE_UUID_PREFIX = "episode."


class GraphitiRole(StrEnum):
    """读写两套客户端：角色即"是否允许触碰 LLM"的边界。"""

    read = "read"
    write = "write"


class GraphitiUnavailableError(AegisError):
    code = ErrorCode.NOT_READY


class LlmCallInRecallError(AegisError):
    """读路径试图调用 LLM = 架构违例（会让预警生成时延脱离 ≤3min 预算）。"""

    code = ErrorCode.INTERNAL


@dataclass(frozen=True, slots=True)
class GraphitiConfig:
    """连接参数：LLM 侧与图谱侧凭据都取自 `Settings`，配置面只有一份。"""

    uri: str
    user: str = "neo4j"
    password: str = ""
    database: str = "neo4j"
    llm_api_key: str = ""
    llm_base_url: str = ""
    llm_model: str = ""
    embedding_model: str = "text-embedding-v3"
    embedding_dim: int = 1024

    @classmethod
    def from_parts(
        cls,
        *,
        uri: str,
        settings: object | None = None,
        user: str | None = None,
        password: str | None = None,
        database: str | None = None,
        embedding_model: str | None = None,
        embedding_dim: int | None = None,
    ) -> GraphitiConfig:
        """取值口径：显式参数 > `Settings` 字段 > 类默认值。

        配置面只有 `Settings` 一处：图谱凭据若改成直接读环境变量，deploy 里的键就脱离了
        配置声明，compose/.env.example 的一致性校验也管不到它（见 tests/unit/test_config_env_surface.py）。
        """
        return cls(
            uri=uri,
            user=user or str(getattr(settings, "neo4j_user", "") or "") or "neo4j",
            password=password or str(getattr(settings, "neo4j_password", "") or ""),
            database=database or str(getattr(settings, "neo4j_database", "") or "") or "neo4j",
            llm_api_key=str(getattr(settings, "llm_api_key", "") or ""),
            llm_base_url=str(getattr(settings, "llm_base_url", "") or ""),
            llm_model=str(getattr(settings, "llm_model", "") or ""),
            embedding_model=embedding_model or str(getattr(settings, "knowledge_embedding_model", "") or "") or "text-embedding-v3",
            embedding_dim=embedding_dim or int(getattr(settings, "knowledge_embedding_dim", 0) or 0) or 1024,
        )


def sanitize_graph_token(value: str | None) -> str:
    """把维度值压成 group_id 合法片段：仅保留 ASCII 小写字母/数字/下划线，其余转下划线。"""
    if not value:
        return ""
    kept = "".join(ch if (ch.isascii() and (ch.isalnum() or ch == "_")) else "_" for ch in value.lower())
    return kept.strip("_")


def region_scope(region_code: str | None) -> str:
    if not region_code:
        return ""
    return region_code[:REGION_SCOPE_LENGTH].lower()


def group_id_for(hazard_type: str | None, region_code: str | None) -> str:
    hazard = sanitize_graph_token(hazard_type) or HAZARD_ALL
    scope = region_scope(region_code) or REGION_ALL
    return f"{GROUP_PREFIX}_{hazard}_{scope}"


def group_ids_for(hazard_type: str | None, region_code: str | None) -> list[str]:
    """由具体到泛化的扇出：灾种+区域 → 灾种 → 区域 → 全域（一次查询覆盖四种写入形态）。"""
    hazard = sanitize_graph_token(hazard_type) or HAZARD_ALL
    scope = region_scope(region_code) or REGION_ALL
    ordered = [
        group_id_for(hazard, scope),
        group_id_for(hazard, None),
        group_id_for(None, scope),
        group_id_for(None, None),
    ]
    return list(dict.fromkeys(ordered))


def case_episode_uuid(case_id: str) -> str:
    """剧集主键由案例主键派生（加前缀避免与其它剧集混用），召回据此回指本地案例本体。"""
    return f"{EPISODE_UUID_PREFIX}{case_id}"


def case_id_from_episode_uuid(uuid: str | None) -> str | None:
    if not uuid or not uuid.startswith(EPISODE_UUID_PREFIX):
        return None
    candidate = uuid.removeprefix(EPISODE_UUID_PREFIX)
    return candidate if candidate.startswith("case_") else None


def _iso(moment: datetime | None) -> str:
    return now_iso(moment) if moment is not None else ""


def _db_error_types() -> tuple[type[BaseException], ...]:
    """neo4j 驱动异常按需收集；未安装 graph extra 时返回空元组（except 永不匹配）。"""
    try:
        from neo4j.exceptions import AuthError, ClientError, ServiceUnavailable, TransientError
    except ImportError:
        return ()
    return (ServiceUnavailable, TransientError, AuthError, ClientError)


class ReadGuards:
    """读实例的"零模型调用"客户端组：LLM 与 cross-encoder 都被换成抛错守卫。

    守卫类必须真继承 `graphiti_core` 的抽象基类（Graphiti 内部用 pydantic 做 isinstance 校验），
    所以类定义放在函数内——模块导入阶段依然不触碰任何图谱依赖。
    """

    def __init__(self, llm_client: Any, cross_encoder: Any) -> None:
        self.llm_client = llm_client
        self.cross_encoder = cross_encoder

    @property
    def llm_calls(self) -> int:
        return int(getattr(self.llm_client, "calls", 0))


def make_read_guards(role: GraphitiRole = GraphitiRole.read) -> ReadGuards:
    """构造读路径专用客户端：一旦被调用就抛 `LlmCallInRecallError` 并累计计数。

    Graphiti 的 `search_()` 只需要 embedder（查询向量化）与 reranker；只要重排器是 RRF，
    读路径就永不触碰这里。谁把 reranker 改回 cross_encoder、或往召回里塞 add_episode，
    都会当场炸掉，而不是静默地把 4—7 次 LLM 调用塞进 ≤3min 的预警链路。
    """
    from graphiti_core.cross_encoder.client import CrossEncoderClient
    from graphiti_core.llm_client.client import LLMClient

    class GuardedLlmClient(LLMClient):
        def __init__(self, read_role: GraphitiRole) -> None:
            super().__init__(config=None)
            self.read_role = read_role
            self.calls = 0

        async def generate_response(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            self.calls += 1
            raise LlmCallInRecallError(
                "召回路径试图调用 LLM",
                detail={"role": self.read_role.value, "caller": "generate_response"},
                retryable=False,
            )

        async def _generate_response(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            return await self.generate_response(*args, **kwargs)

    class GuardedCrossEncoder(CrossEncoderClient):
        async def rank(self, query: str, passages: list[str]) -> list[tuple[str, float]]:
            raise LlmCallInRecallError("召回路径试图调用 cross_encoder 重排", detail={"passages": len(passages)}, retryable=False)

    return ReadGuards(GuardedLlmClient(role), GuardedCrossEncoder())


def hybrid_search_config(limit: int) -> Any:
    """语义向量 + BM25 + 图遍历三路混合，重排一律 RRF（零模型调用）。延迟导入检索依赖。

    读路径之所以能做到零 LLM 调用，一半靠 `ReadGuards`（调用即抛错），另一半靠这里的
    `reranker=rrf`——Graphiti 默认的 cross_encoder 重排会发起额外的模型请求。
    """
    from graphiti_core.search.search_config import (
        EdgeReranker,
        EdgeSearchConfig,
        EdgeSearchMethod,
        EpisodeReranker,
        EpisodeSearchConfig,
        EpisodeSearchMethod,
        NodeReranker,
        NodeSearchConfig,
        NodeSearchMethod,
        SearchConfig,
    )

    return SearchConfig(
        edge_config=EdgeSearchConfig(
            search_methods=[EdgeSearchMethod.bm25, EdgeSearchMethod.cosine_similarity, EdgeSearchMethod.bfs],
            reranker=EdgeReranker.rrf,
        ),
        node_config=NodeSearchConfig(
            search_methods=[NodeSearchMethod.bm25, NodeSearchMethod.cosine_similarity, NodeSearchMethod.bfs],
            reranker=NodeReranker.rrf,
        ),
        episode_config=EpisodeSearchConfig(search_methods=[EpisodeSearchMethod.bm25], reranker=EpisodeReranker.rrf),
        limit=limit,
    )


def build_batched_embedder(config: GraphitiConfig) -> Any:
    """DashScope 等 API 单次 embed 批量 ≤10：切片后拼回（NexusMind 现场验证的约束）。

    凭据门禁必须在这里也有：`build_graphiti()` 两个角色都走本函数，缺 key 时
    `OpenAIEmbedder` 会先抛 SDK 的裸 `OpenAIError`，绕过类型化降级——装配面于是把
    "没配凭据"报成一个无法归类的错误（真 Neo4j 上实测到）。读写一视同仁：
    图谱检索要把查询串向量化，所以**没有凭据连读路径都建不起来**，别假装能降级成"只读"。
    """
    if not config.llm_api_key:
        raise GraphitiUnavailableError("图谱检索需要向量凭据（查询向量化由 embedding 模型完成），llm_api_key 为空")
    from graphiti_core.embedder.openai import OpenAIEmbedder, OpenAIEmbedderConfig

    class BatchedOpenAIEmbedder(OpenAIEmbedder):
        async def create_batch(self, input_data: list[str]) -> list[list[float]]:
            results: list[list[float]] = []
            for start in range(0, len(input_data), DASHSCOPE_EMBEDDING_BATCH_SIZE):
                chunk = input_data[start : start + DASHSCOPE_EMBEDDING_BATCH_SIZE]
                results.extend(await super().create_batch(chunk))
            return results

    embedder_config = OpenAIEmbedderConfig(
        api_key=config.llm_api_key,
        base_url=config.llm_base_url or None,
        embedding_model=config.embedding_model,
        embedding_dim=config.embedding_dim,
    )
    return BatchedOpenAIEmbedder(config=embedder_config)


def build_llm_client(config: GraphitiConfig) -> Any:
    """Chat-Completions 口径的通用客户端（兼容 DashScope），只交给写实例。"""
    from graphiti_core.llm_client.config import LLMConfig
    from graphiti_core.llm_client.openai_generic_client import OpenAIGenericClient

    if not config.llm_api_key:
        raise GraphitiUnavailableError("案例入库需要 LLM 凭据（图谱抽取由模型完成），llm_api_key 为空")
    return OpenAIGenericClient(
        config=LLMConfig(
            api_key=config.llm_api_key,
            base_url=config.llm_base_url or None,
            model=config.llm_model or None,
            small_model=config.llm_model or None,
        )
    )


def build_graphiti(role: GraphitiRole, config: GraphitiConfig, guards: ReadGuards) -> Any:
    """默认实例工厂：读写两套客户端共享同一 Neo4jDriver（连接池复用，不重复建链）。

    read 实例的 llm_client 是守卫（见 `ReadGuards`），write 实例才是真实模型客户端；
    两者的 cross_encoder 一律是抛错守卫——预案图谱的重排用 RRF 足够。
    """
    from graphiti_core import Graphiti
    from graphiti_core.driver.neo4j_driver import Neo4jDriver

    driver = Neo4jDriver(uri=config.uri, user=config.user, password=config.password, database=config.database)
    llm_client: Any = guards.llm_client if role is GraphitiRole.read else build_llm_client(config)
    return Graphiti(graph_driver=driver, llm_client=llm_client, embedder=build_batched_embedder(config), cross_encoder=guards.cross_encoder)


class GraphitiKnowledgeProvider:
    """真实 Graphiti 实现：`learn()` 只在入库时写剧集，`recall()` 只做混合检索。"""

    name = "graphiti"

    def __init__(
        self,
        *,
        config: GraphitiConfig,
        cases: Sequence[HazardCase] | None = None,
        default_budget_ms: float | None = DEFAULT_RECALL_BUDGET_MS,
        graphiti_factory: Callable[[GraphitiRole, Callable[[], ReadGuards]], Any] | None = None,
        search_config_factory: Callable[[int], Any] = hybrid_search_config,
    ) -> None:
        if not config.uri:
            raise KnowledgeConfigError("Graphiti 连接缺少 neo4j uri")
        self._config = config
        self._index: dict[str, HazardCase] = {case.case_id: case for case in (cases if cases is not None else load_builtin_cases())}
        self._default_budget_ms = default_budget_ms
        self._search_config_factory = search_config_factory
        self._guards: ReadGuards | None = None
        # 守卫（会抛错的 LLM 客户端）只在真要建图谱实例时才构造，注入替身的单测不必导入图谱栈
        self._factory = graphiti_factory or (lambda role, guards: build_graphiti(role, self._config, guards()))
        self._instances: dict[GraphitiRole, Any] = {}
        # asyncio 原生互斥即可，不需要 NexusMind 的 threading.Lock + 后台事件循环桥
        self._lock = asyncio.Lock()

    @property
    def config(self) -> GraphitiConfig:
        return self._config

    @property
    def default_budget_ms(self) -> float | None:
        return self._default_budget_ms

    @property
    def case_ids(self) -> list[str]:
        return sorted(self._index)

    @property
    def read_guards(self) -> ReadGuards:
        if self._guards is None:
            self._guards = make_read_guards()
        return self._guards

    def instance_of(self, role: GraphitiRole) -> Any | None:
        """已建实例只读暴露：测试据此确认召回没有顺带创建写实例。"""
        return self._instances.get(role)

    async def _graphiti(self, role: GraphitiRole) -> Any:
        """按角色惰性建实例并缓存；建连接本身不占用召回预算。"""
        cached = self._instances.get(role)
        if cached is not None:
            return cached
        async with self._lock:
            cached = self._instances.get(role)
            if cached is None:
                try:
                    cached = self._factory(role, lambda: self.read_guards)
                except ImportError as exc:  # 未安装 graph extra → 类型化错误，交由降级层处理
                    raise GraphitiUnavailableError(f"Graphiti 依赖不可用: {exc}") from exc
                self._instances[role] = cached
            return cached

    async def recall(
        self,
        query: str,
        *,
        hazard_type: str | None = None,
        region_code: str | None = None,
        limit: int = 5,
        budget_ms: float | None = None,
    ) -> list[CaseMatch]:
        """混合检索：一次查询向量嵌入 + Neo4j 读事务，零 LLM 调用。"""
        if limit <= 0:
            return []
        groups = group_ids_for(hazard_type, region_code)
        graphiti = await self._graphiti(GraphitiRole.read)
        try:
            raw = await run_with_budget(
                graphiti.search_(query=query, config=self._search_config_factory(limit), group_ids=groups),
                self._effective_budget(budget_ms),
                label="graphiti_recall",
            )
        except _db_error_types() as exc:
            raise GraphitiUnavailableError(f"图谱召回失败: {exc}", detail={"group_ids": groups}) from exc
        return self._to_matches(raw, limit=limit)

    async def learn(self, case: HazardCase) -> None:
        """唯一的写入口：一次 add_episode ≈ 4—7 次 LLM 调用，因此只允许出现在入库/复盘路径。"""
        graphiti = await self._graphiti(GraphitiRole.write)
        region = case.region_prefixes[0] if case.region_prefixes else None
        try:
            await graphiti.add_episode(
                name=case.case_id,
                episode_body=case.to_episode_text(),
                source_description=CASE_SOURCE_DESCRIPTION,
                reference_time=case.valid_at,
                source=_episode_type(),
                group_id=group_id_for(case.hazard_types[0], region),
                uuid=case_episode_uuid(case.case_id),
                update_communities=False,
                custom_extraction_instructions=CASE_EXTRACTION_INSTRUCTIONS,
            )
        except _db_error_types() as exc:
            raise GraphitiUnavailableError(f"案例入库失败: {exc}", detail={"case_id": case.case_id}) from exc
        # 图谱回读只给得到 uuid，本地索引必须同步收下才能补全预案字段
        self._index[case.case_id] = case

    async def prepare_schema(self) -> None:
        """建好向量/全文索引（幂等）：否则 cosine 与 bm25 两路检索直接报错。"""
        graphiti = await self._graphiti(GraphitiRole.write)
        await graphiti.build_indices_and_constraints()

    async def close(self) -> None:
        for instance in list(self._instances.values()):
            await instance.close()
        self._instances.clear()

    def _effective_budget(self, budget_ms: float | None) -> float | None:
        return budget_ms if budget_ms is not None else self._default_budget_ms

    def _to_matches(self, raw: Any, *, limit: int) -> list[CaseMatch]:
        """把 SearchResults 归并为"案例 → 证据"；RRF 分数只表序，故按组内最大原始分归一。"""
        collected: dict[str, dict[str, Any]] = {}

        def slot(case_id: str) -> dict[str, Any]:
            return collected.setdefault(
                case_id,
                {"raw": 0.0, "facts": [], "valid_at": "", "invalid_at": "", "evidence": []},
            )

        for episode, score in _aligned(getattr(raw, "episodes", None) or [], getattr(raw, "episode_reranker_scores", None) or []):
            case_id = case_id_from_episode_uuid(getattr(episode, "uuid", None))
            if case_id is None:
                continue
            target = slot(case_id)
            target["raw"] = max(target["raw"], float(score))
            target["evidence"].append(f"episode:{getattr(episode, 'name', None) or case_id}")
            target["valid_at"] = target["valid_at"] or _iso(getattr(episode, "valid_at", None))

        for edge, score in _aligned(getattr(raw, "edges", None) or [], getattr(raw, "edge_reranker_scores", None) or []):
            fact = str(getattr(edge, "fact", "") or "").strip()
            edge_case_ids = [
                case_id for case_id in (case_id_from_episode_uuid(uuid) for uuid in (getattr(edge, "episodes", None) or [])) if case_id
            ]
            for case_id in edge_case_ids:
                target = slot(case_id)
                target["raw"] = max(target["raw"], float(score))
                if fact and fact not in target["facts"] and len(target["facts"]) < GRAPH_FACT_LIMIT:
                    target["facts"].append(fact)
                target["valid_at"] = target["valid_at"] or _iso(getattr(edge, "valid_at", None))
                target["invalid_at"] = max(target["invalid_at"], _iso(getattr(edge, "invalid_at", None)))

        top = max((item["raw"] for item in collected.values()), default=0.0)
        matches: list[CaseMatch] = []
        for case_id, item in collected.items():
            case = self._index.get(case_id)
            matches.append(
                CaseMatch(
                    case_id=case_id,
                    score=round(item["raw"] / top if top > 0 else 1.0, 4),
                    source="graphiti",
                    matched_on=[*item["evidence"], f"facts:{len(item['facts'])}"],
                    case=case,
                    graph_facts=item["facts"],
                    valid_at=item["valid_at"] or (case.observed_at if case else None),
                    invalid_at=item["invalid_at"] or None,
                    note="" if case else "图谱命中了本地案例库之外的剧集",
                )
            )
        matches.sort(key=lambda match: (-match.score, match.case_id))
        return matches[:limit]


def _episode_type() -> Any:
    from graphiti_core.nodes import EpisodeType

    return EpisodeType.text


def _aligned(items: list[Any], scores: list[float]) -> list[tuple[Any, float]]:
    """Graphiti 约定 reranker 分数与结果同序；长度不一致（未重排）时以位次兜底。"""
    if not items:
        return []
    if len(scores) == len(items):
        return list(zip(items, scores, strict=True))
    return [(item, float(len(items) - index)) for index, item in enumerate(items)]
