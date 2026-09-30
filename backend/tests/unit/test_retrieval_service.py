"""混合检索服务的契约：真 SQL 路径、并发双腿、预算到点交卷、降级不抛异常、凭证可审计。

密集腿走的是 `persistence.vectors.search_top_k` 的真代码（SQL 由持久层构造），
连接用假对象替身；嵌入腿用 HashingEmbedder（1024 维、无外部依赖）。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import pytest

from aegis.observability.tracer import Tracer
from aegis.persistence.errors import VectorArgumentError, VectorEmbeddingError
from aegis.retrieval.docs import KnowledgeDoc, RetrievedDoc
from aegis.retrieval.embedder import Embedder, HashingEmbedder
from aegis.retrieval.lexical import build_lexical_index
from aegis.retrieval.onnx_io import RetrievalArgumentError
from aegis.retrieval.reranker import NoopReranker, RerankedHit, Reranker
from aegis.retrieval.service import HybridRetrievalService, RetrievalQuery, render_context

REGION = "540121"


def doc(doc_id: str, text: str, *, hazard: str | None = "debris_flow", region: str | None = REGION) -> KnowledgeDoc:
    return KnowledgeDoc(doc_id=doc_id, text=text, hazard_type=hazard, region_code=region, source="warning_history")


LEXICON_DOCS = [
    doc("stu_1", "林周县短时强降水触发泥石流，疏散河道两岸群众"),
    doc("stu_2", "当雄县坡体裂缝扩张，禁止返回危险区", hazard="landslide", region="540122"),
    doc("stu_3", "冰湖水位异常，下游两岸转移", hazard="lake_outburst"),
]


class FakeConn:
    """最小 asyncpg 面：记录调用、返回预置行、可注入异常或阻塞。"""

    def __init__(
        self,
        *,
        rows: list[Mapping[str, Any]] | None = None,
        error: Exception | None = None,
        delay_ms: float = 0.0,
    ) -> None:
        self.rows = rows if rows is not None else []
        self.error = error
        self.delay_ms = delay_ms
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetch(self, sql: str, *args: Any) -> list[Mapping[str, Any]]:
        self.calls.append((sql, args))
        if self.delay_ms:
            await asyncio.sleep(self.delay_ms / 1000.0)
        if self.error is not None:
            raise self.error
        return list(self.rows)

    @property
    def sql(self) -> str:
        assert self.calls, "密集腿没有走到 SQL"
        return self.calls[0][0]

    @property
    def args(self) -> tuple[Any, ...]:
        assert self.calls, "密集腿没有走到 SQL"
        return self.calls[0][1]


class FakeAcquire:
    """池借还面的替身：证明 acquire 路径也走通（生产用 asyncpg pool.acquire()）。"""

    def __init__(self, conn: FakeConn) -> None:
        self.conn = conn
        self.entered = 0
        self.exited = 0

    def __call__(self) -> FakeAcquire:
        return self

    async def __aenter__(self) -> FakeConn:
        self.entered += 1
        return self.conn

    async def __aexit__(self, *exc: Any) -> None:
        self.exited += 1


def hit(
    doc_id: str, score: float, *, objective: str = "过程雨量达阈值，需转移", hazard: str = "debris_flow", region: str = REGION
) -> Mapping[str, Any]:
    return {
        "task_unit_id": doc_id,
        "hazard_type": hazard,
        "region_code": region,
        "event_id": "evt_1",
        "objective": objective,
        "created_at": datetime(2026, 9, 29, 3, 4, 5, tzinfo=UTC),
        "score": score,
    }


def build_service(
    *,
    conn: FakeConn | None = None,
    acquire: Any | None = None,
    lexicon_docs: list[KnowledgeDoc] | None = LEXICON_DOCS,
    embedder: Embedder | None = None,
    reranker: Reranker | None = None,
    tracer: Tracer | None = None,
    **kwargs: Any,
) -> HybridRetrievalService:
    return HybridRetrievalService(
        embedder=embedder or HashingEmbedder(),
        lexicon=build_lexical_index(lexicon_docs or []),
        conn=conn,
        acquire=acquire,
        reranker=reranker,
        tracer=tracer,
        **kwargs,
    )


class ReversingReranker:
    """把候选顺序整体反转，便于断言"重排确实改了序"。"""

    name = "fake-cross-encoder"
    enabled = True

    def __init__(self) -> None:
        self.seen: list[tuple[str, str]] = []

    def rerank(self, query: str, candidates: Any) -> list[RerankedHit]:
        self.seen = list(candidates)
        return [RerankedHit(doc_id=doc_id, score=1.0 - 0.1 * position) for position, (doc_id, _) in enumerate(reversed(list(candidates)))]


class ExplodingReranker:
    name = "broken"
    enabled = True

    def rerank(self, query: str, candidates: Any) -> list[RerankedHit]:
        raise RuntimeError("权重段错误")


class StubEmbedder:
    """说谎的嵌入器：声明 1024，实际给短向量——用来验证持久层挡住了它。"""

    model_id = "stub"

    def __init__(self, *, declares: int, returns: int) -> None:
        self.dim = declares
        self._returns = returns

    def embed(self, texts: Any) -> list[list[float]]:
        return [[0.1] * self._returns for _ in texts]


class ScriptedClock:
    """预置读数序列（超出后停在末值）：把"预算耗尽"变成可判定条件，而不是靠墙钟抖动等。

    密集腿里的 asyncio.to_thread 有真实线程交接开销，用等步长假时钟会让超时窗口随负载漂；
    这里让腿内读数恒定、只在重排检查那一刻跳到超出 deadline 的位置。
    """

    def __init__(self, values: list[float]) -> None:
        self.values = values
        self.reads: list[float] = []

    def __call__(self) -> float:
        value = self.values.pop(0) if self.values else self.reads[-1]
        self.reads.append(value)
        return value


def ids(outcome_docs: tuple[RetrievedDoc, ...] | list[RetrievedDoc]) -> list[str]:
    return [item.doc_id for item in outcome_docs]


class TestDenseLegUsesRealSqlPath:
    async def test_sql_and_args_come_from_persistence(self) -> None:
        conn = FakeConn(rows=[hit("stu_1", 0.82)])
        service = build_service(conn=conn)
        outcome = await service.retrieve(RetrievalQuery(text="泥石流 疏散", k=8, hazard_type="debris_flow", region_code=REGION))

        sql, args = conn.calls[0]
        assert "ORDER BY embedding <=> $1::vector" in sql
        assert "hazard_type = $2" in sql and "region_code = $3" in sql
        assert len(args[0]) == 1024 and all(isinstance(value, float) for value in args[0])
        assert args[1] == "debris_flow" and args[2] == REGION
        assert args[-1] == 24, "每腿候选 = k * 3"
        assert "stu_1" in ids(outcome.docs)

    async def test_pool_acquire_is_entered_and_released(self) -> None:
        gate = FakeAcquire(FakeConn(rows=[hit("stu_2", 0.7, hazard="landslide", region="540122")]))
        service = build_service(acquire=gate)
        await service.retrieve(RetrievalQuery(text="坡体 裂缝", k=4))
        assert (gate.entered, gate.exited) == (1, 1)

    async def test_dense_only_result_carries_dense_provenance(self) -> None:
        conn = FakeConn(rows=[hit("stu_x", 0.9)])
        service = build_service(conn=conn, lexicon_docs=[])
        outcome = await service.retrieve(RetrievalQuery(text="泥石流"))
        top = outcome.docs[0]
        assert top.provenance.legs == ("dense",)
        assert top.provenance.leg_scores == {"dense": 0.9}
        assert top.text == "过程雨量达阈值，需转移"
        assert top.hazard_type == "debris_flow"
        assert top.updated_at == datetime(2026, 9, 29, 3, 4, 5, tzinfo=UTC)
        assert {item.leg for item in outcome.degradations} == {"lexical"}

    async def test_filters_reach_the_lexical_leg_too(self) -> None:
        conn = FakeConn(rows=[])
        service = build_service(conn=conn)
        outcome = await service.retrieve(RetrievalQuery(text="坡体 裂缝", hazard_type="landslide", region_code="540122"))
        assert ids(outcome.docs) == ["stu_2"], "同一批结构化过滤必须同时约束两条腿"


class TestHybridFusion:
    async def test_docs_hit_by_both_legs_are_marked_and_promoted(self) -> None:
        conn = FakeConn(rows=[hit("stu_1", 0.9), hit("stu_9", 0.8)])
        service = build_service(conn=conn)
        outcome = await service.retrieve(RetrievalQuery(text="林周县 泥石流 疏散", k=5))

        shared = next(item for item in outcome.docs if item.doc_id == "stu_1")
        assert shared.provenance.legs == ("dense", "lexical")
        assert shared.provenance.leg_scores["lexical"] > 0
        assert shared.text == LEXICON_DOCS[0].text, "重叠文档取索引里的全文，而不是 SQL 摘要"
        assert shared.rank == 1

    async def test_scores_are_reciprocal_rank_fusion(self) -> None:
        conn = FakeConn(rows=[hit("stu_1", 0.9)])
        service = build_service(conn=conn)
        outcome = await service.retrieve(RetrievalQuery(text="林周县 泥石流", k=5))
        top = outcome.docs[0]
        assert top.score == pytest.approx(1 / 61 + 1 / 61)
        assert top.provenance.fusion_rank == 1

    async def test_lexical_only_doc_still_returns(self) -> None:
        conn = FakeConn(rows=[hit("stu_1", 0.9)])
        service = build_service(conn=conn)
        outcome = await service.retrieve(RetrievalQuery(text="冰湖 水位 转移", k=5))
        assert "stu_3" in ids(outcome.docs)

    async def test_k_truncates_final_result(self) -> None:
        conn = FakeConn(rows=[hit(f"stu_{i}", 0.9 - i / 100) for i in range(1, 4)])
        service = build_service(conn=conn)
        outcome = await service.retrieve(RetrievalQuery(text="泥石流 疏散 坡体 裂缝 冰湖", k=2, rerank=False))
        assert len(outcome.docs) == 2


class TestRerank:
    async def test_enabled_reranker_reorders_and_records_scores(self) -> None:
        rows = [hit("stu_1", 0.9), hit("stu_2", 0.7, hazard="landslide", region="540122")]
        baseline = await build_service(conn=FakeConn(rows=list(rows))).retrieve(
            RetrievalQuery(text="林周县 泥石流 坡体 裂缝", k=5, rerank=False)
        )
        reranker = ReversingReranker()
        service = build_service(conn=FakeConn(rows=list(rows)), reranker=reranker)
        outcome = await service.retrieve(RetrievalQuery(text="林周县 泥石流 坡体 裂缝", k=5))

        assert reranker.seen, "重排器应收到融合后的头部候选 (doc_id, text)"
        assert ids(outcome.docs) == list(reversed(ids(baseline.docs))), "反序重排器必须真的改了序"
        assert outcome.docs[0].provenance.rerank_score == pytest.approx(1.0)
        assert "rerank" in outcome.docs[0].provenance.legs
        assert [item.rank for item in outcome.docs] == list(range(1, len(outcome.docs) + 1))

    async def test_noop_reranker_keeps_rrf_order_and_claims_nothing(self) -> None:
        conn = FakeConn(rows=[hit("stu_1", 0.9)])
        service = build_service(conn=conn, reranker=NoopReranker())
        outcome = await service.retrieve(RetrievalQuery(text="林周县 泥石流", k=5))
        assert all(item.provenance.rerank_score is None for item in outcome.docs)
        assert "rerank" not in outcome.leg_timings_ms

    async def test_reranker_failure_falls_back_to_rrf_order(self) -> None:
        rows = [hit("stu_1", 0.9), hit("stu_2", 0.7, hazard="landslide", region="540122")]
        baseline = await build_service(conn=FakeConn(rows=list(rows))).retrieve(RetrievalQuery(text="林周县 泥石流 坡体 裂缝", k=5))
        outcome = await build_service(conn=FakeConn(rows=list(rows)), reranker=ExplodingReranker()).retrieve(
            RetrievalQuery(text="林周县 泥石流 坡体 裂缝", k=5)
        )

        assert ids(outcome.docs) == ids(baseline.docs), "重排失败必须原样保留 RRF 次序"
        assert {item.leg for item in outcome.degradations} == {"rerank"}
        assert all(item.provenance.rerank_score is None for item in outcome.docs)

    async def test_rerank_is_skipped_when_budget_is_spent(self) -> None:
        conn = FakeConn(rows=[hit("stu_1", 0.9)])
        # 读数序列：起点 / 密集腿进·出 / 词法腿进·出 / 重排检查（越过 deadline）
        clock = ScriptedClock([0.0, 0.0, 0.0, 0.0, 0.0, 0.5])
        service = build_service(conn=conn, reranker=ReversingReranker(), clock=clock, default_budget_ms=100.0)
        outcome = await service.retrieve(RetrievalQuery(text="林周县 泥石流", k=5))

        skip = next(item for item in outcome.degradations if item.leg == "rerank")
        assert skip.outcome == "skipped"
        assert "跳过重排" in skip.reason
        assert outcome.docs, "跳过重排不影响交出融合结果"
        assert all(item.provenance.rerank_score is None for item in outcome.docs)
        assert "rerank" not in outcome.leg_timings_ms


class TestBudgetAndDegradation:
    async def test_slow_dense_leg_yields_partial_result_within_budget(self) -> None:
        conn = FakeConn(rows=[hit("stu_1", 0.95)], delay_ms=400)
        service = build_service(conn=conn, default_budget_ms=40.0)
        started = time.perf_counter()
        outcome = await service.retrieve(RetrievalQuery(text="冰湖 水位 转移", k=5))
        wall_ms = (time.perf_counter() - started) * 1000.0

        assert wall_ms < 300.0, "预算到点就交卷，不等慢腿"
        assert outcome.degraded
        dense = next(item for item in outcome.degradations if item.leg == "dense")
        assert dense.outcome == "timeout"
        assert "stu_3" in ids(outcome.docs), "词法腿的结果照常交出"
        assert outcome.legs_completed == ("lexical",)
        assert outcome.elapsed_ms <= 300.0

    async def test_dense_runtime_failure_degrades_instead_of_raising(self) -> None:
        conn = FakeConn(error=RuntimeError("connection terminated unexpectedly"))
        service = build_service(conn=conn)
        outcome = await service.retrieve(RetrievalQuery(text="坡体 裂缝", hazard_type="landslide", region_code="540122"))
        assert outcome.degraded
        assert ids(outcome.docs) == ["stu_2"]
        assert "connection terminated" in outcome.degradations[0].reason

    async def test_missing_connection_and_empty_lexicon_still_answer(self) -> None:
        service = build_service(conn=None, lexicon_docs=[])
        outcome = await service.retrieve(RetrievalQuery(text="泥石流"))
        assert outcome.docs == ()
        assert {item.leg for item in outcome.degradations} == {"dense", "lexical"}
        assert all(item.outcome == "skipped" for item in outcome.degradations)

    async def test_empty_query_is_not_a_degradation(self) -> None:
        service = build_service(conn=FakeConn(rows=[hit("stu_1", 0.9)]))
        outcome = await service.retrieve(RetrievalQuery(text="   "))
        assert outcome.docs == ()
        assert not outcome.degraded
        assert outcome.leg_timings_ms == {}

    async def test_non_positive_budget_is_a_caller_error(self) -> None:
        service = build_service(conn=FakeConn())
        with pytest.raises(RetrievalArgumentError, match="budget_ms"):
            await service.retrieve(RetrievalQuery(text="泥石流", budget_ms=0))

    async def test_non_positive_k_is_a_caller_error(self) -> None:
        service = build_service(conn=FakeConn())
        with pytest.raises(RetrievalArgumentError, match="k 必须为正"):
            await service.retrieve(RetrievalQuery(text="泥石流", k=0))

    async def test_naive_time_window_is_a_caller_error_not_a_degradation(self) -> None:
        service = build_service(conn=FakeConn())
        with pytest.raises(VectorArgumentError, match="时区"):
            await service.retrieve(RetrievalQuery(text="泥石流", since=datetime(2026, 9, 1)))

    async def test_score_threshold_is_forwarded_verbatim(self) -> None:
        conn = FakeConn(rows=[hit("stu_1", 0.9)])
        service = build_service(conn=conn)
        await service.retrieve(RetrievalQuery(text="泥石流", score_threshold=0.35))
        assert 0.35 in conn.args


class TestEmbeddingDimensionContract:
    async def test_wrong_dim_surfaces_persistence_error_before_the_round_trip(self) -> None:
        """bge-m3/persistence 的口径是 vector(1024)。

        嵌入器少给一维时，必须先被 `persistence` 的 validate_embedding 挡住并抛
        VectorEmbeddingError，而不是把坏向量交给 asyncpg 去报原生驱动错误。
        """
        conn = FakeConn(rows=[hit("stu_1", 0.9)])
        service = build_service(conn=conn, embedder=StubEmbedder(declares=8, returns=8))
        with pytest.raises(VectorEmbeddingError) as excinfo:
            await service.retrieve(RetrievalQuery(text="泥石流"))
        assert excinfo.value.detail == {"actual": 8}
        assert conn.calls == [], "校验发生在 SQL 下发之前"

    async def test_declared_dim_mismatch_is_caught_in_the_embedding_leg(self) -> None:
        service = build_service(conn=FakeConn(), embedder=StubEmbedder(declares=1024, returns=8))
        outcome = await service.retrieve(RetrievalQuery(text="泥石流"))
        assert outcome.degraded
        assert "维度" in next(item for item in outcome.degradations if item.leg == "dense").reason

    async def test_hashing_embedder_is_bound_to_the_same_constant(self) -> None:
        assert HashingEmbedder().dim == 1024


class TestMetricsAndContext:
    async def test_every_leg_is_timed_and_total_is_attributed(self) -> None:
        tracer = Tracer()
        conn = FakeConn(rows=[hit("stu_1", 0.9)])
        service = build_service(conn=conn, tracer=tracer)
        await service.retrieve(RetrievalQuery(text="林周县 泥石流", k=5, trace_id="trc_" + "a" * 16))

        names = set(tracer.ledger.names())
        assert {"retrieval_dense_ms", "retrieval_lexical_ms", "retrieval_total_ms"} <= names
        samples = tracer.ledger.by_trace("trc_" + "a" * 16)
        assert {sample.name for sample in samples} == {"retrieval_dense_ms", "retrieval_lexical_ms", "retrieval_total_ms"}

    async def test_timeout_is_visible_in_the_ledger_outcome(self) -> None:
        tracer = Tracer()
        conn = FakeConn(rows=[hit("stu_1", 0.9)], delay_ms=300)
        service = build_service(conn=conn, tracer=tracer, default_budget_ms=30.0)
        await service.retrieve(RetrievalQuery(text="冰湖 水位", k=5))
        assert tracer.ledger.stats("retrieval_total_ms", outcome="timeout").count == 1

    async def test_rerank_metric_only_appears_when_it_ran(self) -> None:
        tracer = Tracer()
        conn = FakeConn(rows=[hit("stu_1", 0.9)])
        service = build_service(conn=conn, tracer=tracer, reranker=ReversingReranker())
        await service.retrieve(RetrievalQuery(text="林周县 泥石流 坡体", k=5))
        assert "retrieval_rerank_ms" in tracer.ledger.names()

    async def test_outcome_dict_is_report_ready(self) -> None:
        conn = FakeConn(rows=[hit("stu_1", 0.9)])
        service = build_service(conn=conn, default_budget_ms=40.0)
        outcome = await service.retrieve(RetrievalQuery(text="林周县 泥石流", k=5))
        payload = outcome.as_dict()
        assert payload["count"] == len(outcome.docs)
        assert payload["degraded"] is False
        assert payload["legs_completed"] == ["dense", "lexical"]
        assert payload["budget_ms"] == 40.0

    async def test_context_block_carries_rank_and_provenance(self) -> None:
        conn = FakeConn(rows=[hit("stu_1", 0.9)])
        service = build_service(conn=conn)
        outcome = await service.retrieve(RetrievalQuery(text="林周县 泥石流", k=5))
        block = outcome.context_block(max_docs=3, per_doc_chars=12)
        assert block.splitlines()[0].startswith("[1] (dense+lexical")
        assert render_context([]) == ""

    async def test_retrieval_never_leaks_documents_without_provenance(self) -> None:
        conn = FakeConn(rows=[hit("stu_1", 0.9)])
        service = build_service(conn=conn)
        outcome = await service.retrieve(RetrievalQuery(text="林周县 泥石流 冰湖", k=8))
        for item in outcome.docs:
            assert item.provenance.legs
            assert item.provenance.fusion_score > 0
            assert item.score == item.provenance.fusion_score or item.provenance.rerank_score is not None


class TestConcurrency:
    async def test_legs_run_concurrently_not_serially(self) -> None:
        """两腿各 120ms：串行下界 240ms，并发应明显低于它。

        这是"预算守得住"的结构性证据——把两条腿排起来跑，等于白送一个 SLA 违约。
        """
        conn = FakeConn(rows=[hit("stu_1", 0.9)], delay_ms=120)
        service = build_service(conn=conn, default_budget_ms=5_000.0)

        started = time.perf_counter()
        outcome = await service.retrieve(RetrievalQuery(text="林周县 泥石流", k=5))
        wall_ms = (time.perf_counter() - started) * 1000.0

        assert outcome.degradations == ()
        assert 100.0 <= wall_ms < 230.0, f"实测 {wall_ms:.1f}ms：两条腿没有并发"
