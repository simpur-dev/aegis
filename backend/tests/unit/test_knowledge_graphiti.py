"""Graphiti 实现的单元验证：无图谱、无 LLM 条件下用注入替身跑通读写分离与命名规则。

核心门禁在这里：`recall()` 结构上不可能触碰 LLM 客户端（读实例的 llm_client 是抛错守卫），
且召回路径不得创建写实例（否则 add_episode 会被塞进 ≤3min 的预警链路）。
"""

from __future__ import annotations

import ast
import asyncio
import builtins
import re
import sys
from datetime import datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from aegis.config import Settings
from aegis.errors import AegisError, DeadlineExceededError
from aegis.knowledge import graphiti_store
from aegis.knowledge.cases import HazardCase, load_builtin_cases
from aegis.knowledge.graphiti_store import (
    DASHSCOPE_EMBEDDING_BATCH_SIZE,
    DEFAULT_RECALL_BUDGET_MS,
    GraphitiConfig,
    GraphitiKnowledgeProvider,
    GraphitiRole,
    GraphitiUnavailableError,
    LlmCallInRecallError,
    ReadGuards,
    case_episode_uuid,
    case_id_from_episode_uuid,
    group_id_for,
    group_ids_for,
    hybrid_search_config,
    make_read_guards,
    region_scope,
    sanitize_graph_token,
)

QUERY = "冰湖水位骤降 下游要不要撤"
MODULE_PATH = Path(graphiti_store.__file__)


def _episode(uuid: str, name: str, valid_at: datetime | None = None) -> SimpleNamespace:
    return SimpleNamespace(uuid=uuid, name=name, content=QUERY, valid_at=valid_at)


