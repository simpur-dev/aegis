"""知识层契约与降级装配：Protocol 合规、预算强制、主召回失败时的内存兜底与记账。"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from pydantic import ValidationError

from aegis.config import Settings
from aegis.errors import DeadlineExceededError
from aegis.knowledge.cases import HazardCase, load_builtin_cases
from aegis.knowledge.graphiti_store import GraphitiConfig, GraphitiKnowledgeProvider, GraphitiUnavailableError
from aegis.knowledge.memory_store import InMemoryKnowledgeProvider
from aegis.knowledge.provider import (
    DEFAULT_RECALL_BUDGET_MS,
    RECALL_LATENCY_METRIC,
    CaseMatch,
    DegradationRecord,
    FallbackKnowledgeProvider,
    KnowledgeConfigError,
    KnowledgeProvider,
    LearnOutcome,
    RecallSource,
    SupportsSchemaPreparation,
    build_knowledge_provider,
    run_with_budget,
)
from aegis.observability.tracer import Tracer


class StubProvider:
    """可编排的主实现替身：延迟、异常、返回集都由测试给定。"""

    name = "stub"
    driver: RecallSource = "graphiti"

    def __init__(
        self,
        *,
        results: list[CaseMatch] | None = None,
        delay_ms: float = 0.0,
        error: Exception | None = None,
        learn_error: Exception | None = None,
    ) -> None:
        self.results = results if results is not None else []
        self.delay_ms = delay_ms
        self.error = error
        self.learn_error = learn_error
        self.recall_calls: list[dict[str, Any]] = []
        self.learned: list[HazardCase] = []

    async def recall(
        self,
        query: str,
        *,
        hazard_type: str | None = None,
        region_code: str | None = None,
        limit: int = 5,
        budget_ms: float | None = None,
    ) -> list[CaseMatch]:
        self.recall_calls.append({"query": query, "hazard_type": hazard_type, "region_code": region_code, "limit": limit})
        if self.delay_ms:
            await asyncio.sleep(self.delay_ms / 1000)
        if self.error is not None:
            raise self.error
        return self.results[:limit]

    async def learn(self, case: HazardCase) -> None:
        if self.learn_error is not None:
            raise self.learn_error
        self.learned.append(case)

    async def learn_case(self, case: HazardCase) -> LearnOutcome:
        await self.learn(case)
        return LearnOutcome(case_id=case.case_id, driver=self.driver)


def match(case: HazardCase, *, score: float = 1.0, source: RecallSource = "graphiti") -> CaseMatch:
    return CaseMatch.from_case(case, score=score, source=source)


@pytest.fixture
def case() -> HazardCase:
    return load_builtin_cases()[0]


class TestProtocol:
    def test_in_memory_and_graphiti_and_fallback_all_satisfy_the_protocol(self, case: HazardCase) -> None:
        assert isinstance(InMemoryKnowledgeProvider(), KnowledgeProvider)
        assert isinstance(GraphitiKnowledgeProvider(config=GraphitiConfig(uri="bolt://x:7687"), cases=[case]), KnowledgeProvider)
        assert isinstance(
            FallbackKnowledgeProvider(primary=StubProvider(), fallback=InMemoryKnowledgeProvider()),
            KnowledgeProvider,
        )

    def test_stub_missing_learn_is_not_a_provider(self) -> None:
        class NoLearn:
            async def recall(self, query: str, **_: Any) -> list[CaseMatch]:
                return []

        assert not isinstance(NoLearn(), KnowledgeProvider)

    def test_缺driver或learn_case的实现都不算提供者(self) -> None:
        """写路径 widening 之后，`isinstance` 必须真的在管这两个成员。

        这条断言是实测出来的：把协议成员改成只读属性后，Python 仍按"属性存在与否"判定，
        缺 `driver` 或缺 `learn_case` 都会被判 False——少了这一条，实现漂移就只剩 mypy 在管。
        """

        class NoDriver:
            async def recall(self, query: str, **_: Any) -> list[CaseMatch]:
                return []

            async def learn(self, case: HazardCase) -> None:
                del case

            async def learn_case(self, case: HazardCase) -> LearnOutcome:
                return LearnOutcome(case_id=case.case_id, driver="in_memory")

        class NoLearnCase:
            driver: RecallSource = "graphiti"

            async def recall(self, query: str, **_: Any) -> list[CaseMatch]:
                return []

            async def learn(self, case: HazardCase) -> None:
                del case

        assert not isinstance(NoDriver(), KnowledgeProvider)
        assert not isinstance(NoLearnCase(), KnowledgeProvider)


class TestCaseMatch:
    def test_from_case_carries_identity_and_valid_at(self, case: HazardCase) -> None:
        hit = CaseMatch.from_case(case, score=0.51234, source="in_memory", matched_on=["title:冰湖"])
        assert hit.case_id == case.case_id
        assert hit.score == 0.5123
        assert hit.source == "in_memory"
        assert hit.valid_at == case.observed_at
        assert hit.usable and not hit.degraded
        assert hit.title == case.title and hit.hazard_types == case.hazard_types

    def test_negative_score_is_clamped(self, case: HazardCase) -> None:
        assert CaseMatch.from_case(case, score=-3, source="in_memory").score == 0.0

    def test_dangling_graph_hit_is_not_usable(self) -> None:
        hit = CaseMatch(case_id="case_tibet_missing", score=0.4, source="graphiti", case=None)
        assert not hit.usable
        assert hit.title == "case_tibet_missing"
        assert hit.confidence == 0.0 and hit.estimated_delay_hours == 0.0
        assert hit.hazard_types == []

    def test_match_is_immutable(self, case: HazardCase) -> None:
        hit = match(case)
        with pytest.raises(ValidationError):
            hit.score = 9.9  # type: ignore[misc]


class TestRunWithBudget:
    async def test_no_budget_just_awaits(self) -> None:
        assert await run_with_budget(asyncio.sleep(0, result="ok"), None, label="x") == "ok"

    async def test_timeout_becomes_typed_deadline(self) -> None:
        with pytest.raises(DeadlineExceededError) as caught:
            await run_with_budget(asyncio.sleep(0.05), 5, label="recall")
        assert caught.value.detail["budget_ms"] == 5
        assert caught.value.retryable

    async def test_non_positive_budget_is_a_config_error(self) -> None:
        coro = asyncio.sleep(0, result=1)
        with pytest.raises(KnowledgeConfigError):
            await run_with_budget(coro, 0, label="recall")
        coro.close()  # 预算校验先于 await：显式关闭协程，不留未 await 告警


class TestFallbackRecall:
    async def test_healthy_primary_is_returned_verbatim(self, case: HazardCase) -> None:
        memory = InMemoryKnowledgeProvider()
        router = FallbackKnowledgeProvider(primary=StubProvider(results=[match(case)]), fallback=memory)
        hits = await router.recall("冰湖", hazard_type="lake_outburst")
        assert [h.case_id for h in hits] == [case.case_id]
        assert hits[0].source == "graphiti"
        assert not hits[0].degraded
        assert router.degradations == ()

    async def test_timeout_degrades_to_memory_and_marks_every_hit(self, case: HazardCase) -> None:
        memory = InMemoryKnowledgeProvider()
        router = FallbackKnowledgeProvider(primary=StubProvider(results=[match(case)], delay_ms=60), fallback=memory, default_budget_ms=10)
        hits = await router.recall("强降雨 泥位 沟口转移", hazard_type="debris_flow", region_code="540121")
        assert hits
        assert all(h.degraded and h.source == "in_memory" for h in hits)
        assert all(h.note == "primary_timeout" for h in hits)
        assert router.degradations[-1].reason == "primary_timeout"

    async def test_typed_primary_error_degrades(self, case: HazardCase) -> None:
        router = FallbackKnowledgeProvider(
            primary=StubProvider(error=GraphitiUnavailableError("图谱不可用")),
            fallback=InMemoryKnowledgeProvider(cases=[case]),
        )
        hits = await router.recall("泥位 沟口转移", hazard_type="debris_flow", region_code="540121")
        assert [h.case_id for h in hits] == [case.case_id]
        assert hits[0].note == "primary_error"
        assert "图谱不可用" in router.degradations[-1].detail

    async def test_unexpected_primary_crash_is_not_swallowed(self) -> None:
        router = FallbackKnowledgeProvider(primary=StubProvider(error=RuntimeError("实现缺陷")), fallback=InMemoryKnowledgeProvider())
        with pytest.raises(RuntimeError):
            await router.recall("冰湖")

    async def test_empty_primary_result_also_degrades(self) -> None:
        router = FallbackKnowledgeProvider(primary=StubProvider(results=[]), fallback=InMemoryKnowledgeProvider())
        hits = await router.recall("雪崩 封控", hazard_type="avalanche", region_code="540121")
        assert hits and hits[0].degraded
        assert router.degradations[-1].reason == "primary_empty"

    async def test_caller_budget_overrides_the_default(self, case: HazardCase) -> None:
        stub = StubProvider(results=[match(case)], delay_ms=50)
        router = FallbackKnowledgeProvider(
            primary=stub,
            fallback=InMemoryKnowledgeProvider(cases=[]),
            default_budget_ms=5_000,
        )
        assert await router.recall("冰湖", budget_ms=5) == []
        assert router.degradations[-1].reason == "primary_timeout"
        assert router.degradations[-1].budget_ms == 5

    async def test_query_and_filters_are_forwarded_to_primary(self) -> None:
        stub = StubProvider()
        router = FallbackKnowledgeProvider(primary=stub, fallback=InMemoryKnowledgeProvider())
        await router.recall("冰湖", hazard_type="lake_outburst", region_code="540400", limit=2)
        assert stub.recall_calls == [{"query": "冰湖", "hazard_type": "lake_outburst", "region_code": "540400", "limit": 2}]

    async def test_degradation_callback_feeds_the_chain_ledger(self, case: HazardCase) -> None:
        seen: list[DegradationRecord] = []
        router = FallbackKnowledgeProvider(
            primary=StubProvider(error=GraphitiUnavailableError("无图谱")),
            fallback=InMemoryKnowledgeProvider(cases=[case]),
            on_degradation=seen.append,
        )
        hits = await router.recall("泥位", hazard_type="debris_flow")
        assert len(seen) == 1
        assert seen[0].primary == "StubProvider"
        assert seen[0].fallback == "InMemoryKnowledgeProvider"
        assert seen[0].returned == len(hits) >= 1

    async def test_degradation_history_is_bounded(self, case: HazardCase) -> None:
        router = FallbackKnowledgeProvider(
            primary=StubProvider(error=GraphitiUnavailableError("无图谱")),
            fallback=InMemoryKnowledgeProvider(cases=[case]),
            history_size=3,
        )
        for _ in range(6):
            await router.recall("泥位", hazard_type="debris_flow")
        assert len(router.degradations) == 3

    async def test_recall_latency_is_booked_against_the_sla_ledger(self, case: HazardCase) -> None:
        tracer = Tracer()
        router = FallbackKnowledgeProvider(
            primary=StubProvider(results=[match(case)]),
            fallback=InMemoryKnowledgeProvider(),
            tracer=tracer,
            default_budget_ms=800,
        )
        await router.recall("冰湖")
        stats = tracer.ledger.stats(RECALL_LATENCY_METRIC)
        assert stats.count == 1
        assert stats.budget == 800


class TestFallbackLearn:
    async def test_健康的图谱收下案例时内存库一个字节都不写(self, case: HazardCase) -> None:
        stub = StubProvider()
        memory = InMemoryKnowledgeProvider(cases=[])
        outcome = await FallbackKnowledgeProvider(primary=stub, fallback=memory).learn_case(case)
        assert stub.learned == [case]
        assert len(memory) == 0
        # 落点必须由实现自己说：stub 是图谱替身，答案就是 graphiti 而不是"某侧"
        assert (outcome.driver, outcome.degraded, outcome.reason) == ("graphiti", False, "")

    async def test_图谱坏了案例仍进本地库且降级原因随结果返回(self, case: HazardCase) -> None:
        memory = InMemoryKnowledgeProvider(cases=[])
        router = FallbackKnowledgeProvider(
            primary=StubProvider(learn_error=GraphitiUnavailableError("图谱写入失败")),
            fallback=memory,
        )
        outcome = await router.learn_case(case)
        assert len(memory) == 1
        assert router.degradations[-1].reason == "learn_failed"
        # 只看 HTTP/返回码看不出降级：这三件事必须一起成立，否则"案例已入图"就是谎报
        assert (outcome.driver, outcome.degraded, outcome.reason) == ("in_memory", True, "learn_failed")
        assert "图谱写入失败" in outcome.detail

    async def test_learn_timeout_degrades_without_breaking_the_chain(self, case: HazardCase) -> None:
        memory = InMemoryKnowledgeProvider(cases=[])
        router = FallbackKnowledgeProvider(primary=StubProvider(learn_error=TimeoutError()), fallback=memory)
        await router.learn(case)
        assert len(memory) == 1

    async def test_内层已经降级时外层不再重复写自己的兜底(self, case: HazardCase) -> None:
        """嵌套链的事实要原样传出去，不能被外层再盖一次。

        外层若按"primary.learn 成功"来判断，内层"图谱坏了、已落内存"这件事就会被读成
        "写进了图谱"；外层若再写一次自己的内存库，同一条案例就有了两个落点。
        """
        inner_memory = InMemoryKnowledgeProvider(cases=[])
        inner = FallbackKnowledgeProvider(primary=StubProvider(learn_error=GraphitiUnavailableError("挂了")), fallback=inner_memory)
        outer_memory = InMemoryKnowledgeProvider(cases=[])
        outer = FallbackKnowledgeProvider(primary=inner, fallback=outer_memory)

        outcome = await outer.learn_case(case)
        assert (outcome.driver, outcome.degraded, outcome.reason) == ("in_memory", True, "learn_failed")
        assert len(inner_memory) == 1
        assert len(outer_memory) == 0
        assert outer.degradations == ()

    async def test_兜底也写不进去时响亮失败而不是静默丢弃(self, case: HazardCase) -> None:
        broken_memory = StubProvider(learn_error=GraphitiUnavailableError("内存库也写不进"))
        broken_memory.driver = "in_memory"
        router = FallbackKnowledgeProvider(primary=StubProvider(learn_error=TimeoutError()), fallback=broken_memory)
        with pytest.raises(GraphitiUnavailableError):
            await router.learn_case(case)

    async def test_驱动名取自主实现而不是类型名猜测(self) -> None:
        memory_stub = StubProvider()
        memory_stub.driver = "in_memory"
        assert FallbackKnowledgeProvider(primary=memory_stub, fallback=InMemoryKnowledgeProvider(cases=[])).driver == "in_memory"


class SchemaCapableStub(StubProvider):
    """只多一件事：实现了 `prepare_schema`，用来证明装配层的能力判定不是按配置字符串猜的。"""

    def __init__(self) -> None:
        super().__init__()
        self.prepared = 0

    async def prepare_schema(self) -> None:
        self.prepared += 1


class TestSchemaPreparation:
    def test_图谱替身满足能力协议_纯内存实现不满足(self) -> None:
        assert isinstance(SchemaCapableStub(), SupportsSchemaPreparation)
        assert not isinstance(InMemoryKnowledgeProvider(cases=[]), SupportsSchemaPreparation)

    async def test_降级链把索引初始化转给主实现(self) -> None:
        primary = SchemaCapableStub()
        await FallbackKnowledgeProvider(primary=primary, fallback=InMemoryKnowledgeProvider(cases=[])).prepare_schema()
        assert primary.prepared == 1

    async def test_主实现不建索引时是配置错误而不是静默跳过(self) -> None:
        # 静默跳过会让"我配了图谱所以索引建好了"这句话失去依据
        with pytest.raises(KnowledgeConfigError):
            await FallbackKnowledgeProvider(primary=StubProvider(), fallback=InMemoryKnowledgeProvider(cases=[])).prepare_schema()


class TestAssembly:
    def test_without_uri_the_chain_runs_pure_memory(self) -> None:
        provider = build_knowledge_provider(Settings(env="test"))
        assert isinstance(provider, InMemoryKnowledgeProvider)
        assert isinstance(provider, KnowledgeProvider)

    def test_with_uri_the_graph_is_primary_and_memory_is_fallback(self) -> None:
        provider = build_knowledge_provider(Settings(env="test"), graphiti_uri="bolt://127.0.0.1:7687", recall_budget_ms=900)
        assert isinstance(provider, FallbackKnowledgeProvider)
        assert isinstance(provider.primary, GraphitiKnowledgeProvider)
        assert isinstance(provider.fallback, InMemoryKnowledgeProvider)
        assert provider.primary.config.uri == "bolt://127.0.0.1:7687"
        assert provider.primary.default_budget_ms == 900  # 同一预算旋钮同时给到主实现与降级层

    def test_budget_knob_defaults_to_the_declared_constant(self) -> None:
        provider = build_knowledge_provider(Settings(env="test"), graphiti_uri="bolt://127.0.0.1:7687")
        assert isinstance(provider, FallbackKnowledgeProvider)
        assert provider.primary.default_budget_ms == DEFAULT_RECALL_BUDGET_MS

    def test_blank_uri_is_treated_as_no_graph(self) -> None:
        assert isinstance(build_knowledge_provider(Settings(env="test"), graphiti_uri=""), InMemoryKnowledgeProvider)

    def test_explicit_case_set_reaches_both_implementations(self) -> None:
        subset = load_builtin_cases()[:2]
        provider = build_knowledge_provider(Settings(env="test"), graphiti_uri="bolt://127.0.0.1:7687", cases=subset)
        assert provider.primary.case_ids == sorted(c.case_id for c in subset)
        assert len(provider.fallback) == len(subset)

    async def test_degraded_chain_still_produces_a_plan(self) -> None:
        """无图谱、无 LLM 时预案生成仍有可用输入：这是降级链的验收口径。"""
        provider = build_knowledge_provider(Settings(env="test"))
        hits = await provider.recall("强降雨后沟口泥位超阈值", hazard_type="debris_flow", region_code="540121", limit=3)
        assert hits
        assert all(hit.case is not None and hit.case.actions for hit in hits)
