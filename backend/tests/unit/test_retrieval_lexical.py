"""词法腿契约：切分口径、BM25 排名数学、结构化过滤、确定性并列。

全部是纯函数测试：不建库、不读文件，索引就在内存里构造。
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from aegis.retrieval.docs import KnowledgeDoc
from aegis.retrieval.lexical import DEFAULT_B, DEFAULT_K1, LexicalIndex, build_lexical_index, rank, tokenize
from aegis.retrieval.onnx_io import RetrievalArgumentError

CORPUS: tuple[KnowledgeDoc, ...] = (
    KnowledgeDoc("d_debris", "林周县短时强降水触发泥石沟沟道堵塞，需疏散河道两岸群众", "debris_flow", "540121", "warning_history"),
    KnowledgeDoc("d_slide", "当雄县坡体后缘裂缝持续扩张，疑似滑坡前兆，禁止返回危险区", "landslide", "540122", "warning_history"),
    KnowledgeDoc("d_quake", "震后次生滑坡风险升高，值守巡查频次加倍", "quake_triggered", "540121", "playbook"),
    KnowledgeDoc("d_code", "G318线K12+500处 50mm 过程雨量达阈值", "debris_flow", "540121", "telemetry"),
)


def index_of(docs: list[KnowledgeDoc] | None = None) -> LexicalIndex:
    return build_lexical_index(docs if docs is not None else list(CORPUS))


class TestTokenize:
    def test_cjk_produces_unigrams_and_bigrams(self) -> None:
        tokens = tokenize("泥石流")
        assert tokens == ["泥", "石", "流", "泥石", "石流"]

    def test_single_char_is_a_unigram_only(self) -> None:
        assert tokenize("雨") == ["雨"]

    def test_ascii_runs_are_lowercased_words(self) -> None:
        tokens = tokenize("G318线K12")
        assert "g318" in tokens and "k12" in tokens
        assert "G318" not in tokens

    def test_punctuation_splits_runs(self) -> None:
        # 每段连续汉字先出 unigram 再出 bigram，段与段之间互不跨接
        assert tokenize("坡体，裂缝") == ["坡", "体", "坡体", "裂", "缝", "裂缝"]

    def test_mixed_code_and_units(self) -> None:
        tokens = tokenize("50mm 雨量")
        assert "50mm" in tokens and "雨" in tokens and "雨量" in tokens

    def test_empty_and_symbol_only_text(self) -> None:
        assert tokenize("") == []
        assert tokenize("！！！，。，") == []

    def test_long_run_counts(self) -> None:
        # n 个连续汉字 -> n 个 unigram + (n-1) 个 bigram
        assert len(tokenize("西藏山地灾害")) == 6 + 5


class TestBuildIndex:
    def test_empty_input_is_queryable(self) -> None:
        index = build_lexical_index([])
        assert index.is_empty and len(index) == 0
        assert rank(index, "泥石流") == []

    def test_duplicate_doc_id_overwrites(self) -> None:
        docs = [
            KnowledgeDoc("a", "泥石流"),
            KnowledgeDoc("a", "雪崩"),
        ]
        index = index_of(docs)
        assert len(index) == 1
        assert index.get("a") is not None and index.get("a").text == "雪崩"
        assert [hit.doc_id for hit in rank(index, "雪崩")] == ["a"]

    def test_get_returns_none_for_unknown(self) -> None:
        assert index_of().get("nope") is None


class TestRanking:
    def test_only_matching_docs_come_back(self) -> None:
        hits = rank(index_of(), "泥石流 沟道")
        assert [hit.doc_id for hit in hits] == ["d_debris"]
        assert hits[0].score > 0.0

    def test_multi_word_query_orders_by_overlap(self) -> None:
        hits = rank(index_of(), "坡体裂缝扩张危险区")
        assert hits[0].doc_id == "d_slide"

    def test_scores_descend(self) -> None:
        hits = rank(index_of(), "滑坡风险巡查")
        assert all(hits[i].score >= hits[i + 1].score for i in range(len(hits) - 1))

    def test_rare_term_outweighs_common_term(self) -> None:
        """idf 口径：出现在全部文档里的词几乎不带信息量，稀有词命中应当更值钱。"""
        index = index_of(
            [
                KnowledgeDoc("c1", "风险"),
                KnowledgeDoc("c2", "风险"),
                KnowledgeDoc("rare", "溃决"),
            ]
        )
        hits = rank(index, "风险 溃决")
        assert [hit.doc_id for hit in hits] == ["rare", "c1", "c2"]

    def test_doc_length_normalization_prefers_short_exact_doc(self) -> None:
        index = index_of(
            [
                KnowledgeDoc("short", "泥石流"),
                KnowledgeDoc("long", "泥石流 " + "的" * 60),
            ]
        )
        hits = rank(index, "泥石流")
        assert hits[0].doc_id == "short"

    def test_k_limits_the_result(self) -> None:
        hits = rank(index_of(), "灾害风险区域", k=1)
        assert len(hits) == 1

    def test_ties_break_by_doc_id(self) -> None:
        index = index_of([KnowledgeDoc("z_doc", "冰湖溃决"), KnowledgeDoc("a_doc", "冰湖溃决")])
        hits = rank(index, "冰湖溃决")
        assert [hit.doc_id for hit in hits] == ["a_doc", "z_doc"]

    def test_zero_score_docs_are_dropped(self) -> None:
        assert rank(index_of(), "完全不相关的外星词语") == []

    def test_degenerate_query_returns_empty(self) -> None:
        assert rank(index_of(), "！！！") == []

    @pytest.mark.parametrize("bad_k", [0, -3])
    def test_k_must_be_positive(self, bad_k: int) -> None:
        with pytest.raises(RetrievalArgumentError, match="k 必须为正"):
            rank(index_of(), "泥石流", k=bad_k)

    def test_empty_corpus_is_not_an_error(self) -> None:
        assert rank(build_lexical_index([]), "泥石流") == []

    def test_zero_length_docs_cannot_divide_by_zero(self) -> None:
        index = index_of([KnowledgeDoc("blank", ""), KnowledgeDoc("word", "泥石流")])
        hits = rank(index, "泥石")
        assert [hit.doc_id for hit in hits] == ["word"]


class TestFilters:
    def test_where_predicate_narrows_the_candidate_set(self) -> None:
        index = index_of()
        hits = rank(index, "滑坡 风险", where=lambda doc: doc.hazard_type == "quake_triggered")
        assert [hit.doc_id for hit in hits] == ["d_quake"]

    def test_region_and_hazard_combined(self) -> None:
        def predicate(doc: KnowledgeDoc) -> bool:
            return doc.matches(hazard_type="debris_flow", region_code="540121")

        ids = {hit.doc_id for hit in rank(index_of(), "50mm 沟道 疏散", where=predicate)}
        assert ids == {"d_debris", "d_code"}

    def test_filter_that_matches_nothing(self) -> None:
        assert rank(index_of(), "泥石流", where=lambda doc: doc.region_code == "999999") == []


class TestHyperparameters:
    def test_defaults_are_the_paper_values(self) -> None:
        assert pytest.approx(1.2) == DEFAULT_K1
        assert pytest.approx(0.75) == DEFAULT_B

    def test_b_zero_disables_length_normalization(self) -> None:
        index = index_of([KnowledgeDoc("short", "泥石流"), KnowledgeDoc("long", "泥石流 " + "的" * 60)])
        even = rank(index, "泥石流", b=0.0)
        assert even[0].doc_id == "long"  # 不做长度归一时，长文档的词频累加更高

    def test_scores_are_scale_free_positive(self) -> None:
        hits = rank(index_of(), "泥石流 坡体 裂缝 50mm", k1=2.0, b=0.3)
        assert all(hit.score > 0 for hit in hits)


def _doc_strategy() -> st.SearchStrategy[KnowledgeDoc]:
    text = st.text(alphabet="泥石滑坡危岩雨雪0123AB观测点，。 ", min_size=0, max_size=40)
    return st.builds(
        KnowledgeDoc,
        doc_id=st.text(alphabet="abcdef0123456789", min_size=1, max_size=8).map(lambda raw: f"d_{raw}"),
        text=text,
    )


class TestProperties:
    @given(docs=st.lists(_doc_strategy(), max_size=12), query=st.text(alphabet="泥石滑坡雨雪012AB，", max_size=12))
    def test_ranking_is_ordered_unique_bounded_and_in_corpus(self, docs: list[KnowledgeDoc], query: str) -> None:
        index = build_lexical_index(docs)
        corpus_ids = {doc.doc_id for doc in docs}
        hits = rank(index, query, k=5)
        ids = [hit.doc_id for hit in hits]

        assert set(ids) <= corpus_ids
        assert len(ids) == len(set(ids))
        assert len(ids) <= min(5, len(index))
        assert all(hit.score > 0 for hit in hits)
        assert all(hits[i].score >= hits[i + 1].score for i in range(len(hits) - 1))

    @given(docs=st.lists(_doc_strategy(), max_size=8))
    def test_index_doc_count_respects_doc_id_uniqueness(self, docs: list[KnowledgeDoc]) -> None:
        index = build_lexical_index(docs)
        assert len(index) == len({doc.doc_id for doc in docs})
