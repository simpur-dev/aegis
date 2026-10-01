"""seekdb 检索适配层的离线单测：用替身 DBAPI 钉住 SQL 形状、写入校验与失败翻译。

这里刻意不连真引擎（在线用例在 `tests/integration/test_retrieval_seekdb_live.py`），
但每条断言的口径都来自本机真 seekdb 1.3.0 上量到的行为：
默认全文解析器是 `space`（中文整句一个 token，召回静默全 0）、ngram(2) 才有效、
向量索引要 `WITH (distance=cosine, type=hnsw)`、`cosine_distance()` 配 `ORDER BY ... APPROXIMATE LIMIT`。
"""

from __future__ import annotations

from typing import Any

import pytest

from aegis.retrieval.docs import KnowledgeDoc
from aegis.retrieval.embedder import HashingEmbedder
from aegis.retrieval.lexical import build_lexical_index
from aegis.retrieval.onnx_io import RetrievalArgumentError
from aegis.retrieval.port import IndexFilter
from aegis.retrieval.seekdb import (
    SeekdbConfig,
    SeekdbIndexError,
    SeekdbKnowledgeIndex,
    _vector_literal,
)
from aegis.retrieval.service import HybridRetrievalService, RetrievalQuery


class FakeCursor:
    def __init__(self, connection: FakeConnection) -> None:
        self._connection = connection

    def execute(self, sql: str, params: Any = None) -> None:
        self._connection.calls.append((sql, params))
        if self._connection.fail_with is not None:
            raise self._connection.fail_with

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._connection.results.pop(0)) if self._connection.results else []

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *_exc: Any) -> bool:
        return False


class FakeConnection:
    def __init__(self, *, results: list[list[tuple[Any, ...]]] | None = None, fail_with: Exception | None = None) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.results = results or []
        self.fail_with = fail_with
        self.committed = 0
        self.closed = 0

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        self.committed += 1

    def close(self) -> None:
        self.closed += 1

    @property
    def sql_text(self) -> str:
        return "\n".join(sql for sql, _params in self.calls)


GOOD_CREATE = (
    "CREATE TABLE `t` (`doc_id` ... FULLTEXT KEY `ft_content` (`content`) WITH PARSER ngram "
    "PARSER_PROPERTIES=(ngram_token_size=2), VECTOR KEY `vdx_emb` (`emb`) WITH (DISTANCE=COSINE, TYPE=HNSW))"
)


def make_index(
    connection: FakeConnection,
    **overrides: Any,
) -> SeekdbKnowledgeIndex:
    config_kwargs: dict[str, Any] = {"dim": 4, "table": "aegis_doc"}
    config_kwargs.update(overrides)
    return SeekdbKnowledgeIndex(SeekdbConfig(**config_kwargs), connect=lambda: connection)


def doc(doc_id: str = "case_1", **overrides: Any) -> KnowledgeDoc:
    fields: dict[str, Any] = {"text": "泥石流沟口巡查需要加密", "hazard_type": "debris_flow", "region_code": "540121", "source": "cases"}
    fields.update(overrides)
    return KnowledgeDoc(doc_id=doc_id, **fields)


ROW = ("case_1", "泥石流沟口巡查需要加密", "debris_flow", "540121", "cases", None)


class TestSchema:
    def test_create_sql_pins_the_two_indexes_the_engine_actually_needs(self) -> None:
        sql = make_index(FakeConnection()).create_table_sql()
        assert "WITH PARSER ngram PARSER_PROPERTIES=(ngram_token_size=2)" in sql
        assert "distance=cosine, type=hnsw" in sql
        assert "emb VECTOR(4)" in sql

    @pytest.mark.parametrize("table", ["Orders; DROP TABLE x", "Upper", "1abc", "a" * 64, ""])
    def test_table_name_is_validated_before_it_reaches_the_sql(self, table: str) -> None:
        # 表名是唯一进 SQL 文本的标识符，其余全部走绑定参数。
        with pytest.raises(RetrievalArgumentError):
            SeekdbConfig(table=table)

    async def test_prepare_schema_rejects_the_silent_space_parser(self) -> None:
        """默认 `space` 解析器下中文召回全是 0 分，表却建得成：必须回读定义并响亮拒绝。"""
        conn = FakeConnection(results=[[("t", "CREATE TABLE `t` (FULLTEXT KEY `ft_content` (`content`) WITH PARSER space)")]])
        index = make_index(conn)

        with pytest.raises(SeekdbIndexError, match="不是 ngram 解析器"):
            await index.prepare_schema()

    async def test_prepare_schema_reports_what_the_engine_built(self) -> None:
        conn = FakeConnection(results=[[("t", GOOD_CREATE)]])
        facts = await make_index(conn).prepare_schema()

        assert facts["analyzer"] == "ngram(2)"
        assert facts["dim"] == 4

    async def test_prepare_schema_rejects_a_missing_vector_index(self) -> None:
        conn = FakeConnection(results=[[("t", "CREATE TABLE `t` (FULLTEXT KEY ft_content (content) WITH PARSER ngram)")]])
        with pytest.raises(SeekdbIndexError, match="余弦向量索引"):
            await make_index(conn).prepare_schema()


