"""真实 Neo4j + Graphiti 集成，分两档跑（凭据只挡住需要模型的那一档）：

第一档**不需要模型凭据**（`AEGIS_TEST_NEO4J` 一设就跑）：连得上真图、缺凭据时报成类型化的
"这条腿不可用"。这一档的价值是把"图谱没起/凭据没配"两种情况分开——否则整份文件一起跳过，
连 Neo4j 通不通都不知道。

    AEGIS_TEST_NEO4J=bolt://127.0.0.1:7687 \
    AEGIS_NEO4J_USER=neo4j AEGIS_NEO4J_PASSWORD=... \
    uv run pytest -q -m slow tests/integration/test_knowledge_graphiti_live.py

第二档要写入路径，必须有模型凭据（图谱抽取与查询向量化都由模型完成）：

    AEGIS_LLM_API_KEY=sk-... AEGIS_LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1 \
    ...（同上，再加这两个键）

用例只往 `aegis_case_pytest_live_*` 这一个隔离图里写，teardown 用 remove_episode 清理，
反复运行不会污染真实案例图。
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest

from aegis.config import get_settings
from aegis.knowledge.cases import HazardCase
from aegis.knowledge.graphiti_store import (
    GraphitiConfig,
    GraphitiKnowledgeProvider,
    GraphitiRole,
    GraphitiUnavailableError,
    case_episode_uuid,
)

NEO4J_URI = os.getenv("AEGIS_TEST_NEO4J", "")
LIVE_MARKER_HAZARD = "pytest_live"

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not NEO4J_URI, reason="未设置 AEGIS_TEST_NEO4J，跳过真实图谱集成"),
]
needs_llm = pytest.mark.skipif(not get_settings().llm_api_key, reason="未配置 AEGIS_LLM_API_KEY，无法执行图谱抽取写入与向量检索")


def _live_case(case_id: str, *, title: str, observed_at: str, signal: str) -> HazardCase:
    return HazardCase.model_validate(
        {
            "case_id": case_id,
            "title": title,
            "hazard_types": [LIVE_MARKER_HAZARD],
            "category": "monitor",
            "phase": "response",
            "why_now": f"集成测试专用标记串：{signal}",
            "target_groups": ["集成测试替身"],
            "actions": [f"按 {signal} 加密巡护"],
            "expected_effects": ["集成测试可核对的命中"],
            "possible_side_effects": ["无真实处置影响"],
            "required_prerequisites": ["测试图已建索引"],
            "monitoring_metrics": [f"{signal} 命中率"],
            "trigger_signals": [f"{signal} 出现"],
            "estimated_delay_hours": 0.5,
            "confidence": 0.7,
            "state_effects": {"monitoring_coverage": 0.2},
            "region_prefixes": ["5401"],
            "applies_to_levels": [2],
            "source_note": "tests/integration/test_knowledge_graphiti_live.py",
            "observed_at": observed_at,
        }
    )


@pytest.fixture
async def provider() -> AsyncIterator[GraphitiKnowledgeProvider]:
    config = GraphitiConfig.from_parts(uri=NEO4J_URI, settings=get_settings())
    instance = GraphitiKnowledgeProvider(config=config, default_budget_ms=30_000.0)
    await instance.prepare_schema()
    yield instance
    await instance.close()


async def _cleanup(instance: GraphitiKnowledgeProvider, case_ids: list[str]) -> None:
    write = instance.instance_of(GraphitiRole.write)
    if write is None:
        return
    for case_id in case_ids:
        await write.remove_episode(case_episode_uuid(case_id))


class TestGraphitiLiveWithoutCredentials:
    """第一档：不需要模型凭据也能验的两件事——图连得上、缺凭据报得清楚。

    为什么值得单独一档：整份文件挂在"要有 LLM key"上，等于 Neo4j 通不通也要靠凭据才能知道；
    而"这条腿没起"和"这条腿没配凭据"是两种必须分开的降级原因（见 `GET /api/v1/integrations`）。
    """

    async def test_neo4j_accepts_the_declared_credentials(self) -> None:
        from neo4j import GraphDatabase

        settings = get_settings()
        driver = GraphDatabase.driver(NEO4J_URI, auth=(settings.neo4j_user, settings.neo4j_password))
        try:
            with driver.session() as session:
                assert session.run("RETURN 1 AS n").single()["n"] == 1
                components = session.run("CALL dbms.components() YIELD name, versions, edition RETURN name, versions, edition").single()
            # 实测形状：name 是 'Neo4j Kernel'（不是 'Neo4j'）、versions 是列表、edition 是 'community'。
            assert str(components["name"]).startswith("Neo4j"), f"连到的不是 Neo4j：{components['name']!r}"
            assert components["versions"], "读不到版本号，取证里就没法写实测版本"
            assert str(components["edition"]).lower() in {"community", "enterprise"}, f"未知的发行版：{components['edition']!r}"
        finally:
            driver.close()

    async def test_missing_llm_credentials_surface_as_typed_unavailability(self) -> None:
        settings = get_settings()
        if settings.llm_api_key:
            pytest.skip("已配置模型凭据，这条只在缺凭据时有意义")
        provider = GraphitiKnowledgeProvider(config=GraphitiConfig.from_parts(uri=NEO4J_URI, settings=settings))
        try:
            with pytest.raises(GraphitiUnavailableError) as caught:
                await provider.prepare_schema()
            assert "llm_api_key" in str(caught.value), f"降级理由没点名凭据：{caught.value}"
            assert provider.instance_of(GraphitiRole.write) is None, "建实例失败却把半截实例缓存了"
        finally:
            await provider.close()

    def test_batch_backfill_tells_the_truth_on_a_real_graph_without_llm(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """批量回填命令对着真 Neo4j + 缺模型凭据跑一遍：它必须说"一条都没进图"，而不是"导入 16 条"。

        这条值得用真图，因为替身给不出这个现场：图连得上、索引建不成、写入按类型化异常回落内存——
        三种情况混在一起时，`total` 与 `landed` 谁说了算就是这台机器上唯一能验的事。

        刻意写成同步用例：命令自己起事件循环（`asyncio.run`），在 async 用例里调用会撞进
        "running event loop"——那测的就不是这条命令了。
        """
        import json as jsonlib

        from scripts.ingest_cases import EXIT_DEGRADED, main

        from aegis import integrations
        from aegis.integrations import IntegrationState
        from aegis.knowledge.memory_store import InMemoryKnowledgeProvider
        from aegis.knowledge.provider import FallbackKnowledgeProvider

        settings = get_settings()
        if settings.llm_api_key:
            pytest.skip("已配置模型凭据，降级路径不成立（这条只在缺凭据时有意义）")
        case_ids = ["case_df_gully_evacuate", "case_lo_chain_watch"]
        chain = FallbackKnowledgeProvider(
            primary=GraphitiKnowledgeProvider(config=GraphitiConfig.from_parts(uri=NEO4J_URI, settings=settings)),
            fallback=InMemoryKnowledgeProvider(cases=[]),
        )
        monkeypatch.setattr(
            integrations,
            "build_knowledge",
            lambda *_args, **_kwargs: (chain, IntegrationState(name="knowledge", enabled=True, driver="graphiti")),
        )

        argv = [arg for case_id in case_ids for arg in ("--case-id", case_id)]
        code = main(argv)
        out = capsys.readouterr().out
        body = jsonlib.loads(out[out.index("{") :])
        assert code == EXIT_DEGRADED, f"一条都没进图却回了 {code}"
        assert (body["total"], body["landed"], body["degraded"]) == (2, 0, 2)
        assert body["durable"] is False, "目标是图谱不等于落在图谱"
        assert {item["driver"] for item in body["outcomes"]} == {"in_memory"}, "落点被读成图谱就是假事实"
        assert all("llm_api_key" in item["detail"] for item in body["outcomes"]), body["outcomes"]
        # 图谱腿"没凭据"与"库没起"必须能分开读：这一行点名的是凭据
        assert "llm_api_key" in str(body["schema_error"])
        # 关不掉只会在真驱动上出现：半截实例也要能干净退出
        assert body["close_error"] is None
        assert body["recalled_from"] == {case_id: ["in_memory"] for case_id in case_ids}


@needs_llm
class TestGraphitiLive:
    async def test_add_episode_then_hybrid_recall_finds_the_case(self, provider: GraphitiKnowledgeProvider) -> None:
        case = _live_case(
            "case_pytest_live_debris_01",
            title="集成测试：泥石流沟口巡护加密",
            observed_at="2025-07-01T00:00:00Z",
            signal="泥位骤升甲乙丙",
        )
        await provider.learn(case)
        try:
            hits = await provider.recall("泥位骤升甲乙丙 要不要加密巡护", hazard_type=LIVE_MARKER_HAZARD, region_code="540121", limit=5)
            assert hits, "写入后的案例应被混合检索召回"
            assert case.case_id in {hit.case_id for hit in hits}
            hit = next(h for h in hits if h.case_id == case.case_id)
            assert hit.case is not None, "命中必须能回指本地案例本体"
            assert hit.case.actions == case.actions
            assert hit.source == "graphiti"
            assert not hit.degraded
            assert hit.valid_at, "时序图谱命中应带回 valid_at"
        finally:
            await _cleanup(provider, [case.case_id])

    async def test_recall_is_llm_free_and_write_only_on_learn(self, provider: GraphitiKnowledgeProvider) -> None:
        case = _live_case(
            "case_pytest_live_bitemporal_02",
            title="集成测试：冰湖溃决下游撤空",
            observed_at="2025-08-01T00:00:00Z",
            signal="坝前渗漏丁戊己",
        )
        await provider.learn(case)
        write_before = provider.instance_of(GraphitiRole.write)
        guard_calls_before = provider.read_guards.llm_calls
        try:
            for _ in range(2):
                await provider.recall("坝前渗漏丁戊己 下游是否撤空", hazard_type=LIVE_MARKER_HAZARD, region_code="540121")
            assert provider.read_guards.llm_calls == guard_calls_before, "召回路径不得触发任何 LLM 调用"
            assert provider.instance_of(GraphitiRole.write) is write_before
        finally:
            await _cleanup(provider, [case.case_id])
