"""进程内案例库：确定性关键词打分、灾种/区域过滤、时效惩罚与学习可见性。

本文件全部在无图谱、无 LLM、无网络条件下运行（降级链的真实形态）。
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from aegis.knowledge.cases import HazardCase, load_builtin_cases
from aegis.knowledge.memory_store import InMemoryKnowledgeProvider, tokenize


@pytest.fixture
def provider() -> InMemoryKnowledgeProvider:
    return InMemoryKnowledgeProvider()


class TestTokenizer:
    def test_ascii_tokens_kept_whole_and_lowercased(self) -> None:
        assert "debris_flow" in tokenize("Debris_Flow burst")

    def test_cjk_run_yields_whole_plus_bigrams(self) -> None:
        tokens = tokenize("泥位超阈值")
        assert "泥位超阈值" in tokens
        assert "泥位" in tokens and "位超" in tokens

    def test_single_characters_are_dropped(self) -> None:
        assert "冰" not in tokenize("冰 湖")

    def test_deduplicated_and_order_stable(self) -> None:
        first = tokenize("泥石流 预警 泥石流")
        assert first.count(next(token for token in first if token == "泥石流")) == 1
        assert first == tokenize("泥石流 预警 泥石流")

    @given(st.text(max_size=60))
    def test_tokenize_is_deterministic_for_any_text(self, value: str) -> None:
        assert tokenize(value) == tokenize(value)
        assert len(set(tokenize(value))) == len(tokenize(value))


class TestRecallBasics:
    async def test_builtin_library_is_loaded(self, provider: InMemoryKnowledgeProvider) -> None:
        assert len(provider) == len(load_builtin_cases())
        assert provider.name == "in_memory"

    async def test_rain_debris_query_puts_immediate_evacuation_first(self, provider: InMemoryKnowledgeProvider) -> None:
        matches = await provider.recall("短时雨强陡增 泥位超阈值 沟口要不要转移", hazard_type="debris_flow", region_code="540121")
        assert matches
        assert matches[0].case_id == "case_df_gully_evacuate"
        assert matches[0].source == "in_memory"
        assert matches[0].usable
        assert not matches[0].degraded

    async def test_scores_are_descending_and_case_ids_break_ties(self, provider: InMemoryKnowledgeProvider) -> None:
        matches = await provider.recall("泥石流 转移 监测", region_code="540121", limit=8)
        assert [m.score for m in matches] == sorted((m.score for m in matches), reverse=True)
        assert matches == await provider.recall("泥石流 转移 监测", region_code="540121", limit=8)

    async def test_limit_and_zero_limit_semantics(self, provider: InMemoryKnowledgeProvider) -> None:
        assert len(await provider.recall("泥石流", hazard_type="debris_flow", limit=2)) == 2
        assert await provider.recall("泥石流", limit=0) == []

    async def test_matched_on_explains_which_field_hit(self, provider: InMemoryKnowledgeProvider) -> None:
        matches = await provider.recall("泥位", hazard_type="debris_flow", region_code="540121")
        assert any(token.startswith("trigger_signals:") for token in matches[0].matched_on)

    async def test_budget_hint_does_not_change_results(self, provider: InMemoryKnowledgeProvider) -> None:
        plain = await provider.recall("冰湖溃决 下游撤离", hazard_type="lake_outburst")
        budgeted = await provider.recall("冰湖溃决 下游撤离", hazard_type="lake_outburst", budget_ms=60_000)
        assert [m.case_id for m in plain] == [m.case_id for m in budgeted]


class TestFilters:
    async def test_hazard_filter_excludes_other_hazards(self, provider: InMemoryKnowledgeProvider) -> None:
        matches = await provider.recall("转移 封控 监测", hazard_type="avalanche", region_code="540121", limit=10)
        assert matches
        assert all("avalanche" in m.hazard_types for m in matches)

    async def test_region_prefix_excludes_other_prefectures(self, provider: InMemoryKnowledgeProvider) -> None:
        matches = await provider.recall("牲畜 棚圈 饲草", hazard_type="ice_snow", region_code="540121", limit=10)
        assert all(m.case_id != "case_sd_livestock_shelter" for m in matches)

    async def test_matching_region_prefecture_lifts_the_case(self, provider: InMemoryKnowledgeProvider) -> None:
        matches = await provider.recall("牲畜 棚圈 饲草", hazard_type="ice_snow", region_code="540600", limit=10)
        assert matches[0].case_id == "case_sd_livestock_shelter"
        assert any(token.startswith("region:") for token in matches[0].matched_on)

    async def test_unknown_hazard_widens_instead_of_returning_nothing(self, provider: InMemoryKnowledgeProvider) -> None:
        matches = await provider.recall("冰湖 水位", hazard_type="glacier_advance", region_code="540400", limit=5)
        assert matches, "灾种词表外时应放宽灾种，保证预案生成有内容可用"

    async def test_empty_query_with_hazard_still_returns_that_hazard(self, provider: InMemoryKnowledgeProvider) -> None:
        matches = await provider.recall("", hazard_type="lake_outburst", limit=5)
        assert matches
        assert all("lake_outburst" in m.hazard_types for m in matches)

    async def test_no_signal_query_returns_nothing(self, provider: InMemoryKnowledgeProvider) -> None:
        assert await provider.recall("xyzzy") == []


class TestDelayPenalty:
    async def test_faster_case_outranks_slower_when_textually_equal(self) -> None:
        quick = _twin(delay=0.5)
        slow = _twin(delay=48.0, case_id="case_tibet_slow_twin")
        provider = InMemoryKnowledgeProvider(cases=[slow, quick])
        matches = await provider.recall("冰湖 巡护 水位", region_code="540121")
        assert [m.case_id for m in matches] == ["case_tibet_fast_twin", "case_tibet_slow_twin"]

    async def test_penalty_is_capped(self) -> None:
        extreme = _twin(case_id="case_tibet_extreme", delay=720.0)
        matches = await InMemoryKnowledgeProvider(cases=[extreme]).recall("冰湖 巡护", region_code="540121")
        assert matches and matches[0].score > 0


class TestLearn:
    async def test_learned_case_becomes_recallable(self) -> None:
        provider = InMemoryKnowledgeProvider(cases=[])
        assert await provider.recall("堰塞湖 壅高 处置", hazard_type="quake_triggered") == []
        await provider.learn(_twin(case_id="case_tibet_quake_lake", hazard_types=["quake_triggered"]))
        matches = await provider.recall("堰塞湖 壅高 处置", hazard_type="quake_triggered")
        assert [m.case_id for m in matches] == ["case_tibet_quake_lake"]

    async def test_relearning_same_id_overrides_the_case(self) -> None:
        provider = InMemoryKnowledgeProvider(cases=[_twin(confidence=0.4)])
        await provider.learn(_twin(confidence=0.95))
        assert len(provider) == 1
        assert provider.cases[0].confidence == 0.95

    async def test_learning_does_not_mutate_the_builtin_library(self) -> None:
        builtin = load_builtin_cases()
        provider = InMemoryKnowledgeProvider()
        await provider.learn(_twin(case_id="case_tibet_new_entry"))
        assert len(provider) == len(builtin) + 1
        assert len(load_builtin_cases()) == len(builtin)


def _twin(**overrides: object) -> HazardCase:
    if "delay" in overrides:  # 测试侧简写：delay → estimated_delay_hours
        overrides["estimated_delay_hours"] = overrides.pop("delay")
    base: dict[str, object] = {
        "case_id": "case_tibet_fast_twin",
        "title": "冰湖巡护水位观测",
        "hazard_types": ["lake_outburst"],
        "category": "monitor",
        "phase": "handling",
        "why_now": "冰湖水位持续壅高且坝体渗漏，需在汛期前掌握水位变化",
        "target_groups": ["地灾监测员"],
        "actions": ["巡护冰湖并记录水位"],
        "expected_effects": ["掌握壅高趋势"],
        "possible_side_effects": ["高海拔作业窗口受限"],
        "required_prerequisites": ["巡护路线安全确认"],
        "monitoring_metrics": ["水位日变化"],
        "trigger_signals": ["冰湖水位壅高"],
        "estimated_delay_hours": 0.5,
        "confidence": 0.7,
        "state_effects": {"monitoring_coverage": 0.2},
        "region_prefixes": ["5401"],
        "applies_to_levels": [2, 3],
        "observed_at": "2025-06-01T00:00:00Z",
    }
    base.update(overrides)
    return HazardCase.model_validate(base)
