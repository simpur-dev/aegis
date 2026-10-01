"""检索层接线测试：语料映射、装配降级、链路里的检索块与审计出口。

这一层的验收口径不是"检索准不准"（那需要真权重与标注集），而是三件结构性的事：
1. LLM 不进检索回路——链路里的检索块由平台召回后随请求交出；
2. 权重/连接缺失必须变成**看得见的降级**，而不是让预警报 500 或静默变慢；
3. 取件脚本与装配层对同一目录约定的分歧，必须有一条测试来兜住。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import pytest

from aegis.agents.mock import MockAgent
from aegis.api.app import create_app
from aegis.bus import subjects
from aegis.bus.gateway import AgentGateway, ContractRegistry
from aegis.bus.inmemory import InMemoryBus
from aegis.bus.registry import AgentRegistry
from aegis.config import Settings
from aegis.container import PlatformContainer, create_container
from aegis.domain.enums import Action, AgentType
from aegis.domain.messages import AgentMessage
from aegis.integrations import build_retrieval, warm_retrieval
from aegis.knowledge.cases import HazardCase, load_builtin_cases
from aegis.observability.tracer import Tracer
from aegis.pipeline.chain import ChainResult, HazardResponseChain
from aegis.retrieval.corpus import CASES_SOURCE, doc_from_case, docs_from_cases
from aegis.retrieval.docs import LEG_LEXICAL, Provenance, RetrievedDoc
from aegis.retrieval.embedder import HashingEmbedder
from aegis.retrieval.lexical import build_lexical_index
from aegis.retrieval.onnx_io import EMBEDDER_MODEL_DIR, MODEL_TOKENIZER_FILE, MODEL_WEIGHTS_FILE, RERANKER_MODEL_DIR
from aegis.retrieval.service import Degradation, HybridRetrievalService, RetrievalOutcome, RetrievalQuery
from conftest import REGION, readings_rain_burst

REPO_ROOT = Path(__file__).resolve().parents[3]


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


def lay_weights(root: Path, *dirs: str) -> None:
    """按取件脚本的目录约定放好权重文件名（内容无所谓：from_dir 是惰性的，不做装载）。"""
    for name in dirs:
        target = root / name
        target.mkdir(parents=True, exist_ok=True)
        (target / MODEL_WEIGHTS_FILE).write_text("placeholder", encoding="utf-8")
        (target / MODEL_TOKENIZER_FILE).write_text("{}", encoding="utf-8")


def outcome_of(*docs: RetrievedDoc, degradations: tuple[Degradation, ...] = ()) -> RetrievalOutcome:
    return RetrievalOutcome(
        docs=docs,
        elapsed_ms=12.5,
        budget_ms=800.0,
        leg_timings_ms={LEG_LEXICAL: 11.0},
        degradations=degradations,
    )


def reference_doc(rank: int = 1, *, doc_id: str = "case_debris_rain", text: str = "强降雨后沟口泥位超阈值，先转移再排查") -> RetrievedDoc:
    return RetrievedDoc(
        doc_id=doc_id,
        text=text,
        rank=rank,
        score=0.0328,
        provenance=Provenance(
            legs=(LEG_LEXICAL,),
            leg_scores={LEG_LEXICAL: 7.4},
            fusion_score=0.0328,
            fusion_rank=rank,
        ),
        source=CASES_SOURCE,
    )


class StubRetrieval:
    """检索服务替身：记录查询、按需抛错，永不触碰模型与数据库。"""

    def __init__(self, outcome: RetrievalOutcome | None = None, *, error: Exception | None = None) -> None:
        self._outcome = outcome or outcome_of(reference_doc())
        self._error = error
        self.queries: list[RetrievalQuery] = []

    async def retrieve(self, query: RetrievalQuery) -> RetrievalOutcome:
        self.queries.append(query)
        if self._error is not None:
            raise self._error
        return self._outcome


class TestCorpusMapping:
    def test_doc_identity_is_the_case_id_so_hits_need_no_extra_lookup(self) -> None:
        case = builtin_case(0)
        doc = doc_from_case(case)

        assert doc.doc_id == case.case_id
        assert doc.source == CASES_SOURCE
        assert doc.updated_at == case.valid_at
        assert doc.metadata["hazard_types"] == case.hazard_types
        assert doc.metadata["title"] == case.title

    def test_doc_text_carries_hazard_terms_and_region_prefixes(self) -> None:
        case = builtin_case(0)
        doc = doc_from_case(case)

        for signal in case.trigger_signals[:1]:
            assert signal.lower() in doc.text
        assert case.title.lower() in doc.text
        for prefix in case.region_prefixes:
            assert prefix in doc.text

    def test_structured_filters_stay_unset_because_the_case_model_is_multivalued(self) -> None:
        """hazard/region 留空是有意的：单值等值过滤表达不了多灾种与前缀，压成单值会静默丢案例。"""
        doc = doc_from_case(builtin_case(0))
        assert doc.hazard_type is None and doc.region_code is None
        assert doc.metadata["region_prefixes"]

    def test_duplicate_case_ids_are_collapsed(self) -> None:
        case = builtin_case(0)
        docs = docs_from_cases([case, case, builtin_case(1)])
        assert [doc.doc_id for doc in docs] == [case.case_id, builtin_case(1).case_id]

    def test_lexical_index_covers_the_whole_builtin_library(self) -> None:
        index = build_lexical_index(docs_from_cases(load_builtin_cases()))
        assert len(index) == len(load_builtin_cases())
        assert not index.is_empty


class TestLayoutAgreesWithTheFetchScript:
    """取件脚本与装配层共用一套目录约定：这里比对一次，任何一边改名都会红。"""

    @staticmethod
    def _script() -> Any:
        path = REPO_ROOT / "scripts" / "fetch_retrieval_models.py"
        spec = importlib.util.spec_from_file_location("fetch_retrieval_models", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        # 先登记再执行：脚本里有 @dataclass，dataclasses 要按 __module__ 回查 sys.modules
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)  # 只执行模块级常量，不触发任何下载路径
        return module

    def test_model_subdirectories_match(self) -> None:
        script = self._script()
        assert script.EMBEDDER.dir_name == EMBEDDER_MODEL_DIR
        assert script.RERANKER.dir_name == RERANKER_MODEL_DIR

    def test_weight_file_names_match_the_lazy_loaders(self) -> None:
        assert Path("onnx/model_int8.onnx").name == MODEL_WEIGHTS_FILE
        assert MODEL_TOKENIZER_FILE == "tokenizer.json"

    def test_default_model_dir_points_at_where_the_script_writes(self) -> None:
        script = self._script()
        configured = Path(Settings(env="test").retrieval_model_dir)
        tail = tuple(configured.parts)
        assert tuple(script.DEFAULT_DEST.parts[-len(tail) :]) == tail, (
            f"配置默认 {configured} 与脚本落点 {script.DEFAULT_DEST} 不一致：现场按脚本取件后平台会找不到权重"
        )


class TestBuildRetrieval:
    def test_disabled_by_default_assembly_produces_no_service(self) -> None:
        service, state = build_retrieval(base_settings())
        assert service is None
        assert (state.enabled, state.driver) == (False, "off")

    def test_missing_weights_degrade_visibly_instead_of_failing(self, tmp_path: Path) -> None:
        service, state = build_retrieval(base_settings(retrieval_enabled=True, retrieval_model_dir=str(tmp_path)))

        assert isinstance(service, HybridRetrievalService)
        assert state.enabled is True
        # 降级必须把"用的是哈希嵌入"写在驱动名上，否则准确率口径会被误引
        assert state.driver == HashingEmbedder().model_id
        assert "权重缺失" in str(state.detail["degraded"])
        assert state.detail["rerank_leg"] is False
        assert state.detail["dense_leg"] is False
        assert state.detail["corpus_docs"] == len(load_builtin_cases())

    def test_present_weights_select_the_onnx_models_without_loading_them(self, tmp_path: Path) -> None:
        lay_weights(tmp_path, EMBEDDER_MODEL_DIR, RERANKER_MODEL_DIR)
        service, state = build_retrieval(base_settings(retrieval_enabled=True, retrieval_model_dir=str(tmp_path)))

        assert service is not None
        assert state.driver == "bge-m3-int8"
        assert state.detail["rerank_leg"] is True
        assert state.detail["degraded"] == ""
        # 装载是惰性的：装配阶段不得阻塞事件循环
        assert service.embedder._session is None

    def test_dense_leg_follows_the_store_backend(self, tmp_path: Path) -> None:
        class Pool:
            def acquire(self) -> None:
                return None

        _service, without = build_retrieval(base_settings(retrieval_enabled=True, retrieval_model_dir=str(tmp_path)))
        _service, with_pg = build_retrieval(base_settings(retrieval_enabled=True, retrieval_model_dir=str(tmp_path)), store=Pool())
        assert with_pg.detail["dense_leg"] is True
        assert without.detail["dense_leg"] is False

    def test_budget_reaches_the_service(self, tmp_path: Path) -> None:
        service, _state = build_retrieval(
            base_settings(retrieval_enabled=True, retrieval_model_dir=str(tmp_path), retrieval_budget_ms=321.0)
        )
        assert service is not None
        assert service.default_budget_ms == 321.0

    async def test_lexical_leg_returns_case_documents_without_any_optional_dependency(self, tmp_path: Path) -> None:
        """没权重、没 pgvector 也要能出结果：词法腿就是那根零依赖的腿。"""
        service, _state = build_retrieval(base_settings(retrieval_enabled=True, retrieval_model_dir=str(tmp_path)))
        assert service is not None
        outcome = await service.retrieve(RetrievalQuery(text="强降雨 沟口 泥位 超阈值", k=3))

        assert outcome.docs
        assert outcome.legs_completed == (LEG_LEXICAL,)
        assert {doc.source for doc in outcome.docs} == {CASES_SOURCE}
        # 密集腿是"跳过"而不是失败：这条区分决定了预警不会被装配状态误记成降级
        assert [item.leg for item in outcome.degradations] == ["dense"]
        assert all(item.is_skipped for item in outcome.degradations)


def build_chain(
    bus: InMemoryBus,
    registry: AgentRegistry,
    contracts: ContractRegistry,
    settings: Settings,
    *,
    retrieval: Any | None,
) -> HazardResponseChain:
    gateway = AgentGateway(bus, registry, contracts, Tracer(), default_deadline_ms=1_500)
    return HazardResponseChain(gateway=gateway, registry=registry, contracts=contracts, settings=settings, retrieval=retrieval)


async def drive_with_plan_agent(
    bus: InMemoryBus,
    registry: AgentRegistry,
    contracts: ContractRegistry,
    settings: Settings,
    *,
    retrieval: Any | None,
) -> tuple[ChainResult, list[AgentMessage]]:
    """起一个真 PLAN 智能体，返回链路结果与它经总线收到的请求（检索块必须交出去）。"""
    gateway = AgentGateway(bus, registry, contracts, Tracer(), default_deadline_ms=1_500)
    await gateway.start()
    captured: list[AgentMessage] = []

    async def record(message: AgentMessage) -> None:
        captured.append(message)

    await bus.subscribe(subjects.agent_in(AgentType.PLAN), record)
    agent = MockAgent(
        transport=bus,
        agent_type=AgentType.PLAN,
        agent_id="plan.retr01",
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
        settings=settings,
        retrieval=retrieval,
    )
    try:
        result = await chain.process(readings_rain_burst())
    finally:
        await agent.stop()
    assert result.ok, [result.as_dict()]
    return result, captured


class TestChainRetrieval:
    async def test_query_carries_the_verdict_and_the_configured_budget(
        self, bus: InMemoryBus, registry: AgentRegistry, contracts: ContractRegistry
    ) -> None:
        stub = StubRetrieval()
        settings = base_settings(retrieval_enabled=True, retrieval_budget_ms=456.0)
        await drive_with_plan_agent(bus, registry, contracts, settings, retrieval=stub)

        query = stub.queries[0]
        assert query.text
        assert REGION in query.text
        assert query.k == 5
        assert query.budget_ms == 456.0
        assert query.trace_id

    async def test_retrieved_blocks_reach_the_plan_payload_with_provenance(
        self, bus: InMemoryBus, registry: AgentRegistry, contracts: ContractRegistry
    ) -> None:
        stub = StubRetrieval(outcome_of(reference_doc(rank=1), reference_doc(rank=2, doc_id="case_evac")))
        result, captured = await drive_with_plan_agent(bus, registry, contracts, base_settings(), retrieval=stub)

        request = next(message for message in captured if message.action == Action.PLAN_STU.value)
        blocks = request.payload["retrieved_context"]
        assert [block["doc_id"] for block in blocks] == ["case_debris_rain", "case_evac"]
        assert blocks[0]["legs"] == [LEG_LEXICAL]
        assert blocks[0]["leg_scores"] == {LEG_LEXICAL: 7.4}
        assert result.context_docs == ["case_debris_rain", "case_evac"]
        assert result.as_dict()["context_docs"] == result.context_docs
        assert result.degradations == []

    async def test_a_runtime_failure_on_a_leg_degrades_without_breaking_the_warning(
        self, bus: InMemoryBus, registry: AgentRegistry, contracts: ContractRegistry
    ) -> None:
        failure = Degradation(leg="rerank", reason="交叉编码超时", outcome="timeout")
        stub = StubRetrieval(outcome_of(degradations=(failure,)))
        result, _captured = await drive_with_plan_agent(bus, registry, contracts, base_settings(), retrieval=stub)

        assert result.ok
        assert result.warning is not None
        assert any("检索腿降级: rerank" in line for line in result.degradations)

    async def test_skipped_legs_are_never_written_into_the_chain_ledger(
        self, bus: InMemoryBus, registry: AgentRegistry, contracts: ContractRegistry
    ) -> None:
        """没有 pgvector 是常态：把"跳过"写进每条预警的降级列表会淹没真故障。"""
        skipped = Degradation(leg="dense", reason="未注入连接（conn/acquire），密集腿跳过", outcome="skipped")
        stub = StubRetrieval(outcome_of(reference_doc(), degradations=(skipped,)))
        result, _captured = await drive_with_plan_agent(bus, registry, contracts, base_settings(), retrieval=stub)

        assert result.degradations == []
        assert result.context_docs == ["case_debris_rain"]

    async def test_a_broken_retriever_never_breaks_the_warning(
        self, bus: InMemoryBus, registry: AgentRegistry, contracts: ContractRegistry
    ) -> None:
        stub = StubRetrieval(error=RuntimeError("权重文件损坏"))
        result, _captured = await drive_with_plan_agent(bus, registry, contracts, base_settings(), retrieval=stub)

        assert result.warning is not None and result.task_units
        assert result.context_docs == []
        assert any("检索上下文降级" in line for line in result.degradations)

    async def test_no_retrieval_service_keeps_the_pre_wiring_semantics(
        self, bus: InMemoryBus, registry: AgentRegistry, contracts: ContractRegistry
    ) -> None:
        result, _captured = await drive_with_plan_agent(bus, registry, contracts, base_settings(), retrieval=None)

        assert result.context_docs == []
        assert result.degradations == []

    async def test_local_playbook_path_does_not_pay_for_blocks_nobody_reads(
        self, bus: InMemoryBus, registry: AgentRegistry, contracts: ContractRegistry
    ) -> None:
        """PLAN 智能体不在场时不做检索：本地剧本不消费上下文块，为它付一次 CPU 推理不值。"""
        stub = StubRetrieval()
        settings = base_settings()
        gateway = AgentGateway(bus, registry, contracts, Tracer(), default_deadline_ms=1_500)
        chain = HazardResponseChain(gateway=gateway, registry=registry, contracts=contracts, settings=settings, retrieval=stub)

        result = await chain.process(readings_rain_burst())

        assert result.ok and result.acted
        assert stub.queries == []


class TestRealServiceThroughTheChain:
    async def test_lexical_blocks_land_in_the_plan_request_without_mocks(
        self, bus: InMemoryBus, registry: AgentRegistry, contracts: ContractRegistry, tmp_path: Path
    ) -> None:
        """无替身：容器装配的真检索服务必须真的被链路调用并留下 doc id。"""
        ctn = create_container(base_settings(retrieval_enabled=True, retrieval_model_dir=str(tmp_path)), with_simulator=False)
        await ctn.start(with_mock_agents=True)
        try:
            results = await ctn.chain.process_many({REGION: readings_rain_burst()})
            acted = [r for r in results if r.acted]
            assert acted
            for result in acted:
                assert result.context_docs, "检索接进了链路就必须留下证据"
                assert len(result.context_docs) <= 5
                assert set(result.context_docs) <= {case.case_id for case in load_builtin_cases()}
        finally:
            await ctn.shutdown()


@pytest.fixture
async def retrieval_client(tmp_path: Path) -> Any:
    cfg = base_settings(retrieval_enabled=True, retrieval_model_dir=str(tmp_path))
    ctn = create_container(cfg, with_simulator=False)
    await ctn.start()
    app = create_app(ctn.settings, container=ctn)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")
    try:
        yield client, ctn
    finally:
        await client.aclose()
        await ctn.shutdown()


class _WarmEmbedder:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.model_id = "bge-m3-int8"
        self.dim = 1024
        self.batches: list[list[str]] = []
        self._error = error

    def embed(self, texts: Any) -> list[list[float]]:
        self.batches.append(list(texts))
        if self._error is not None:
            raise self._error
        return [[0.0] * self.dim for _ in texts]


class _WarmReranker:
    def __init__(self, *, enabled: bool = True, error: Exception | None = None) -> None:
        self.name = "bge-reranker-v2-m3-int8"
        self.enabled = enabled
        self.calls = 0
        self._error = error

    def rerank(self, query: str, candidates: Any) -> list[Any]:
        self.calls += 1
        if self._error is not None:
            raise self._error
        return []


class _WarmService:
    def __init__(self, embedder: _WarmEmbedder, reranker: _WarmReranker) -> None:
        self.embedder = embedder
        self.reranker = reranker


class TestPrewarm:
    async def test_no_service_is_a_no_op(self) -> None:
        assert await warm_retrieval(None) is None

    async def test_both_sessions_load_before_the_first_request(self) -> None:
        embedder, reranker = _WarmEmbedder(), _WarmReranker()
        reason = await warm_retrieval(_WarmService(embedder, reranker))  # type: ignore[arg-type]

        assert reason is None
        assert embedder.batches and reranker.calls == 1

    async def test_a_disabled_reranker_is_not_warmed(self) -> None:
        embedder, reranker = _WarmEmbedder(), _WarmReranker(enabled=False)
        await warm_retrieval(_WarmService(embedder, reranker))  # type: ignore[arg-type]
        assert reranker.calls == 0

    async def test_broken_weights_report_instead_of_raising(self) -> None:
        """启动期装载失败不得阻止平台起来：原因要能被状态接口看见。"""
        embedder = _WarmEmbedder(error=RuntimeError("权重文件损坏"))
        service = _WarmService(embedder, _WarmReranker())
        reason = await warm_retrieval(service)  # type: ignore[arg-type]

        assert reason == "RuntimeError"

    async def test_warm_error_surfaces_through_the_integration_state(self, tmp_path: Path) -> None:
        ctn = create_container(base_settings(retrieval_enabled=True, retrieval_model_dir=str(tmp_path)), with_simulator=False)
        ctn._retrieval_warm_error = "ModelUnavailableError"
        state = {item.name: item for item in ctn.integration_status()}["retrieval"]
        assert state.detail["warm_error"] == "ModelUnavailableError"

    def test_default_budget_covers_a_measured_rerank_round(self) -> None:
        """本机实测（真实案例正文 ~305 字）：重排 5 对中位 4201ms。

        预算低于它就是让重排腿每次必然超时——装了等于没装。改这个数字要连带看
        `_RERANK_TOP_N`，两者是一组配套测量。
        """
        cfg = Settings(env="test")
        assert cfg.retrieval_budget_ms >= 4_500
        assert cfg.retrieval_budget_ms < 180_000, "检索预算必须仍是预警口径的一小部分"


class TestRetrievalApi:
    async def test_search_returns_ranked_blocks_with_auditable_provenance(
        self, retrieval_client: tuple[httpx.AsyncClient, PlatformContainer]
    ) -> None:
        client, ctn = retrieval_client
        response = await client.get("/api/v1/retrieval/search", params={"q": "冰湖溃决 下游 转移", "k": 3})

        assert response.status_code == 200
        body = response.json()
        assert body["count"] == len(body["items"]) <= 3
        assert body["budget_ms"] == ctn.settings.retrieval_budget_ms
        assert body["legs_completed"] == [LEG_LEXICAL]
        first = body["items"][0]
        assert {"doc_id", "rank", "score", "source", "text", "legs", "leg_scores", "degraded_legs"} <= set(first)
        assert first["rank"] == 1

    async def test_search_is_deterministic(self, retrieval_client: tuple[httpx.AsyncClient, PlatformContainer]) -> None:
        client, _ctn = retrieval_client
        params = {"q": "强降雨 泥位", "k": 4}
        first = (await client.get("/api/v1/retrieval/search", params=params)).json()
        second = (await client.get("/api/v1/retrieval/search", params=params)).json()

        assert [item["doc_id"] for item in first["items"]] == [item["doc_id"] for item in second["items"]]

    async def test_empty_query_is_an_empty_result_not_an_error(self, retrieval_client: tuple[httpx.AsyncClient, PlatformContainer]) -> None:
        """q 允许只给空白（上游没拿到正文）：服务口径是空结果零耗时，不是 422 也不是降级。"""
        client, _ctn = retrieval_client
        response = await client.get("/api/v1/retrieval/search", params={"q": "   "})

        assert response.status_code == 200
        body = response.json()
        assert body["count"] == 0
        assert body["degradations"] == []

    async def test_rerank_flag_is_accepted_as_a_boolean(self, retrieval_client: tuple[httpx.AsyncClient, PlatformContainer]) -> None:
        """rerank 开关要能被外部关掉（预算紧的现场）：布尔查询串必须被正确解析而不是当成字符串。"""
        client, _ctn = retrieval_client
        on = await client.get("/api/v1/retrieval/search", params={"q": "雪崩 公路", "k": 2, "rerank": "true"})
        off = await client.get("/api/v1/retrieval/search", params={"q": "雪崩 公路", "k": 2, "rerank": "false"})

        assert on.status_code == off.status_code == 200
        assert [item["doc_id"] for item in on.json()["items"]] == [item["doc_id"] for item in off.json()["items"]]

    async def test_input_bounds_are_declared(self, retrieval_client: tuple[httpx.AsyncClient, PlatformContainer]) -> None:
        client, _ctn = retrieval_client
        assert (await client.get("/api/v1/retrieval/search", params={"q": "x", "k": 999})).status_code == 422
        assert (await client.get("/api/v1/retrieval/search", params={"q": ""})).status_code == 422

    async def test_disabled_leg_answers_503_not_an_empty_200(self) -> None:
        ctn = create_container(base_settings(), with_simulator=False)
        await ctn.start()
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=create_app(ctn.settings, container=ctn)), base_url="http://testserver"
            ) as client:
                response = await client.get("/api/v1/retrieval/search", params={"q": "泥石流"})

            states = {state.name: state for state in ctn.integration_status()}
            assert response.status_code == 503
            assert states["retrieval"].enabled is False
        finally:
            await ctn.shutdown()

    async def test_search_never_enters_the_llm(self, retrieval_client: tuple[httpx.AsyncClient, PlatformContainer]) -> None:
        """P0 硬约束的量测版：检索一次的时间是毫秒级，模型往返会立刻把它顶穿预算。"""
        client, ctn = retrieval_client
        started = asyncio.get_running_loop().time()
        response = await client.get(f"/api/v1/retrieval/search?q={quote('危岩崩塌 公路')}&k=5")
        elapsed_ms = (asyncio.get_running_loop().time() - started) * 1000

        assert response.status_code == 200
        assert elapsed_ms < ctn.settings.retrieval_budget_ms * 4, f"检索耗时异常，疑似走了模型往返: {elapsed_ms:.1f}ms"
