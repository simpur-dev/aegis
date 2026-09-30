"""RRF 数学：默认 k=60、每腿权重、名次起点、并列打破、空腿与单文档。

这里的期望值都是手算的 1/(k+rank)，用来把公式钉死：
任何"顺手改成平方倒数/改成从 0 开始计数"的改动都会立刻红掉。
"""

from __future__ import annotations

import math

import pytest
from hypothesis import given
from hypothesis import strategies as st

from aegis.retrieval.fusion import DEFAULT_WEIGHTS, RRF_K, Ranking, fuse
from aegis.retrieval.onnx_io import RetrievalArgumentError


def rrf(rank: int, *, k: int = RRF_K, weight: float = 1.0) -> float:
    return weight / (k + rank)


def dense(ids: list[str]) -> Ranking:
    return Ranking.of("dense", [(doc_id, 0.9 - 0.01 * position) for position, doc_id in enumerate(ids)])


def lexical(ids: list[str]) -> Ranking:
    return Ranking.of("lexical", [(doc_id, 8.0 - 0.5 * position) for position, doc_id in enumerate(ids)])


class TestDefaults:
    def test_k_is_the_paper_value(self) -> None:
        assert RRF_K == 60
        assert DEFAULT_WEIGHTS == {"dense": 1.0, "lexical": 1.0}


class TestDisjointAndOverlap:
    def test_disjoint_lists_keep_both_with_leg_order(self) -> None:
        fused = fuse([dense(["a"]), lexical(["b"])])
        assert [hit.doc_id for hit in fused] == ["a", "b"]
        assert fused[0].score == pytest.approx(rrf(1))
        assert fused[1].score == pytest.approx(rrf(1))

    def test_overlap_accumulates_and_wins(self) -> None:
        fused = fuse([dense(["a", "shared"]), lexical(["shared", "b"])])
        assert fused[0].doc_id == "shared"
        assert fused[0].score == pytest.approx(rrf(2) + rrf(1))
        assert fused[1].doc_id == "a"
        assert fused[2].doc_id == "b"

    def test_full_overlap_reduces_to_first_leg_order(self) -> None:
        fused = fuse([dense(["a", "b", "c"]), lexical(["a", "b", "c"])])
        assert [hit.doc_id for hit in fused] == ["a", "b", "c"]
        assert [hit.score for hit in fused] == pytest.approx([rrf(1) * 2, rrf(2) * 2, rrf(3) * 2])

    def test_second_leg_beats_first_when_head_is_wrong(self) -> None:
        fused = fuse([dense(["noise", "target"]), lexical(["target"])])
        assert fused[0].doc_id == "target"
        assert fused[0].score == pytest.approx(rrf(2) + rrf(1))


class TestEmptyAndSingle:
    def test_no_rankings_at_all(self) -> None:
        assert fuse([]) == []

    def test_empty_legs_contribute_nothing(self) -> None:
        fused = fuse([dense([]), lexical([])])
        assert fused == []

    def test_one_empty_one_populated(self) -> None:
        fused = fuse([dense([]), lexical(["x", "y"])])
        assert [hit.doc_id for hit in fused] == ["x", "y"]
        assert fused[0].legs == ("lexical",)

    def test_single_doc_single_leg(self) -> None:
        fused = fuse([dense(["only"])])
        assert len(fused) == 1
        assert fused[0].score == pytest.approx(1 / 61)

    def test_input_order_of_legs_does_not_change_the_answer(self) -> None:
        left = fuse([dense(["a", "b"]), lexical(["b", "a"])])
        right = fuse([lexical(["b", "a"]), dense(["a", "b"])])
        assert [(hit.doc_id, hit.score) for hit in left] == [(hit.doc_id, hit.score) for hit in right]


class TestDeterministicTieBreak:
    def test_equal_scores_sort_by_doc_id(self) -> None:
        fused = fuse([dense(["zulu", "alpha", "mike"])])
        assert [hit.doc_id for hit in fused] == ["zulu", "alpha", "mike"], "名次不同就不该并列"

    def test_true_ties_break_on_id(self) -> None:
        # 两腿互为镜像：a 与 b 的融合分完全相同 -> 按 id 升序
        fused = fuse([dense(["a", "b"]), lexical(["b", "a"])])
        assert fused[0].doc_id == "a"
        assert fused[0].score == pytest.approx(fused[1].score)

    def test_repeated_id_in_one_leg_counts_once_at_best_rank(self) -> None:
        fused = fuse([dense(["a", "b", "a"])])
        assert len(fused) == 2
        assert fuse([dense(["a", "a"])])[0].score == pytest.approx(rrf(1))