class TestUpsert:
    async def test_values_go_through_placeholders_not_the_sql_text(self) -> None:
        hostile = "a'; DROP TABLE aegis_doc; --"
        conn = FakeConnection()
        await make_index(conn).upsert([KnowledgeDoc(doc_id=hostile[:64], text="正文")], [[1, 0, 0, 0]])

        sql, params = conn.calls[0]
        assert "DROP TABLE" not in sql
        assert params[0] == hostile[:64]
        assert sql.count("%s") == 7

    async def test_vector_literal_is_exact_and_finite(self) -> None:
        assert _vector_literal([1, 2.5], dim=2) == "[1.0,2.5]"
        with pytest.raises(RetrievalArgumentError, match="维度"):
            _vector_literal([1, 2, 3], dim=2)
        with pytest.raises(RetrievalArgumentError, match="NaN"):
            _vector_literal([float("nan"), 0], dim=2)

    async def test_docs_and_vectors_must_pair_up(self) -> None:
        with pytest.raises(RetrievalArgumentError, match="数量不一致"):
            await make_index(FakeConnection()).upsert([doc()], [[1, 0, 0, 0], [0, 1, 0, 0]])

    async def test_empty_corpus_is_not_a_query(self) -> None:
        conn = FakeConnection()
        assert await make_index(conn).upsert([], []) == 0
        assert conn.calls == []

    async def test_oversized_doc_id_is_refused_instead_of_being_truncated(self) -> None:
        # 编号被截断会留下"写进去了但按原编号召不回"的暗坑。
        with pytest.raises(RetrievalArgumentError, match="超过列宽"):
            await make_index(FakeConnection()).upsert([doc("x" * 100)], [[1, 0, 0, 0]])

    async def test_repeated_upsert_updates_in_place(self) -> None:
        conn = FakeConnection()
        await make_index(conn).upsert([doc(text="第一版")], [[1, 0, 0, 0]])
        assert "ON DUPLICATE KEY UPDATE" in conn.calls[0][0]
        assert conn.committed == 1


class TestLegs:
    async def test_dense_leg_scores_from_cosine_distance_and_applies_the_threshold(self) -> None:
        conn = FakeConnection(
            results=[[[*ROW, 0.2], ["case_2", "低相关", "debris_flow", "540121", "cases", None, 0.9]]],
        )
        rows = await make_index(conn).dense_topk([1, 0, 0, 0], limit=5, filter=IndexFilter(score_threshold=0.5))

        sql, params = conn.calls[0]
        assert "cosine_distance(emb, %s)" in sql and "ORDER BY dist APPROXIMATE LIMIT %s" in sql
        assert params[0] == "[1.0,0.0,0.0,0.0]" and params[-1] == 5
        assert [row.doc.doc_id for row in rows] == ["case_1"]
        assert rows[0].score == pytest.approx(0.8)

    async def test_dense_leg_filters_are_bound_and_anded(self) -> None:
        conn = FakeConnection(results=[[]])
        await make_index(conn).dense_topk(
            [1, 0, 0, 0],
            limit=3,
            filter=IndexFilter(hazard_type="debris_flow", region_code="540121"),
        )
        sql, params = conn.calls[0]
        assert "hazard_type = %s" in sql and "region_code = %s" in sql
        assert params == ["[1.0,0.0,0.0,0.0]", "debris_flow", "540121", 3]

    async def test_lexical_leg_binds_the_match_expression_twice(self) -> None:
        conn = FakeConnection(results=[[(ROW[0], ROW[1], ROW[2], ROW[3], ROW[4], ROW[5], 1.69)]])
        rows = await make_index(conn).lexical_topk("泥石流", limit=4, filter=IndexFilter(hazard_type="debris_flow"))

        sql, params = conn.calls[0]
        assert sql.count("MATCH(content) AGAINST(%s IN NATURAL LANGUAGE MODE)") == 2
        assert params == ["泥石流", "泥石流", "debris_flow", 4]
        assert rows[0].score == 1.69
        assert rows[0].doc.hazard_type == "debris_flow"

    async def test_lexical_leg_short_circuits_blank_text(self) -> None:
        conn = FakeConnection()
        assert await make_index(conn).lexical_topk("   ", limit=5, filter=IndexFilter()) == ()
        assert conn.calls == []

    async def test_zero_limit_never_reaches_the_engine(self) -> None:
        conn = FakeConnection()
        index = make_index(conn)
        assert await index.dense_topk([1, 0, 0, 0], limit=0, filter=IndexFilter()) == ()
        assert await index.lexical_topk("泥石流", limit=0, filter=IndexFilter()) == ()
        assert conn.calls == []


