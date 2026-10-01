"""向量检索层边界测试：维度契约、参数校验、占位符编号与结果映射。

不连库：SQL 侧断言构造结果，执行侧用替身 conn 捕获实参。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from aegis.persistence.errors import VectorArgumentError, VectorEmbeddingError
from aegis.persistence.rows import EMBEDDING_DIM
from aegis.persistence.vectors import (
    MAX_K,
    VectorHit,
    build_top_k,
    check_k,
    check_score_threshold,
    search_top_k,
)


def _placeholders(sql: str) -> list[int]:
    """出现过的占位符编号（同一编号可合法地出现在 SELECT/WHERE/ORDER BY 多处）。"""
    return [int(n) for n in re.findall(r"\$(\d+)", sql)]


def vector(scale: float = 1.0, dim: int = EMBEDDING_DIM) -> list[float]:
    return [scale * (i % 7 + 1) / 10 for i in range(dim)]


class TestKBounds:
    def test_max_k_boundary_is_allowed(self) -> None:
        assert check_k(MAX_K) == MAX_K

    @pytest.mark.parametrize("bad", [0, -1, -1000])
    def test_rejects_non_positive_k(self, bad: int) -> None:
        with pytest.raises(VectorArgumentError, match="k 必须为正"):
            check_k(bad)

    def test_rejects_k_above_cap(self) -> None:
        with pytest.raises(VectorArgumentError, match="超出单次检索上限") as excinfo:
            check_k(MAX_K + 1)
        assert excinfo.value.detail["max"] == MAX_K


class TestScoreThreshold:
    def test_none_means_no_threshold(self) -> None:
        assert check_score_threshold(None) is None

    @pytest.mark.parametrize("value", [-1.0, 0.0, 1.0])
    def test_inclusive_bounds_allowed(self, value: float) -> None:
        assert check_score_threshold(value) == value

    @pytest.mark.parametrize("value", [-1.01, 1.01, 2.0])
    def test_out_of_range_rejected(self, value: float) -> None:
        with pytest.raises(VectorArgumentError, match=r"\[-1, 1\]"):
            check_score_threshold(value)


class TestEmbeddingContract:
    def test_wrong_dimension_surfaces_vector_dimension_error(self) -> None:
        """维度不符必须是专属错误：重试同一个向量必然再失败，调用方要换模型而不是重查。"""
        with pytest.raises(VectorEmbeddingError, match=str(EMBEDDING_DIM)) as excinfo:
            build_top_k([0.1] * 3, k=5)
        assert excinfo.value.detail.get("expected") == EMBEDDING_DIM or "1024" in str(excinfo.value)

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
    def test_non_finite_embedding_is_rejected(self, bad: float) -> None:
        with pytest.raises(VectorEmbeddingError):
            build_top_k([bad] * EMBEDDING_DIM, k=5)

    def test_empty_embedding_is_rejected(self) -> None:
        with pytest.raises(VectorEmbeddingError):
            build_top_k([], k=5)

    def test_custom_dim_is_honoured(self) -> None:
        sql, args = build_top_k([0.5] * 8, k=2, dim=8)
        assert len(args) >= 1
        assert "SELECT" in sql


class TestTopKSql:
    def test_uses_cosine_operator_and_orders_by_distance(self) -> None:
        sql, args = build_top_k(vector(), k=10)
        assert "<=>" in sql
        assert "ORDER BY embedding <=> $1::vector" in sql
        assert "1 - (embedding <=> $1::vector) AS score" in sql
        assert "LIMIT $2" in sql
        assert args[0] == vector()

    def test_embedding_is_always_a_bound_parameter(self) -> None:
        """向量绝不能被拼进 SQL 文本：既防注入也防语句膨胀。"""
        sql, args = build_top_k(vector(), k=3)
        assert "0.1" not in sql
        assert any(isinstance(a, list) for a in args)

    def test_filters_append_placeholders_in_order(self) -> None:
        since = datetime.now(UTC)
        sql, args = build_top_k(
            vector(),
            k=7,
            hazard_type="debris_flow",
            region_code="540100",
            since=since,
            until=since + timedelta(hours=1),
            score_threshold=0.5,
        )
        assert set(_placeholders(sql)) == set(range(1, len(args) + 1))
        assert "hazard_type = $" in sql and "region_code = $" in sql
        assert "created_at >= $" in sql and "created_at < $" in sql

    def test_no_filters_yields_minimal_where(self) -> None:
        sql, args = build_top_k(vector(), k=1)
        assert "embedding IS NOT NULL" in sql
        assert "hazard_type = " not in sql
        assert set(_placeholders(sql)) == {1, 2}
        assert len(args) == 2

    def test_score_threshold_becomes_similarity_floor(self) -> None:
        sql, args = build_top_k(vector(), k=5, score_threshold=0.8)
        assert "1 - (embedding <=> $2::vector) >= $2" in sql
        assert 0.8 in args

    def test_naive_or_reversed_window_rejected(self) -> None:
        aware = datetime.now(UTC)
        with pytest.raises(VectorArgumentError, match="必须带时区"):
            build_top_k(vector(), k=5, since=aware.replace(tzinfo=None))
        with pytest.raises(VectorArgumentError, match="必须带时区"):
            build_top_k(vector(), k=5, until=aware.replace(tzinfo=None))
        with pytest.raises(VectorArgumentError, match="逆序"):
            build_top_k(vector(), k=5, since=aware, until=aware - timedelta(seconds=1))

    def test_invalid_k_rejected_after_embedding_validation(self) -> None:
        with pytest.raises(VectorArgumentError, match="k 必须为正"):
            build_top_k(vector(), k=0)


class _FakeConn:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self.calls.append((sql, args))
        return self.rows


def _row(wid: str = "stu_1", score: float = 0.91) -> dict[str, Any]:
    return {
        "task_unit_id": wid,
        "hazard_type": "debris_flow",
        "region_code": "540100",
        "event_id": "evt_1",
        "objective": "疏浥排导槽",
        "created_at": datetime.now(UTC),
        "score": score,
    }


class TestSearchExecution:
    async def test_maps_rows_to_vector_hits(self) -> None:
        conn = _FakeConn([_row("stu_a", 0.98), _row("stu_b", 0.55)])
        hits = await search_top_k(conn, vector(), k=2)
        assert [h.id for h in hits] == ["stu_a", "stu_b"]
        assert hits[0].score == pytest.approx(0.98)
        assert isinstance(hits[0], VectorHit)
        assert hits[0].payload["hazard_type"] == "debris_flow"
        assert hits[0].payload["objective"] == "疏浥排导槽"

    async def test_empty_candidate_set_returns_empty_list(self) -> None:
        assert await search_top_k(_FakeConn([]), vector(), k=10) == []

    async def test_defaults_k_to_ten(self) -> None:
        conn = _FakeConn([])
        await search_top_k(conn, vector())
        _, args = conn.calls[0]
        assert args[-1] == 10

    async def test_argument_errors_propagate_before_any_query(self) -> None:
        conn = _FakeConn([])
        with pytest.raises(VectorEmbeddingError):
            await search_top_k(conn, [0.1], k=5)
        assert conn.calls == []


@settings(max_examples=50, deadline=None)
@given(
    k=st.integers(min_value=1, max_value=MAX_K),
    hazard=st.one_of(st.none(), st.sampled_from(["debris_flow", "glacial_lake_outburst", "wet_snow_avalanche"])),
    region=st.one_of(st.none(), st.from_regex(r"[0-9A-Z]{6}", fullmatch=True)),
    threshold=st.one_of(st.none(), st.floats(min_value=-1, max_value=1, allow_nan=False)),
)
def test_placeholder_numbering_is_contiguous_for_any_filter_combination(
    k: int, hazard: str | None, region: str | None, threshold: float | None
) -> None:
    sql, args = build_top_k(vector(), k=k, hazard_type=hazard, region_code=region, score_threshold=threshold)
    assert set(_placeholders(sql)) == set(range(1, len(args) + 1))
    for value in (hazard, region):
        if value:
            assert value not in sql
