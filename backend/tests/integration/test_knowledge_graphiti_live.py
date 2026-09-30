"""真实 Neo4j + Graphiti 集成：证明 add_episode 与混合检索在真图上确实可用，且召回零 LLM 调用。

默认跳过。启用方式（两个条件都要满足，写入路径必须有模型凭据）：
    AEGIS_TEST_NEO4J=bolt://127.0.0.1:7687 \\
    AEGIS_LLM_API_KEY=sk-... AEGIS_LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1 \\
    uv run pytest -q -m slow tests/integration/test_knowledge_graphiti_live.py

用例只往 `aegis_case_pytest_live_*` 这一个隔离图里写，teardown 用 remove_episode 清理，
反复运行不会污染真实案例图。
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest

from aegis.config import get_settings
from aegis.knowledge.cases import HazardCase
from aegis.knowledge.graphiti_store import GraphitiConfig, GraphitiKnowledgeProvider, GraphitiRole, case_episode_uuid

NEO4J_URI = os.getenv("AEGIS_TEST_NEO4J", "")
LIVE_MARKER_HAZARD = "pytest_live"

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not NEO4J_URI, reason="未设置 AEGIS_TEST_NEO4J，跳过真实图谱集成"),
    pytest.mark.skipif(not get_settings().llm_api_key, reason="未配置 AEGIS_LLM_API_KEY，无法执行图谱抽取写入"),
]


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