class TestFailureTranslation:
    async def test_dbapi_errors_become_one_typed_error_with_a_cause(self) -> None:
        conn = FakeConnection(fail_with=OSError("connect refused"))
        with pytest.raises(SeekdbIndexError, match="connect refused") as caught:
            await make_index(conn).count()
        assert caught.value.detail["cause"] == "OSError"

    async def test_status_carries_the_last_error_but_never_the_password(self) -> None:
        conn = FakeConnection(fail_with=RuntimeError("超时会话"))
        index = make_index(conn)
        index._config.password = "sup3rs3cr3t"  # 断言的就是这个值不外泄
        with pytest.raises(SeekdbIndexError):
            await index.count()

        status = index.status()
        blob = str(status)
        assert "sup3rs3cr3t" not in blob
        assert status["last_error"] == "RuntimeError: 超时会话"
        assert status["target"].endswith("test.aegis_doc")

    async def test_connections_are_opened_per_call_and_always_closed(self) -> None:
        first, second = FakeConnection(results=[[]]), FakeConnection(results=[[]])
        connections = [first, second]
        index = SeekdbKnowledgeIndex(SeekdbConfig(dim=4, table="aegis_doc"), connect=lambda: connections.pop(0))

        await index.dense_topk([1, 0, 0, 0], limit=1, filter=IndexFilter())
        await index.lexical_topk("泥石流", limit=1, filter=IndexFilter())

        assert (first.closed, second.closed) == (1, 1), "两条腿并发取数不能共用一个非线程安全的连接"


class _RecordingIndex:
    """替身索引：让服务层的两腿都走外部引擎，验证候选与凭证来自索引而不是本地语料。"""

    def __init__(self) -> None:
        self.prepared = 0
        self.upserted: list[tuple[int, int]] = []

    async def prepare_schema(self) -> dict[str, object]:
        self.prepared += 1
        return {"analyzer": "ngram(2)"}

    async def upsert(self, docs: list[KnowledgeDoc], vectors: list[list[float]]) -> int:
        self.upserted.append((len(docs), len(vectors)))
        return len(docs)

    async def dense_topk(self, vector: Any, *, limit: int, filter: IndexFilter) -> list[Any]:
        from aegis.retrieval.port import IndexedRow

        return [IndexedRow(doc=doc("case_1"), score=0.9)]

    async def lexical_topk(self, text: str, *, limit: int, filter: IndexFilter) -> list[Any]:
        from aegis.retrieval.port import IndexedRow

        return [IndexedRow(doc=doc("case_2", text="冰湖溃决下游需撤空"), score=1.7)]


class TestServiceIntegration:
    async def test_both_legs_come_from_the_index_with_their_own_scores(self) -> None:
        index = _RecordingIndex()
        service = HybridRetrievalService(
            embedder=HashingEmbedder(),
            lexicon=build_lexical_index([]),
            index=index,
            corpus=[doc("case_1"), doc("case_2", text="冰湖溃决下游需撤空")],
        )
        outcome = await service.retrieve(RetrievalQuery(text="泥石流 沟口", k=2, rerank=False))

        assert outcome.legs_completed == ("dense", "lexical")
        assert not outcome.degraded, "外部索引在位时不该因为缺 pgvector 连接或本地语料而记降级"
        assert {item.doc_id for item in outcome.docs} == {"case_1", "case_2"}
        assert all(item.provenance.leg_scores for item in outcome.docs)

    async def test_prepare_index_embeds_the_corpus_and_writes_once(self) -> None:
        index = _RecordingIndex()
        service = HybridRetrievalService(
            embedder=HashingEmbedder(),
            lexicon=build_lexical_index([]),
            index=index,
            corpus=[doc("case_1"), doc("case_2")],
        )

        assert await service.prepare_index() == 2
        assert index.prepared == 1
        assert index.upserted == [(2, 2)]

    async def test_local_backend_is_untouched_by_the_index_seam(self) -> None:
        """没注入索引时，可用性判断必须回到原口径：缺连接 = 密集腿跳过并记账。"""
        service = HybridRetrievalService(embedder=HashingEmbedder(), lexicon=build_lexical_index([doc("case_1")]))
        outcome = await service.retrieve(RetrievalQuery(text="泥石流", k=1, rerank=False))

        assert [item.leg for item in outcome.degradations] == ["dense"]
        assert outcome.docs[0].doc_id == "case_1"
        assert await service.prepare_index() == 0