def _edge(
    uuid: str,
    fact: str,
    episodes: list[str],
    *,
    valid_at: datetime | None,
    invalid_at: datetime | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(uuid=uuid, fact=fact, episodes=episodes, valid_at=valid_at, invalid_at=invalid_at)


class FakeGraphiti:
    """Graphiti 替身：只记录调用，不做任何模型或数据库动作。

    读替身在 `search_` 里主动试探一次 LLM 客户端与重排器——模拟"检索内部想调模型"，
    用来证明生产代码注入的守卫确实当场拦下，而不是只在测试里贴个标签。
    """

    def __init__(self, role: GraphitiRole, guards: ReadGuards) -> None:
        self.role = role
        self.llm_client = guards.llm_client
        self.cross_encoder = guards.cross_encoder
        self.search_calls: list[dict[str, Any]] = []
        self.add_calls: list[dict[str, Any]] = []
        self.closed = False
        self.index_built = 0
        self.results = SimpleNamespace(episodes=[], edges=[], episode_reranker_scores=[], edge_reranker_scores=[])
        self.delay_ms = 0.0
        self.db_error: Exception | None = None

    async def search_(self, **kwargs: Any) -> SimpleNamespace:
        self.search_calls.append(kwargs)
        if self.role is GraphitiRole.read:
            with pytest.raises(LlmCallInRecallError):
                await self.llm_client.generate_response([])
            with pytest.raises(LlmCallInRecallError):
                await self.cross_encoder.rank("查询", ["段落"])
        if self.db_error is not None:
            raise self.db_error
        if self.delay_ms:
            await asyncio.sleep(self.delay_ms / 1000)
        return self.results

    async def add_episode(self, **kwargs: Any) -> SimpleNamespace:
        self.add_calls.append(kwargs)
        if self.db_error is not None:
            raise self.db_error
        return SimpleNamespace(feature_warnings=[])

    async def build_indices_and_constraints(self) -> None:
        self.index_built += 1

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def cases() -> list[HazardCase]:
    return load_builtin_cases()


@pytest.fixture
def config() -> GraphitiConfig:
    return GraphitiConfig(uri="bolt://127.0.0.1:7687", llm_api_key="sk-test", llm_base_url="https://example.invalid/v1")


def provider_for(
    config: GraphitiConfig,
    cases: list[HazardCase],
    registry: dict[GraphitiRole, FakeGraphiti],
    **kw: Any,
) -> GraphitiKnowledgeProvider:
    def factory(role: GraphitiRole, guards: Any) -> FakeGraphiti:
        return registry.setdefault(role, FakeGraphiti(role, guards()))

    return GraphitiKnowledgeProvider(config=config, cases=cases, graphiti_factory=factory, **kw)


class TestGraphNamingRule:
    def test_group_id_is_the_specific_pair(self) -> None:
        assert group_id_for("debris_flow", "540121") == "aegis_case_debris_flow_5401"

    def test_missing_dimensions_become_all(self) -> None:
        assert group_id_for(None, None) == "aegis_case_all_all"
        assert group_id_for("avalanche", None) == "aegis_case_avalanche_all"
        assert group_id_for(None, "540600") == "aegis_case_all_5406"

    def test_scope_uses_prefecture_digits(self) -> None:
        assert region_scope("540121") == "5401"
        assert region_scope("5401") == "5401"
        assert region_scope("54") == "54"
        assert region_scope(None) == ""

    def test_tokens_are_sanitized(self) -> None:
        assert sanitize_graph_token("Debris Flow!") == "debris_flow"
        assert sanitize_graph_token("rain_10min") == "rain_10min"
        assert sanitize_graph_token("泥石流") == ""
        assert group_id_for("泥石流", None) == "aegis_case_all_all"

    def test_recall_fanout_is_specific_to_general_and_deduped(self) -> None:
        assert group_ids_for("lake_outburst", "540421") == [
            "aegis_case_lake_outburst_5404",
            "aegis_case_lake_outburst_all",
            "aegis_case_all_5404",
            "aegis_case_all_all",
        ]
        assert group_ids_for(None, None) == ["aegis_case_all_all"]

    def test_write_cell_is_findable_by_recall(self) -> None:
        # 写入落最具体的一格，召回扇出必须能查到它——两处共用同一函数，规则不会漂移
        assert group_id_for("ice_snow", "5406") in group_ids_for("ice_snow", "540621")

    def test_group_ids_pass_graphiti_validation(self) -> None:
        """Graphiti 用 `^[a-zA-Z0-9_-]+$` 校验 group_id：点号会被当场拒绝（真实 add_episode 踩到的坑）。"""
        pattern = re.compile(r"^[a-zA-Z0-9_-]+$")
        for hazard, region in [("debris_flow", "540121"), (None, None), ("ice_snow", "54"), ("泥石流", "5401")]:
            for group in group_ids_for(hazard, region):
                assert pattern.match(group), group

    def test_episode_uuid_roundtrip(self) -> None:
        case = load_builtin_cases()[0]
        assert case_id_from_episode_uuid(case_episode_uuid(case.case_id)) == case.case_id

    def test_foreign_episode_uuids_are_ignored(self) -> None:
        assert case_id_from_episode_uuid("episode:some_other_feed") is None
        assert case_id_from_episode_uuid("node_abc") is None
        assert case_id_from_episode_uuid(None) is None


class TestRecallNeverCallsLlm:
    """P0 硬约束：Graphiti 的 add_episode 每次约 4—7 次模型调用，召回一次都不许有。"""

    async def test_recall_uses_only_the_guarded_read_client(self, config: GraphitiConfig, cases: list[HazardCase]) -> None:
        registry: dict[GraphitiRole, FakeGraphiti] = {}
        provider = provider_for(config, cases, registry)
        await provider.recall(QUERY, hazard_type="lake_outburst", region_code="540421")

        read = registry[GraphitiRole.read]
        assert len(read.search_calls) == 1
        assert read.llm_client is provider.read_guards.llm_client
        assert provider.read_guards.llm_calls == 1, "读实例的 LLM 客户端必须是会自增计数的抛错守卫"
        assert GraphitiRole.write not in registry, "召回不得创建写实例（add_episode 只属于入库路径）"
        assert read.add_calls == []

    async def test_guard_refuses_every_llm_entry_point(self) -> None:
        guards = make_read_guards()
        with pytest.raises(LlmCallInRecallError) as caught:
            await guards.llm_client.generate_response([], None, 16_384)
        assert caught.value.code.value == "E_INTERNAL"
        assert not caught.value.retryable
        assert guards.llm_calls == 1

        with pytest.raises(LlmCallInRecallError):
            await guards.llm_client._generate_response([])
        assert guards.llm_calls == 2

        with pytest.raises(LlmCallInRecallError):
            await guards.cross_encoder.rank("查询", ["段落一", "段落二"])

    def test_guards_satisfy_graphiti_client_contracts(self) -> None:
        pytest.importorskip("graphiti_core")
        from graphiti_core.cross_encoder.client import CrossEncoderClient
        from graphiti_core.llm_client.client import LLMClient

        guards = make_read_guards()
        # Graphiti 用 pydantic 校验客户端类型：鸭子类型会被拒，所以守卫必须真继承抽象基类
        assert isinstance(guards.llm_client, LLMClient)
        assert isinstance(guards.cross_encoder, CrossEncoderClient)

    def test_guards_are_built_once_per_provider(self, config: GraphitiConfig, cases: list[HazardCase]) -> None:
        provider = provider_for(config, cases, {})
        assert provider.read_guards is provider.read_guards

    def test_search_config_never_selects_cross_encoder_reranker(self) -> None:
        built = hybrid_search_config(4)
        rerankers = [built.edge_config.reranker, built.node_config.reranker, built.episode_config.reranker]
        assert {reranker.name for reranker in rerankers} == {"rrf"}

    def test_search_config_wires_bm25_vector_and_graph_traversal(self) -> None:
        built = hybrid_search_config(7)
        assert {m.value for m in built.edge_config.search_methods} == {"bm25", "cosine_similarity", "breadth_first_search"}
        assert {m.value for m in built.node_config.search_methods} == {"bm25", "cosine_similarity", "breadth_first_search"}
        assert built.limit == 7

    async def test_recall_passes_fanned_out_group_ids(self, config: GraphitiConfig, cases: list[HazardCase]) -> None:
        registry: dict[GraphitiRole, FakeGraphiti] = {}
        provider = provider_for(config, cases, registry)
        await provider.recall(QUERY, hazard_type="debris_flow", region_code="540121", limit=3)
        call = registry[GraphitiRole.read].search_calls[0]
        assert call["query"] == QUERY
        assert call["group_ids"] == group_ids_for("debris_flow", "540121")
        assert call["config"].limit == 3

    async def test_recall_honours_the_caller_budget(self, config: GraphitiConfig, cases: list[HazardCase]) -> None:
        registry: dict[GraphitiRole, FakeGraphiti] = {}
        provider = provider_for(config, cases, registry)
        read_fake(registry, provider).delay_ms = 60
        with pytest.raises(DeadlineExceededError):
            await provider.recall(QUERY, budget_ms=10)

    def test_default_budget_is_advertised(self, config: GraphitiConfig, cases: list[HazardCase]) -> None:
        assert provider_for(config, cases, {}).default_budget_ms == DEFAULT_RECALL_BUDGET_MS


def read_fake(registry: dict[GraphitiRole, FakeGraphiti], provider: GraphitiKnowledgeProvider) -> FakeGraphiti:
    """把读替身预先放进注册表：provider 首次召回拿到的就是它，测试可先布置返回结果。"""
    return registry.setdefault(GraphitiRole.read, FakeGraphiti(GraphitiRole.read, provider.read_guards))


class TestRecallMapping:
    async def test_episodes_and_edges_map_back_to_local_cases(self, config: GraphitiConfig, cases: list[HazardCase]) -> None:
        case = next(c for c in cases if c.case_id == "case_lo_downstream_evacuate")
        moment = datetime(2024, 9, 5, tzinfo=case.valid_at.tzinfo)
        registry: dict[GraphitiRole, FakeGraphiti] = {}
        provider = provider_for(config, cases, registry)
        read_fake(registry, provider).results = SimpleNamespace(
            episodes=[_episode(case_episode_uuid(case.case_id), case.case_id, moment)],
            edges=[_edge("e1", "冰湖水位单日骤降说明溃决临近", [case_episode_uuid(case.case_id)], valid_at=moment)],
            episode_reranker_scores=[0.03],
            edge_reranker_scores=[0.01],
        )
        hit = (await provider.recall(QUERY, hazard_type="lake_outburst"))[0]
        assert hit.source == "graphiti"
        assert hit.case is not None and hit.case.case_id == case.case_id
        assert hit.graph_facts == ["冰湖水位单日骤降说明溃决临近"]
        assert hit.valid_at.endswith("Z")
        assert not hit.degraded
        assert any(token.startswith("episode:") for token in hit.matched_on)

    async def test_expired_edge_carries_both_time_axes(self, config: GraphitiConfig, cases: list[HazardCase]) -> None:
        case = cases[0]
        valid = datetime(2024, 7, 18, tzinfo=case.valid_at.tzinfo)
        stale = datetime(2025, 7, 18, tzinfo=case.valid_at.tzinfo)
        registry: dict[GraphitiRole, FakeGraphiti] = {}
        provider = provider_for(config, cases, registry)
        read_fake(registry, provider).results = SimpleNamespace(
            episodes=[_episode(case_episode_uuid(case.case_id), case.case_id, valid)],
            edges=[_edge("e1", "该处置已不适用", [case_episode_uuid(case.case_id)], valid_at=valid, invalid_at=stale)],
            episode_reranker_scores=[0.02],
            edge_reranker_scores=[0.02],
        )
        hit = (await provider.recall(QUERY))[0]
        assert hit.valid_at and hit.invalid_at
        assert hit.invalid_at > hit.valid_at, "双时态：失效时间必须晚于生效时间"

    async def test_scores_are_normalised_and_sorted(self, config: GraphitiConfig, cases: list[HazardCase]) -> None:
        registry: dict[GraphitiRole, FakeGraphiti] = {}
        provider = provider_for(config, cases, registry)
        read_fake(registry, provider).results = SimpleNamespace(
            episodes=[
                _episode(case_episode_uuid("case_lo_chain_watch"), "case_lo_chain_watch"),
                _episode(case_episode_uuid("case_lo_gauge_survey"), "case_lo_gauge_survey"),
            ],
            edges=[],
            episode_reranker_scores=[0.01, 0.04],
            edge_reranker_scores=[],
        )
        assert [(m.case_id, m.score) for m in await provider.recall(QUERY, limit=5)] == [
            ("case_lo_gauge_survey", 1.0),
            ("case_lo_chain_watch", 0.25),
        ]

    async def test_limit_is_applied_after_ranking(self, config: GraphitiConfig, cases: list[HazardCase]) -> None:
        registry: dict[GraphitiRole, FakeGraphiti] = {}
        provider = provider_for(config, cases, registry)
        read_fake(registry, provider).results = SimpleNamespace(
            episodes=[_episode(case_episode_uuid(c.case_id), c.case_id) for c in cases[:4]],
            edges=[],
            episode_reranker_scores=[0.4, 0.3, 0.2, 0.1],
            edge_reranker_scores=[],
        )
        assert len(await provider.recall(QUERY, limit=2)) == 2

    async def test_unknown_episode_uuids_are_dropped(self, config: GraphitiConfig, cases: list[HazardCase]) -> None:
        registry: dict[GraphitiRole, FakeGraphiti] = {}
        provider = provider_for(config, cases, registry)
        read_fake(registry, provider).results = SimpleNamespace(
            episodes=[_episode("episode:not_a_case", "外部剧集")],
            edges=[_edge("e1", "与任何案例无关的事实", ["node_uuid"], valid_at=None)],
            episode_reranker_scores=[0.1],
            edge_reranker_scores=[0.1],
        )
        assert await provider.recall(QUERY) == []

    async def test_graph_hit_outside_local_index_is_flagged(self, config: GraphitiConfig, cases: list[HazardCase]) -> None:
        registry: dict[GraphitiRole, FakeGraphiti] = {}
        provider = provider_for(config, [cases[0]], registry)
        read_fake(registry, provider).results = SimpleNamespace(
            episodes=[_episode(case_episode_uuid("case_lo_gauge_survey"), "case_lo_gauge_survey")],
            edges=[],
            episode_reranker_scores=[0.05],
            edge_reranker_scores=[],
        )
        hit = (await provider.recall(QUERY))[0]
        assert hit.case is None and not hit.usable and hit.note

    async def test_learned_case_becomes_resolvable_without_restarting(self, config: GraphitiConfig) -> None:
        registry: dict[GraphitiRole, FakeGraphiti] = {}
        provider = provider_for(config, [], registry)
        case = load_builtin_cases()[0]
        await provider.learn(case)
        read_fake(registry, provider).results = SimpleNamespace(
            episodes=[_episode(case_episode_uuid(case.case_id), case.case_id)],
            edges=[],
            episode_reranker_scores=[0.05],
            edge_reranker_scores=[],
        )
        hit = (await provider.recall(QUERY))[0]
        assert hit.case is not None and hit.usable

    async def test_db_outage_is_reported_as_not_ready(self, config: GraphitiConfig, cases: list[HazardCase]) -> None:
        from neo4j.exceptions import ServiceUnavailable

        registry: dict[GraphitiRole, FakeGraphiti] = {}
        provider = provider_for(config, cases, registry)
        read_fake(registry, provider).db_error = ServiceUnavailable("bolt 连接被拒绝")
        with pytest.raises(GraphitiUnavailableError):
            await provider.recall(QUERY)

    async def test_zero_limit_short_circuits(self, config: GraphitiConfig, cases: list[HazardCase]) -> None:
        registry: dict[GraphitiRole, FakeGraphiti] = {}
        provider = provider_for(config, cases, registry)
        assert await provider.recall(QUERY, limit=0) == []
        assert registry == {}


class TestLearn:
    async def test_learn_writes_exactly_one_episode_with_case_identity(self, config: GraphitiConfig, cases: list[HazardCase]) -> None:
        registry: dict[GraphitiRole, FakeGraphiti] = {}
        provider = provider_for(config, cases, registry)
        case = next(c for c in cases if c.case_id == "case_df_gully_evacuate")
        await provider.learn(case)

        assert GraphitiRole.read not in registry, "入库不应建立读实例"
        write = registry[GraphitiRole.write]
        assert len(write.add_calls) == 1
        call = write.add_calls[0]
        assert call["name"] == case.case_id
        assert call["uuid"] == case_episode_uuid(case.case_id)
        assert call["episode_body"] == case.to_episode_text()
        assert call["group_id"] == "aegis_case_debris_flow_5401"
        assert call["reference_time"] == case.valid_at
        assert call["update_communities"] is False
        assert "预案案例" in call["custom_extraction_instructions"]
        assert call["source"] is graphiti_store._episode_type()

    async def test_learn_indexes_the_case_for_later_recall(self, config: GraphitiConfig) -> None:
        registry: dict[GraphitiRole, FakeGraphiti] = {}
        provider = provider_for(config, [], registry)
        await provider.learn(load_builtin_cases()[0])
        assert provider.case_ids == [load_builtin_cases()[0].case_id]

    async def test_learn_reports_db_outage_as_not_ready(self, config: GraphitiConfig, cases: list[HazardCase]) -> None:
        from neo4j.exceptions import AuthError

        registry: dict[GraphitiRole, FakeGraphiti] = {}
        provider = provider_for(config, cases, registry)
        write_fake = registry.setdefault(GraphitiRole.write, FakeGraphiti(GraphitiRole.write, provider.read_guards))
        write_fake.db_error = AuthError("凭据不符")
        with pytest.raises(GraphitiUnavailableError):
            await provider.learn(cases[0])

    async def test_schema_preparation_targets_write_instance(self, config: GraphitiConfig, cases: list[HazardCase]) -> None:
        registry: dict[GraphitiRole, FakeGraphiti] = {}
        provider = provider_for(config, cases, registry)
        await provider.prepare_schema()
        assert registry[GraphitiRole.write].index_built == 1


class TestAssembly:
    def test_missing_uri_is_a_config_error(self, cases: list[HazardCase]) -> None:
        with pytest.raises(AegisError):
            GraphitiKnowledgeProvider(config=GraphitiConfig(uri=""), cases=cases)

    async def test_close_releases_both_instances(self, config: GraphitiConfig, cases: list[HazardCase]) -> None:
        registry: dict[GraphitiRole, FakeGraphiti] = {}
        provider = provider_for(config, cases, registry)
        await provider.recall(QUERY)
        await provider.learn(cases[0])
        await provider.close()
        assert all(fake.closed for fake in registry.values())
        assert provider.instance_of(GraphitiRole.read) is None

    def test_config_from_parts_reads_the_declared_settings_fields(self) -> None:
        """图谱凭据与嵌入口径都从配置面读：字段名一旦写错，deploy 一致性测试就会红，
        而不是静默用默认值连到错误的库上。"""
        cfg = Settings(
            env="test",
            neo4j_user="aegis",
            neo4j_password="secret",
            neo4j_database="aegis_kg",
            knowledge_embedding_model="text-embedding-v4",
            knowledge_embedding_dim=2048,
            llm_api_key="sk-x",
            llm_model="qwen-plus",
        )
        built = GraphitiConfig.from_parts(uri="bolt://x:7687", settings=cfg)

        assert (built.user, built.password, built.database) == ("aegis", "secret", "aegis_kg")
        assert (built.embedding_model, built.embedding_dim) == ("text-embedding-v4", 2048)
        assert (built.llm_api_key, built.llm_model) == ("sk-x", "qwen-plus")

    def test_explicit_arguments_beat_the_settings(self) -> None:
        built = GraphitiConfig.from_parts(uri="bolt://x:7687", settings=Settings(env="test"), user="override", database="db2")
        assert (built.user, built.database) == ("override", "db2")

    def test_config_ignores_absent_settings_fields(self) -> None:
        assert GraphitiConfig.from_parts(uri="bolt://x:7687", settings=object()).llm_api_key == ""

    def test_batch_size_matches_dashscope_limit(self) -> None:
        assert DASHSCOPE_EMBEDDING_BATCH_SIZE == 10

    def test_missing_credentials_fail_typed_for_both_builders(self) -> None:
        """缺凭据要报成类型化的"这条腿不可用"，并且理由点名是哪个键。

        真 Neo4j 上实测到的缺陷：只有 `build_llm_client` 设了门禁，而 `build_graphiti()`
        两个角色都会先构造 embedder → 读路径抛的是 SDK 裸 `OpenAIError`，
        装配面把它归成一个无法解释的错误，而不是"没配凭据"。
        """
        empty = GraphitiConfig(uri="bolt://x:7687")
        for name in ("build_batched_embedder", "build_llm_client"):
            builder = getattr(graphiti_store, name)
            with pytest.raises(GraphitiUnavailableError) as caught:
                builder(empty)
            assert "llm_api_key" in str(caught.value), f"{name} 的失败理由没点名凭据"

    def test_embedder_gate_runs_before_importing_the_graph_stack(self) -> None:
        """门禁必须排在 `import graphiti_core` 之前：没装 graph extra 的机器上，
        缺凭据也该报"缺凭据"，而不是被 ImportError 抢先后报成"依赖不可用"。"""
        source = (Path(graphiti_store.__file__).read_text(encoding="utf-8")).split("def build_batched_embedder")[1]
        gate = source.index("if not config.llm_api_key")
        imported = source.index("from graphiti_core.embedder.openai import")
        assert gate < imported, "凭据门禁被放到了图谱依赖导入之后"


class TestLazyImports:
    """图谱与驱动依赖必须留在函数体内：单测与降级模式都不能要求 neo4j driver 可导入。"""

    def test_no_module_level_graph_import_statements(self) -> None:
        forbidden = {"graphiti_core", "neo4j", "openai"}
        top_level: list[str] = []
        for node in ast.parse(MODULE_PATH.read_text(encoding="utf-8")).body:
            if isinstance(node, ast.Import):
                top_level.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                top_level.append(node.module)
        assert not [name for name in top_level if name.split(".")[0] in forbidden]

    def test_module_can_be_executed_without_graph_stack(self) -> None:
        blocked = {"graphiti_core", "neo4j", "openai"}
        original = builtins.__import__

        def guarded_import(name: str, *args: Any, **kwargs: Any) -> Any:
            if name.split(".")[0] in blocked:
                raise ImportError(f"测试环境屏蔽了 {name}")
            return original(name, *args, **kwargs)

        probe = ModuleType("aegis_knowledge_graphiti_lazy_probe")
        sys.modules[probe.__name__] = probe  # dataclass 需要能从 sys.modules 找到所属模块
        builtins.__import__ = guarded_import
        try:
            exec(compile(MODULE_PATH.read_text(encoding="utf-8"), str(MODULE_PATH), "exec"), probe.__dict__)
        finally:
            builtins.__import__ = original
            sys.modules.pop(probe.__name__, None)

        assert probe.GROUP_PREFIX == "aegis_case"
        assert probe.group_id_for("debris_flow", "540121") == "aegis_case_debris_flow_5401"
        assert probe.DASHSCOPE_EMBEDDING_BATCH_SIZE == 10

    async def test_missing_graphiti_dependency_degrades_to_typed_error(self, config: GraphitiConfig, cases: list[HazardCase]) -> None:
        def exploding_factory(role: GraphitiRole, guards: Any) -> Any:
            raise ImportError("没有 graphiti_core 怎么办")

        provider = GraphitiKnowledgeProvider(config=config, cases=cases, graphiti_factory=exploding_factory)
        with pytest.raises(GraphitiUnavailableError):
            await provider.recall(QUERY)
