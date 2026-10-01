"""真 seekdb 上的混合检索在线用例（默认跳过）。

启用方式（本机 `seekdb` 起服务后）：
    AEGIS_TEST_SEEKDB_HOST=127.0.0.1 \\
      uv run pytest -q tests/integration/test_retrieval_seekdb_live.py

凭据走环境变量：`AEGIS_TEST_SEEKDB_PORT/USER/PASSWORD/DATABASE`，默认 2881/root/空/test。
用例每次跑用自己的表名（`aegis_live_<8 位>`）并在结束时 DROP，绝不碰库里别的对象——
这条引擎是开发者本机在用的，不是 CI 的一次性容器。

嵌入用 `HashingEmbedder`（确定性 1024 维词面哈希）而不是 bge-m3：本文件要验的是
**引擎侧的建表、ngram 中文全文、HNSW 余弦 ANN、绑定参数与结构化过滤**这些运行时事实，
不是语义质量。哈希嵌入的分数不得用于任何准确率口径（口径见 REPORT 与 embedder 头注释）。
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest

from aegis.knowledge.cases import load_builtin_cases
from aegis.retrieval.corpus import docs_from_cases
from aegis.retrieval.embedder import HashingEmbedder
from aegis.retrieval.port import IndexFilter
from aegis.retrieval.seekdb import SeekdbConfig, SeekdbKnowledgeIndex
from aegis.retrieval.service import HybridRetrievalService, RetrievalQuery

HOST = os.getenv("AEGIS_TEST_SEEKDB_HOST", "")
PORT = int(os.getenv("AEGIS_TEST_SEEKDB_PORT", "2881") or "2881")
USER = os.getenv("AEGIS_TEST_SEEKDB_USER", "root")
PASSWORD = os.getenv("AEGIS_TEST_SEEKDB_PASSWORD", "")
DATABASE = os.getenv("AEGIS_TEST_SEEKDB_DATABASE", "test")

pytestmark = pytest.mark.skipif(not HOST, reason="未设置 AEGIS_TEST_SEEKDB_HOST，跳过真实 seekdb 检索集成")

CORPUS = docs_from_cases(load_builtin_cases())
EMBEDDER = HashingEmbedder()


def _drop(table: str) -> None:
    import pymysql

    connection = pymysql.connect(host=HOST, port=PORT, user=USER, password=PASSWORD, database=DATABASE, charset="utf8mb4")
    try:
        with connection.cursor() as cursor:
            cursor.execute(f"DROP TABLE IF EXISTS `{table}`")
        connection.commit()
    finally:
        connection.close()


@pytest.fixture
def index() -> AsyncIterator[SeekdbKnowledgeIndex]:
    table = f"aegis_live_{uuid.uuid4().hex[:8]}"
    built = SeekdbKnowledgeIndex(
        SeekdbConfig(
            host=HOST,
            port=PORT,
            user=USER,
            password=PASSWORD,
            database=DATABASE,
            table=table,
            dim=EMBEDDER.dim,
        )
    )
    yield built
    _drop(table)


@pytest.fixture
async def loaded_index(index: SeekdbKnowledgeIndex) -> SeekdbKnowledgeIndex:
    assert await index.prepare_schema() != {}
    vectors = EMBEDDER.embed([doc.text for doc in CORPUS])
    assert await index.upsert(CORPUS, vectors) == len(CORPUS)
    return index


class TestSchemaOnRealEngine:
    async def test_ngram_parser_and_hnsw_vector_index_actually_exist(self, index: SeekdbKnowledgeIndex) -> None:
        facts = await index.prepare_schema()

        assert facts["analyzer"] == "ngram(2)"
        # 这两行是本机踩过才知道必须回读的东西：space 解析器下表照建、查询照跑，中文召回全是 0 分。
        assert "WITH PARSER ngram" in str(facts["fulltext_index"])
        assert "DISTANCE=COSINE" in str(facts["vector_index"]) and "TYPE=HNSW" in str(facts["vector_index"])

    async def test_prepare_is_idempotent(self, index: SeekdbKnowledgeIndex) -> None:
        await index.prepare_schema()
        await index.prepare_schema()
        assert await index.count() == 0


class TestChineseLexicalLeg:
    async def test_ngram_full_text_recalls_the_chinese_cases(self, loaded_index: SeekdbKnowledgeIndex) -> None:
        rows = await loaded_index.lexical_topk("泥石流 沟口", limit=5, filter=IndexFilter())

        assert rows, "ngram 全文必须能按中文词面召回，而不是像默认解析器那样全 0"
        assert rows == tuple(sorted(rows, key=lambda row: row.score, reverse=True)), "词法腿必须按相关性降序"
        assert any("泥石流" in row.doc.text for row in rows)
        assert all(row.score > 0 for row in rows)

    async def test_lexical_scores_are_returned_per_leg_for_the_provenance_trail(self, loaded_index: SeekdbKnowledgeIndex) -> None:
        rows = await loaded_index.lexical_topk("冰湖溃决 下游", limit=3, filter=IndexFilter())

        assert rows
        assert {row.doc.doc_id for row in rows} <= {doc.doc_id for doc in CORPUS}, "召回必须回到案例本体，不许凭空造编号"

    async def test_hazard_filter_narrows_the_result_set(self, loaded_index: SeekdbKnowledgeIndex) -> None:
        unfiltered = await loaded_index.lexical_topk("巡查 加密", limit=20, filter=IndexFilter())
        filtered = await loaded_index.lexical_topk("巡查 加密", limit=20, filter=IndexFilter(hazard_type="patrol_route"))

        assert all(row.doc.hazard_type == "patrol_route" for row in filtered)
        assert len(filtered) <= len(unfiltered)

    async def test_blank_text_never_reaches_the_engine(self, loaded_index: SeekdbKnowledgeIndex) -> None:
        assert await loaded_index.lexical_topk("   ", limit=5, filter=IndexFilter()) == ()


class TestVectorLegOnRealEngine:
    async def test_ann_returns_nearest_first_with_usable_scores(self, loaded_index: SeekdbKnowledgeIndex) -> None:
        query = EMBEDDER.embed(["泥石流沟口巡查加密"])[0]
        rows = await loaded_index.dense_topk(query, limit=4, filter=IndexFilter())

        assert rows
        assert rows[0].doc.doc_id == "case_debris_01" or any("泥石流" in row.doc.text for row in rows)
        assert rows[0].score >= rows[-1].score, "余弦距离升序 = 相似度降序"
        assert all(row.score <= 1.0 for row in rows)

    async def test_threshold_drops_weak_candidates(self, loaded_index: SeekdbKnowledgeIndex) -> None:
        query = EMBEDDER.embed(["冰湖溃决下游撤空"])[0]
        loose = await loaded_index.dense_topk(query, limit=10, filter=IndexFilter())
        tight = await loaded_index.dense_topk(query, limit=10, filter=IndexFilter(score_threshold=1.1))

        assert loose and tight == ()


class TestUpsertSafety:
    async def test_hostile_identifiers_are_stored_as_data_not_sql(self, index: SeekdbKnowledgeIndex) -> None:
        await index.prepare_schema()
        hostile = "a'; DROP TABLE aegis_live_probe; --"
        doc = CORPUS[0].__class__(doc_id=hostile[:64], text="恶意编号只当数据", hazard_type="debris_flow")
        assert await index.upsert([doc], [EMBEDDER.embed([doc.text])[0]]) == 1

        rows = await index.lexical_topk("恶意编号", limit=5, filter=IndexFilter())
        assert [row.doc.doc_id for row in rows] == [hostile[:64]]
        assert await index.count() == 1, "写入不许把表干掉"

    async def test_re_upsert_updates_in_place(self, loaded_index: SeekdbKnowledgeIndex) -> None:
        before = await loaded_index.count()
        vectors = EMBEDDER.embed([doc.text for doc in CORPUS])
        assert await loaded_index.upsert(CORPUS, vectors) == len(CORPUS)
        assert await loaded_index.count() == before, "同一批编号重写应当是原地更新，不是追加"


class TestServiceThroughSeekdb:
    async def test_both_legs_run_and_provenance_keeps_each_leg_score(self, loaded_index: SeekdbKnowledgeIndex) -> None:
        service = HybridRetrievalService(
            embedder=EMBEDDER,
            lexicon=_empty_lexicon(),
            index=loaded_index,
            corpus=CORPUS,
        )
        outcome = await service.retrieve(RetrievalQuery(text="泥石流 沟口 巡查", k=3, rerank=False))

        assert outcome.legs_completed == ("dense", "lexical"), "两腿都该在真引擎上跑完，一条都没降级"
        assert not outcome.degraded
        assert outcome.docs
        for item in outcome.docs:
            assert set(item.provenance.leg_scores) <= {"dense", "lexical"}
        assert outcome.elapsed_ms < 5_000, "两腿并发取数的预算口径要能在真引擎上站住"

    async def test_structured_filter_reaches_the_engine_through_the_service(self, loaded_index: SeekdbKnowledgeIndex) -> None:
        service = HybridRetrievalService(embedder=EMBEDDER, lexicon=_empty_lexicon(), index=loaded_index, corpus=CORPUS)
        outcome = await service.retrieve(RetrievalQuery(text="巡查", k=5, hazard_type="patrol_route", rerank=False))

        assert all(item.hazard_type == "patrol_route" for item in outcome.docs)

    async def test_unreachable_engine_degrades_both_legs_instead_of_raising(self) -> None:
        """端口指到没在跑的端口：检索必须交回降级结果，而不是把异常抛进预警路径。"""
        dead = SeekdbKnowledgeIndex(SeekdbConfig(host=HOST, port=_closed_port(PORT), table="aegis_live_dead", dim=EMBEDDER.dim))
        service = HybridRetrievalService(embedder=EMBEDDER, lexicon=_empty_lexicon(), index=dead, corpus=CORPUS)

        outcome = await service.retrieve(RetrievalQuery(text="泥石流", k=3, rerank=False))

        assert outcome.docs == ()
        assert {item.leg for item in outcome.degradations} == {"dense", "lexical"}


def _empty_lexicon() -> Any:
    from aegis.retrieval.lexical import build_lexical_index

    return build_lexical_index([])


def _closed_port(port: int) -> int:
    """选一个几乎肯定没人听的端口，而不是随便写 1：1 会被驱动当成非法端口号。"""
    return 59999 if port != 59999 else 59998