class TestWeightsAndK:
    def test_zero_weight_drops_a_leg(self) -> None:
        fused = fuse([dense(["a"]), lexical(["b"])], weights={"lexical": 0.0})
        assert [hit.doc_id for hit in fused] == ["a"]

    def test_weight_scales_the_contribution(self) -> None:
        fused = fuse([dense(["a"]), lexical(["b"])], weights={"dense": 2.0})
        assert fused[0].doc_id == "a"
        assert fused[0].score == pytest.approx(2.0 * rrf(1))
        assert fused[1].score == pytest.approx(rrf(1))

    def test_weighted_leg_can_flip_the_order(self) -> None:
        rankings = [dense(["a", "b"]), lexical(["b"])]
        # a 只在密集腿排第一，b 是密集腿第二 + 词法腿第一：默认权重下 b 赢
        assert fuse(rankings)[0].doc_id == "b"
        # 要让"单腿第一名"翻盘，权重得大过 1/(k+1) 与 1/(k+2) 的差——这就是 RRF 的钝感设计
        assert fuse(rankings, weights={"lexical": 0.0})[0].doc_id == "a"
        assert fuse(rankings, weights={"dense": 100.0})[0].doc_id == "a"
        assert [hit.doc_id for hit in fuse(rankings, weights={"dense": 100.0})] == ["a", "b"]

    def test_unlisted_leg_defaults_to_unit_weight(self) -> None:
        fused = fuse([dense(["a"]), lexical(["b"])], weights={"dense": 1.0})
        assert fused[1].score == pytest.approx(rrf(1))

    @pytest.mark.parametrize("k", [0, 1, 60, 120])
    def test_k_changes_the_shape_not_the_order(self, k: int) -> None:
        fused = fuse([dense(["a", "b", "c"])], k=k)
        assert [hit.doc_id for hit in fused] == ["a", "b", "c"]
        assert fused[0].score == pytest.approx(1 / (k + 1))

    def test_negative_k_is_rejected(self) -> None:
        with pytest.raises(RetrievalArgumentError, match="k 不得为负"):
            fuse([dense(["a"])], k=-1)

    def test_negative_weight_is_rejected(self) -> None:
        with pytest.raises(RetrievalArgumentError, match="权重"):
            fuse([dense(["a"])], weights={"dense": -0.5})


class TestLimitAndProvenance:
    def test_limit_truncates_after_sorting(self) -> None:
        fused = fuse([dense(["a", "b", "c"])], limit=2)
        assert [hit.doc_id for hit in fused] == ["a", "b"]

    def test_non_positive_limit_is_rejected(self) -> None:
        with pytest.raises(RetrievalArgumentError, match="limit"):
            fuse([dense(["a"])], limit=0)

    def test_contributions_carry_rank_and_raw_scores(self) -> None:
        fused = fuse([dense(["a", "b"]), lexical(["b", "a"])])
        top = fused[0]
        assert [(item.leg, item.rank, item.score) for item in top.contributions] == [("dense", 1, 0.9), ("lexical", 2, 7.5)]
        assert top.raw_scores == {"dense": 0.9, "lexical": 7.5}
        assert [item.weighted for item in top.contributions] == pytest.approx([rrf(1), rrf(2)])

    def test_legs_are_reported_in_stable_order(self) -> None:
        fused = fuse([lexical(["shared"]), dense(["shared"])])
        assert fused[0].legs == ("dense", "lexical")


class TestRankingConstruction:
    def test_pairs_keep_ids_and_scores_in_sync(self) -> None:
        ranking = Ranking.of("dense", [("a", 1.5), ("b", -2.0)])
        assert ranking.doc_ids == ("a", "b")
        assert ranking.scores == (1.5, -2.0)
        assert len(ranking) == 2

    def test_score_of_missing_id_is_none(self) -> None:
        assert Ranking.of("dense", [("a", 1.0)]).score_of("b") is None

    def test_short_scores_fall_back_to_none_instead_of_index_error(self) -> None:
        assert Ranking(leg="dense", doc_ids=("a", "b"), scores=()).score_of("b") is None


class TestProperties:
    @given(
        dense_ids=st.lists(st.text(alphabet="abcdef", min_size=1, max_size=3), max_size=8, unique=True),
        lexical_ids=st.lists(st.text(alphabet="abcdef", min_size=1, max_size=3), max_size=8, unique=True),
        k=st.integers(min_value=0, max_value=200),
    )
    def test_output_is_sorted_unique_and_complete(self, dense_ids: list[str], lexical_ids: list[str], k: int) -> None:
        fused = fuse([dense(dense_ids), lexical(lexical_ids)], k=k)
        ids = [hit.doc_id for hit in fused]
        assert len(ids) == len(set(ids))
        assert set(ids) == set(dense_ids) | set(lexical_ids)
        scores = [hit.score for hit in fused]
        assert all(scores[i] >= scores[i + 1] for i in range(len(scores) - 1))
        assert all(score > 0 for score in scores)
        for hit in fused:
            expected = sum(1.0 / (k + item.rank) for item in hit.contributions)
            assert math.isclose(hit.score, expected, rel_tol=1e-12)
