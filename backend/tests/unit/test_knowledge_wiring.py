"""知识层接线测试：案例召回进了链路，且链路不会被知识层拖垮。

装配层最容易出的事故是"模块写完但没人调用"——单模块自己全绿、容器里却是死代码。
所以这个文件同时盯两头：
1. 正向：从容器出发的真实链路里 `reference_cases` 必须非空（证明 provider 真被调用）；
2. 反向：召回抛错、返回图谱裸节点、无知识层时，预警必须照常产出且语义不变。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from aegis import integrations
from aegis.agents.mock import MockAgent
from aegis.api.app import create_app
from aegis.bus import subjects
from aegis.bus.gateway import AgentGateway, ContractRegistry
from aegis.bus.inmemory import InMemoryBus
from aegis.bus.registry import AgentRegistry
from aegis.config import Settings
from aegis.connectors.simulator import HazardScenarioSimulator
from aegis.container import PlatformContainer, create_container
from aegis.domain.enums import Action, AgentType, HazardType
from aegis.domain.messages import AgentMessage, utc_now
from aegis.integrations import build_knowledge, target_of, warm_knowledge
from aegis.knowledge.cases import HazardCase, load_builtin_cases
from aegis.knowledge.graphiti_store import GraphitiUnavailableError
from aegis.knowledge.memory_store import InMemoryKnowledgeProvider
from aegis.knowledge.provider import (
    CaseMatch,
    FallbackKnowledgeProvider,
    KnowledgeProvider,
    LearnOutcome,
    SupportsSchemaPreparation,
)
from aegis.observability.tracer import Tracer
from aegis.pipeline.chain import ChainResult, HazardResponseChain
from conftest import REGION, readings_rain_burst

GRAPH_URI = "neo4j+ssc://aegis_reader:sup3rs3cr3t@10.0.0.9:7687?database=neo4j"
SECRET = "sup3rs3cr3t"


def base_settings(**overrides: Any) -> Settings:
    payload: dict[str, Any] = {
        "env": "test",
        "bus_backend": "memory",
        "simulator_enabled": False,
        "delivery_mode": "mock",
        "store_backend": "memory",
        "analytics_backend": "off",
    }
    payload.update(overrides)
    return Settings(**payload)


def builtin_case(index: int = 0) -> HazardCase:
    return load_builtin_cases()[index]


def match_of(case: HazardCase, *, score: float = 1.25) -> CaseMatch:
    return CaseMatch.from_case(case, score=score, source="in_memory", matched_on=["trigger_signals:泥位"])


class StubProvider:
    """知识提供者替身：记录入参、按需抛错，永不触碰 I/O。"""

    name = "stub"

    def __init__(self, matches: list[CaseMatch] | None = None, *, error: Exception | None = None) -> None:
        self._matches = matches if matches is not None else [match_of(builtin_case(0))]
        self._error = error
        self.calls: list[dict[str, Any]] = []

    async def recall(
        self,
        query: str,
        *,
        hazard_type: str | None = None,
        region_code: str | None = None,
        limit: int = 5,
        budget_ms: float | None = None,
    ) -> list[CaseMatch]:
        self.calls.append({"query": query, "hazard_type": hazard_type, "region_code": region_code, "limit": limit, "budget_ms": budget_ms})
        if self._error is not None:
            raise self._error
        return self._matches[:limit]

    async def learn(self, case: HazardCase) -> None:
        del case


class TestBuildKnowledge:
    def test_default_assembly_is_memory_backed_and_reports_inventory(self) -> None:
        provider, state = build_knowledge(base_settings())

        assert isinstance(provider, KnowledgeProvider)
        assert isinstance(provider, InMemoryKnowledgeProvider)
        assert state.enabled is True
        assert state.driver == "in_memory"
        # 库存量必须是真实读到的条数，不写死：案例库追加后状态接口要跟着变
        assert state.detail["fallback_cases"] == len(load_builtin_cases())
        assert state.detail["graphiti"] == ""

    def test_configured_budget_reaches_the_provider(self) -> None:
        """配置旋钮要真的落到 provider 的默认预算上，否则 knowledge_recall_budget_ms 是装饰品。"""
        provider, state = build_knowledge(base_settings(knowledge_graphiti_uri="bolt://127.0.0.1:7687", knowledge_recall_budget_ms=777.0))

        assert isinstance(provider, FallbackKnowledgeProvider)
        assert provider.primary.default_budget_ms == 777.0
        assert state.driver == "graphiti"
        assert state.detail["recall_budget_ms"] == 777.0

    def test_graphiti_credentials_never_reach_the_state(self) -> None:
        _provider, state = build_knowledge(base_settings(knowledge_graphiti_uri=GRAPH_URI))

        assert state.detail["graphiti"] == "10.0.0.9:7687"
        dumped = json.dumps(state.as_dict(), ensure_ascii=False)
        assert SECRET not in dumped
        assert "aegis_reader" not in dumped
        assert "neo4j+ssc://" not in dumped

    async def test_recall_produces_actionable_cases_without_graph_or_llm(self) -> None:
        """考核口径：图谱与 LLM 都不在位时，预案生成仍要拿到带处置动作的历史案例。"""
        provider, _state = build_knowledge(base_settings())
        hits = await provider.recall("强降雨后沟口泥位超阈值", hazard_type="debris_flow", region_code=REGION, limit=3)

        assert hits
        assert all(hit.usable and hit.case.actions for hit in hits)
        assert all(hit.degraded is False for hit in hits)


class TestTargetRedaction:
    """凭据脱敏的边界：状态接口匿名可读，切串方式错一位就是把口令写进公网响应。"""

    @pytest.mark.parametrize(
        ("uri", "expected"),
        [
            ("", ""),
            ("bolt://127.0.0.1:7687", "127.0.0.1:7687"),
            ("neo4j+ssc://aegis_reader:sup3rs3cr3t@10.0.0.9:7687?database=neo4j", "10.0.0.9:7687"),
            # 口令里带 '@' 时，主机段在最后一段，前面整体都是凭据
            ("bolt://user:p@ss@graph.internal:7687", "graph.internal:7687"),
            ("bolt://graph.internal", "graph.internal"),
            # 无 scheme 也要能报目标
            ("10.0.0.9:7687", "10.0.0.9:7687"),
            # 端口写坏了只报主机，不把畸形串原样带进响应
            ("bolt://user:pw@graph.internal:notport", "graph.internal"),
        ],
    )
    def test_shapes(self, uri: str, expected: str) -> None:
        assert target_of(uri) == expected
        assert "://" not in target_of(uri)


class TestPlanningBrief:
    """命中切片是链路与 API 共用的唯一映射，单独钉住它的两种形态。"""

    def test_usable_hit_carries_the_case_actions(self) -> None:
        case = builtin_case(0)
        brief = match_of(case, score=2.0).planning_brief()

        assert brief["case_id"] == case.case_id
        assert brief["score"] == 2.0
        assert brief["source"] == "in_memory"
        assert brief["matched_on"] == ["trigger_signals:泥位"]
        assert brief["degraded"] is False
        assert brief["actions"] == case.actions
        assert brief["title"] == case.title

    def test_graph_only_hit_is_evidence_only(self) -> None:
        """图谱侧只回节点摘要（case=None）时不许凭空补处置动作——宁缺毋造。"""
        brief = CaseMatch(case_id="case_graph_only", score=0.9, source="graphiti").planning_brief()

        assert brief["case_id"] == "case_graph_only"
        assert not any(key in brief for key in ("actions", "title", "why_now"))


def build_chain(
    bus: InMemoryBus,
    registry: AgentRegistry,
    contracts: ContractRegistry,
    settings: Settings,
    *,
    knowledge: KnowledgeProvider | None,
) -> HazardResponseChain:
    gateway = AgentGateway(bus, registry, contracts, Tracer(), default_deadline_ms=1_500)
    return HazardResponseChain(gateway=gateway, registry=registry, contracts=contracts, settings=settings, knowledge=knowledge)


class TestChainRecall:
    async def test_recall_arguments_come_from_the_verdict_and_settings(
        self, bus: InMemoryBus, registry: AgentRegistry, contracts: ContractRegistry
    ) -> None:
        cfg = base_settings(knowledge_recall_budget_ms=250.0)
        stub = StubProvider()
        result = await build_chain(bus, registry, contracts, cfg, knowledge=stub).process(readings_rain_burst())

        call = stub.calls[0]
        assert call["hazard_type"] == HazardType.DEBRIS_FLOW.value
        assert call["region_code"] == REGION
        assert call["limit"] == 3
        assert call["budget_ms"] == 250.0
        assert HazardType.DEBRIS_FLOW.cn in call["query"] and REGION in call["query"]
        assert result.reference_cases == [builtin_case(0).case_id]

    async def test_recall_failure_degrades_without_breaking_the_warning(
        self, bus: InMemoryBus, registry: AgentRegistry, contracts: ContractRegistry
    ) -> None:
        stub = StubProvider(error=RuntimeError("图谱连接中断"))
        result = await build_chain(bus, registry, contracts, base_settings(), knowledge=stub).process(readings_rain_burst())

        assert result.ok, "知识层是增强腿，它的故障绝不能把链路判红"
        assert result.verdict is not None and result.warning is not None
        assert result.task_units, "本地剧本仍须产出任务单元"
        assert result.reference_cases == []
        assert any("案例召回降级" in line for line in result.degradations)

    async def test_chain_without_knowledge_keeps_the_pre_wiring_semantics(
        self, bus: InMemoryBus, registry: AgentRegistry, contracts: ContractRegistry
    ) -> None:
        result = await build_chain(bus, registry, contracts, base_settings(), knowledge=None).process(readings_rain_burst())

        assert result.ok
        assert result.reference_cases == []
        assert result.degradations == []

    async def test_graph_only_hit_still_counts_as_referenced_evidence(
        self, bus: InMemoryBus, registry: AgentRegistry, contracts: ContractRegistry
    ) -> None:
        bare = CaseMatch(case_id="case_graph_only", score=0.9, source="graphiti")
        stub = StubProvider([bare, match_of(builtin_case(1))])
        result = await build_chain(bus, registry, contracts, base_settings(), knowledge=stub).process(readings_rain_burst())

        assert result.ok
        assert result.reference_cases == ["case_graph_only", builtin_case(1).case_id]

    async def test_no_trigger_means_no_recall(self, bus: InMemoryBus, registry: AgentRegistry, contracts: ContractRegistry) -> None:
        """未成灾的读数不该触发任何知识检索：预警窗口的毫秒不该花在无事件上。"""
        stub = StubProvider()
        readings = HazardScenarioSimulator(scenario="normal", seed=7).collect_at(utc_now())
        result = await build_chain(bus, registry, contracts, base_settings(), knowledge=stub).process(readings)

        assert result.acted is False
        assert stub.calls == []

    async def test_reference_cases_are_visible_in_the_chain_report(
        self, bus: InMemoryBus, registry: AgentRegistry, contracts: ContractRegistry
    ) -> None:
        """链路 dict 是给 Web 与取证用的：参考案例必须在里面，否则"凭什么这么判"无从回答。"""
        stub = StubProvider([match_of(builtin_case(0))])
        result = await build_chain(bus, registry, contracts, base_settings(), knowledge=stub).process(readings_rain_burst())

        assert result.as_dict()["reference_cases"] == result.reference_cases

    async def test_plan_agent_receives_the_recalled_cases(
        self, bus: InMemoryBus, registry: AgentRegistry, contracts: ContractRegistry
    ) -> None:
        """智能体在场时案例切片要进 PLAN 请求 payload：这是"LLM 不进检索回路"的结构证据——
        案例由平台召回后随请求交出，智能体不需要自己去查图谱或调模型。"""
        stub = StubProvider([match_of(builtin_case(0))])
        gateway = AgentGateway(bus, registry, contracts, Tracer(), default_deadline_ms=1_500)
        await gateway.start()

        captured: list[AgentMessage] = []

        async def record(message: AgentMessage) -> None:
            captured.append(message)

        await bus.subscribe(subjects.agent_in(AgentType.PLAN), record)
        agent = MockAgent(
            transport=bus,
            agent_type=AgentType.PLAN,
            agent_id="plan.wire01",
            capabilities=["task_decompose"],
            latency_ms=5.0,
            seed=7,
        )
        await agent.start(heartbeat_interval=0.05)
        await bus.idle()
        await asyncio.sleep(0.01)
        await bus.idle()

        chain = HazardResponseChain(
            gateway=gateway,
            registry=registry,
            contracts=contracts,
            settings=base_settings(),
            knowledge=stub,
        )
        try:
            result = await chain.process(readings_rain_burst())
        finally:
            await agent.stop()

        requests = [message for message in captured if message.action == Action.PLAN_STU.value]
        assert requests, "规划请求必须经过总线送达 PLAN 智能体"
        payload = requests[0].payload
        assert [item["case_id"] for item in payload["reference_cases"]] == [builtin_case(0).case_id]
        assert payload["reference_cases"][0]["actions"] == builtin_case(0).actions
        assert result.reference_cases == [builtin_case(0).case_id]


class TestContainerWiring:
    async def test_container_reports_knowledge_as_enabled_leg(self) -> None:
        ctn = create_container(base_settings(), with_simulator=False)
        await ctn.start()
        try:
            states = {state.name: state for state in ctn.integration_status()}
            assert states["knowledge"].enabled is True
            assert states["knowledge"].driver == "in_memory"
            assert ctn.knowledge is not None
        finally:
            await ctn.shutdown()

    async def test_real_chain_populates_reference_cases_with_builtin_cases(self) -> None:
        """无替身：容器装配的内存 provider 必须真的被链路调用并留下案例 id。"""
        ctn = create_container(base_settings(), with_simulator=False)
        await ctn.start()
        try:
            result = await ctn.chain.process(readings_rain_burst())
            assert result.ok and result.acted
            known = {case.case_id for case in load_builtin_cases()}
            assert result.reference_cases, "知识层接进了链路就必须留下证据"
            assert set(result.reference_cases) <= known
            assert len(result.reference_cases) <= 3
        finally:
            await ctn.shutdown()


@pytest.fixture
async def knowledge_client(settings: Settings) -> AsyncIterator[tuple[httpx.AsyncClient, PlatformContainer]]:
    ctn = create_container(settings, with_simulator=False)
    await ctn.start()
    app = create_app(ctn.settings, container=ctn)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")
    try:
        yield client, ctn
    finally:
        await client.aclose()
        await ctn.shutdown()


class TestKnowledgeApi:
    async def test_recall_endpoint_returns_the_same_shape_as_the_chain(
        self, knowledge_client: tuple[httpx.AsyncClient, PlatformContainer]
    ) -> None:
        client, ctn = knowledge_client
        response = await client.get(
            "/api/v1/cases/recall",
            params={"q": "强降雨后沟口泥位超阈值", "hazard_type": "debris_flow", "region_code": REGION, "limit": 2},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["count"] == len(body["items"]) == 2
        assert body["degraded_count"] == 0
        assert body["budget_ms"] == ctn.settings.knowledge_recall_budget_ms
        assert {"case_id", "score", "source", "matched_on", "degraded", "actions", "title"} <= set(body["items"][0])

    async def test_recall_is_deterministic(self, knowledge_client: tuple[httpx.AsyncClient, PlatformContainer]) -> None:
        """同一查询两次必须给同一批案例：可解释性证据不能每次刷新都换一套。"""
        client, _ctn = knowledge_client
        params = {"q": "冰湖溃决 下游沟口", "hazard_type": "lake_outburst", "limit": 3}
        first = (await client.get("/api/v1/cases/recall", params=params)).json()
        second = (await client.get("/api/v1/cases/recall", params=params)).json()

        assert first["items"] == second["items"]

    async def test_recall_endpoint_bounds_the_input(self, knowledge_client: tuple[httpx.AsyncClient, PlatformContainer]) -> None:
        client, _ctn = knowledge_client

        too_many = await client.get("/api/v1/cases/recall", params={"q": "泥石流", "limit": 500})
        assert too_many.status_code == 422
        bad_region = await client.get("/api/v1/cases/recall", params={"q": "泥石流", "region_code": "x"})
        assert bad_region.status_code == 422
        bad_hazard = await client.get("/api/v1/cases/recall", params={"q": "泥石流", "hazard_type": "not_a_hazard"})
        assert bad_hazard.status_code == 422

    async def test_empty_query_falls_back_to_hazard_only_browsing(
        self, knowledge_client: tuple[httpx.AsyncClient, PlatformContainer]
    ) -> None:
        """q 允许为空（前端按灾种浏览）：此时按灾种召回，而不是 422 也不是空结果。"""
        client, _ctn = knowledge_client
        response = await client.get("/api/v1/cases/recall", params={"hazard_type": "landslide"})

        assert response.status_code == 200
        assert response.json()["count"] >= 1

    async def test_integrations_endpoint_publishes_the_knowledge_leg(
        self, knowledge_client: tuple[httpx.AsyncClient, PlatformContainer]
    ) -> None:
        client, _ctn = knowledge_client
        body = (await client.get("/api/v1/integrations")).json()

        names = {item["name"] for item in body["items"]}
        assert "knowledge" in names
        assert body["degraded"] == []

    async def test_recall_never_enters_the_llm(self, knowledge_client: tuple[httpx.AsyncClient, PlatformContainer]) -> None:
        """P0 硬约束：读路径结构上不含 LLM——召回一次的时间应是毫秒级，而非模型往返。"""
        client, _ctn = knowledge_client
        started = asyncio.get_running_loop().time()
        response = await client.get("/api/v1/cases/recall", params={"q": "雪崩 公路", "hazard_type": "avalanche", "limit": 5})
        elapsed_ms = (asyncio.get_running_loop().time() - started) * 1000

        assert response.status_code == 200
        assert elapsed_ms < 500, f"读路径耗时异常，疑似走了模型往返: {elapsed_ms:.1f}ms"


def test_chain_result_default_has_no_reference_cases() -> None:
    """`reference_cases` 是可选证据，默认空列表而不是 None：下游可以无脑迭代。"""
    result = ChainResult(trace_id="trc_" + "1" * 16, event_id="evt_" + "1" * 16)

    assert result.reference_cases == []
    assert result.as_dict()["reference_cases"] == []


class SchemaStub:
    """带索引初始化能力的图谱侧替身：预热该不该发生、失败怎么说，都只由它的一句话决定。"""

    driver = "graphiti"

    def __init__(self, *, error: Exception | None = None, delay_ms: float = 0.0) -> None:
        self._error = error
        self._delay_ms = delay_ms
        self.prepared = 0

    async def recall(self, query: str, **_: Any) -> list[CaseMatch]:
        del query
        return []

    async def learn(self, case: HazardCase) -> None:
        del case

    async def learn_case(self, case: HazardCase) -> LearnOutcome:
        return LearnOutcome(case_id=case.case_id, driver=self.driver)

    async def prepare_schema(self) -> None:
        if self._delay_ms:
            await asyncio.sleep(self._delay_ms / 1000)
        if self._error is not None:
            raise self._error
        self.prepared += 1


class TestKnowledgeSchemaWarmup:
    """审计补上的这一段：图谱写入侧与索引初始化在装配层必须有生产调用点。"""

    async def test_纯内存装配不假装预热过图谱(self) -> None:
        provider, _state = build_knowledge(base_settings())
        assert not isinstance(provider, SupportsSchemaPreparation)
        assert await warm_knowledge(provider) is None

    async def test_图谱在位时索引初始化真的被执行(self) -> None:
        stub = SchemaStub()
        provider = FallbackKnowledgeProvider(primary=stub, fallback=InMemoryKnowledgeProvider(cases=[]))
        assert await warm_knowledge(provider) is None
        assert stub.prepared == 1

    async def test_预热失败作为降级事实出现在装配状态上(self) -> None:
        ctn = create_container(base_settings(), with_simulator=False)
        ctn.knowledge = FallbackKnowledgeProvider(
            primary=SchemaStub(error=GraphitiUnavailableError("Neo4j 不可达")),
            fallback=InMemoryKnowledgeProvider(cases=[]),
        )
        await ctn.start()
        try:
            rows = ctn.integration_status()
            row = next(item for item in rows if item.name == "knowledge")
            assert "Neo4j 不可达" in str(row.detail["schema_error"])
            # 光有 detail 不够：/api/v1/integrations 的 degraded 清单要真的把它列出来
            assert "knowledge" in [item.name for item in rows if item.degradation_reason()]
        finally:
            await ctn.shutdown()

    async def test_预热超时不拖住启动(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(integrations, "KNOWLEDGE_SCHEMA_TIMEOUT_SECONDS", 0.01)
        provider = FallbackKnowledgeProvider(primary=SchemaStub(delay_ms=400), fallback=InMemoryKnowledgeProvider(cases=[]))
        reason = await warm_knowledge(provider)
        assert reason is not None and reason.startswith("schema_timeout")
