"""geo 层边界测试：参数校验、占位符编号连续性、注入防护与执行编排。

全部不依赖真实数据库：SQL 侧只断言"构造结果"，执行侧用替身 conn 捕获实参。
"""

from __future__ import annotations

import asyncio
import math
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from aegis.persistence.errors import GeoArgumentError
from aegis.persistence.geo import (
    MAX_LIMIT,
    MAX_RADIUS_M,
    build_stations_in_polygon,
    build_stations_within,
    build_trace_reading_count,
    build_trace_warning_count,
    check_limit,
    check_point,
    check_polygon,
    check_radius,
    check_window,
    point_text,
    stations_in_polygon,
    stations_within,
    trace_summary,
)

LINZHOU = (91.28, 29.896)
TRIANGLE = "POLYGON((91.2 29.8, 91.4 29.8, 91.4 30.0, 91.2 29.8))"


def _placeholders(sql: str) -> list[int]:
    return sorted(int(n) for n in re.findall(r"\$(\d+)", sql))


def assert_well_formed(sql: str, args: list[Any]) -> None:
    """SQL 构造层不变式：每个参数都被引用至少一次，且不存在越界占位符。

    用集合而非序列：同一个参数（如 $1 的查询向量、$1 的 WKT）合法地出现在
    SELECT / WHERE / ORDER BY 多处，序列相等会把这种正确写法判为失败。
    """
    nums = set(_placeholders(sql))
    assert nums == set(range(1, len(args) + 1)), f"占位符与参数表不匹配: {sorted(nums)} vs 1..{len(args)}"


class TestPointBounds:
    def test_accepts_inclusive_longitude_and_latitude_bounds(self) -> None:
        assert check_point(-180.0, -90.0) == (-180.0, -90.0)
        assert check_point(180.0, 90.0) == (180.0, 90.0)

    @pytest.mark.parametrize("lon", [-180.1, 180.1, 999.0])
    def test_rejects_out_of_range_longitude(self, lon: float) -> None:
        with pytest.raises(GeoArgumentError, match="经度越界") as excinfo:
            check_point(lon, 30.0)
        assert excinfo.value.detail["lon"] == lon

    @pytest.mark.parametrize("lat", [-90.1, 90.1])
    def test_rejects_out_of_range_latitude(self, lat: float) -> None:
        with pytest.raises(GeoArgumentError, match="纬度越界"):
            check_point(91.0, lat)

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
    def test_rejects_non_finite_coordinates(self, bad: float) -> None:
        with pytest.raises(GeoArgumentError, match="有限数值"):
            check_point(bad, 30.0)
        with pytest.raises(GeoArgumentError, match="有限数值"):
            check_point(91.0, bad)


class TestRadiusBounds:
    def test_max_radius_boundary_is_allowed(self) -> None:
        assert check_radius(MAX_RADIUS_M) == MAX_RADIUS_M

    @pytest.mark.parametrize("bad", [0.0, -1.0, float("nan"), float("inf")])
    def test_rejects_non_positive_radius(self, bad: float) -> None:
        with pytest.raises(GeoArgumentError, match="半径必须为正"):
            check_radius(bad)

    def test_rejects_radius_above_query_cap(self) -> None:
        with pytest.raises(GeoArgumentError, match="上限") as excinfo:
            check_radius(MAX_RADIUS_M + 1)
        assert excinfo.value.detail["max_m"] == MAX_RADIUS_M


class TestLimitBounds:
    def test_max_limit_boundary_is_allowed(self) -> None:
        assert check_limit(MAX_LIMIT) == MAX_LIMIT

    @pytest.mark.parametrize("bad", [0, -5])
    def test_rejects_non_positive_limit(self, bad: int) -> None:
        with pytest.raises(GeoArgumentError, match="limit 必须为正"):
            check_limit(bad)

    def test_rejects_limit_above_cap(self) -> None:
        with pytest.raises(GeoArgumentError, match="超出上限"):
            check_limit(MAX_LIMIT + 1)


class TestPolygonGuard:
    @pytest.mark.parametrize(
        "wkt",
        [
            TRIANGLE,
            "  " + TRIANGLE + "  ",
            TRIANGLE.lower(),
            "MULTIPOLYGON(((91.2 29.8, 91.4 29.8, 91.4 30.0, 91.2 29.8)))",
        ],
    )
    def test_accepts_polygon_and_multipolygon(self, wkt: str) -> None:
        assert check_polygon(wkt) == wkt.strip()

    @pytest.mark.parametrize("wkt", ["", "   ", "POINT(91 29)", "LINESTRING(91 29, 92 30)", "POLYGON((91 29"])
    def test_rejects_non_areal_or_truncated_wkt(self, wkt: str) -> None:
        with pytest.raises(GeoArgumentError, match="POLYGON"):
            check_polygon(wkt)


class TestWindowGuard:
    def test_naive_since_is_rejected(self) -> None:
        with pytest.raises(GeoArgumentError, match="必须带时区"):
            check_window(datetime.now(), None)

    def test_naive_until_is_rejected(self) -> None:
        since = datetime.now(UTC)
        naive_until = (since + timedelta(hours=1)).replace(tzinfo=None)
        with pytest.raises(GeoArgumentError, match="必须带时区"):
            check_window(since, naive_until)

    def test_forward_aware_window_is_accepted(self) -> None:
        since = datetime.now(UTC)
        assert check_window(since, since + timedelta(hours=1)) is None

    def test_reversed_or_empty_window_is_rejected(self) -> None:
        since = datetime.now(UTC)
        with pytest.raises(GeoArgumentError, match="逆序"):
            check_window(since, since - timedelta(seconds=1))
        with pytest.raises(GeoArgumentError, match="逆序"):
            check_window(since, since)

    def test_open_ended_and_forward_windows_are_allowed(self) -> None:
        since = datetime.now(UTC)
        assert check_window(since, None) is None
        assert check_window(since, since + timedelta(microseconds=1)) is None


class TestPointText:
    def test_longitude_first_with_srid_and_fixed_precision(self) -> None:
        assert point_text(*LINZHOU) == "SRID=4326;POINT(91.280000 29.896000)"

    def test_negative_longitude_keeps_sign_and_space(self) -> None:
        assert point_text(-71.5, -33.25) == "SRID=4326;POINT(-71.500000 -33.250000)"


class TestStationsWithinSql:
    def test_uses_geography_distance_and_orders_by_distance(self) -> None:
        sql, args = build_stations_within(lon=LINZHOU[0], lat=LINZHOU[1], radius_m=5000, limit=10)
        assert "ST_DWithin(geom, ST_GeogFromText($1), $2)" in sql
        assert "ORDER BY distance_m ASC" in sql
        assert args[0].startswith("SRID=4326;POINT(")
        assert args[1] == 5000.0
        assert_well_formed(sql, args)

    def test_region_filter_shifts_limit_placeholder(self) -> None:
        plain_sql, plain_args = build_stations_within(lon=91.0, lat=29.0, radius_m=100, limit=5)
        filtered_sql, filtered_args = build_stations_within(lon=91.0, lat=29.0, radius_m=100, limit=5, region_code="540100")
        assert "region_code = $3" in filtered_sql
        assert "LIMIT $4" in filtered_sql
        assert "LIMIT $3" in plain_sql
        assert len(filtered_args) == len(plain_args) + 1
        assert_well_formed(filtered_sql, filtered_args)

    def test_values_never_enter_sql_text(self) -> None:
        """注入防护：恶意取值必须留在参数表里，SQL 文本不得出现其片段。"""
        evil = "540100'); DROP TABLE monitoring_stations; --"
        sql, args = build_stations_within(lon=91.0, lat=29.0, radius_m=100, limit=5, region_code=evil)
        assert evil in args
        assert "DROP TABLE" not in sql
        assert "540100" not in sql

    def test_invalid_arguments_are_rejected_before_sql_construction(self) -> None:
        with pytest.raises(GeoArgumentError):
            build_stations_within(lon=200.0, lat=29.0, radius_m=100, limit=5)


class TestStationsInPolygonSql:
    def test_uses_covers_and_orders_by_station_id(self) -> None:
        sql, args = build_stations_in_polygon(polygon_wkt=TRIANGLE)
        assert "ST_Covers(ST_GeogFromText($1), geom)" in sql
        assert "ORDER BY station_id" in sql
        assert args == [TRIANGLE]
        assert_well_formed(sql, args)

    def test_rejects_non_areal_geometry(self) -> None:
        with pytest.raises(GeoArgumentError, match="POLYGON"):
            build_stations_in_polygon(polygon_wkt="POINT(91 29)")


class TestTraceCountSql:
    def test_reading_count_has_no_hazard_filter(self) -> None:
        """遥测按观测量建模，灾种过滤只作用于预警计数（口径决定，勿"顺手加过滤"）。"""
        since = datetime.now(UTC)
        sql, args = build_trace_reading_count(polygon_wkt=TRIANGLE, since=since, until=since + timedelta(hours=1))
        assert "hazard_type" not in sql
        assert since in args
        assert_well_formed(sql, args)

    def test_reading_count_without_upper_bound(self) -> None:
        sql, args = build_trace_reading_count(polygon_wkt=TRIANGLE, since=datetime.now(UTC), until=None)
        assert "observed_at <" not in sql
        assert_well_formed(sql, args)

    def test_warning_count_adds_hazard_placeholder(self) -> None:
        since = datetime.now(UTC)
        until = since + timedelta(days=1)
        plain, plain_args = build_trace_warning_count(polygon_wkt=TRIANGLE, since=since, until=until, hazard_type=None)
        typed, typed_args = build_trace_warning_count(polygon_wkt=TRIANGLE, since=since, until=until, hazard_type="debris_flow")
        assert "w.hazard_type = $4" in typed
        assert "hazard_type" not in plain
        assert len(typed_args) == len(plain_args) + 1
        assert_well_formed(plain, plain_args)
        assert_well_formed(typed, typed_args)

    def test_warning_count_scopes_by_station_region_inside_polygon(self) -> None:
        since = datetime.now(UTC)
        sql, _ = build_trace_warning_count(polygon_wkt=TRIANGLE, since=since, until=None, hazard_type=None)
        assert "w.region_code IN (" in sql
        assert "ST_Covers(ST_GeogFromText($1), s.geom)" in sql


class _FakeConn:
    """记录调用参数的 asyncpg 连接替身。"""

    def __init__(self, rows: list[dict[str, Any]] | None = None, row: dict[str, Any] | None = None, delay: float = 0.0) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self._rows = rows or []
        self._row = row or {"n": 0, "warning_ids": []}
        self._delay = delay

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self.calls.append((sql, args))
        if self._delay:
            await asyncio.sleep(self._delay)
        return self._rows

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any]:
        self.calls.append((sql, args))
        if self._delay:
            await asyncio.sleep(self._delay)
        return self._row


class TestQueryExecution:
    async def test_stations_within_returns_dicts_and_passes_bound_args(self) -> None:
        conn = _FakeConn(rows=[{"station_id": "S1", "distance_m": 120.0}])
        out = await stations_within(conn, lon=91.0, lat=29.0, radius_m=1000, limit=3)
        assert out == [{"station_id": "S1", "distance_m": 120.0}]
        sql, args = conn.calls[0]
        assert "ST_DWithin" in sql
        assert list(args) == [point_text(91.0, 29.0), 1000.0, 3]

    async def test_empty_candidate_set_is_empty_list_not_error(self) -> None:
        assert await stations_within(_FakeConn(rows=[]), lon=0.0, lat=0.0, radius_m=10, limit=1) == []
        assert await stations_in_polygon(_FakeConn(rows=[]), polygon_wkt=TRIANGLE) == []

    async def test_trace_summary_runs_three_queries_concurrently(self) -> None:
        """三条子查询互不依赖：串行会累加时延，并发应接近单条耗时。"""
        conn = _FakeConn(rows=[{"station_id": "S1"}], row={"n": 7, "warning_ids": ["W1", "W2"]}, delay=0.05)
        since = datetime.now(UTC)
        task = asyncio.create_task(trace_summary(conn, polygon_wkt=TRIANGLE, since=since))
        summary = await asyncio.wait_for(task, timeout=5.0)
        assert len(conn.calls) == 3
        assert summary["station_count"] == 1
        assert summary["readings"] == 7
        assert summary["warning_ids"] == ["W1", "W2"]
        assert summary["polygon"] == TRIANGLE

    async def test_trace_summary_propagates_argument_errors(self) -> None:
        with pytest.raises(GeoArgumentError):
            await trace_summary(_FakeConn(), polygon_wkt="BOGUE", since=datetime.now(UTC))


@settings(max_examples=60, deadline=None)
@given(
    lon=st.floats(min_value=-180, max_value=180, allow_nan=False, allow_infinity=False),
    lat=st.floats(min_value=-90, max_value=90, allow_nan=False, allow_infinity=False),
    radius=st.floats(min_value=0.001, max_value=MAX_RADIUS_M, allow_nan=False, allow_infinity=False),
    limit=st.integers(min_value=1, max_value=MAX_LIMIT),
    region=st.one_of(st.none(), st.text(min_size=1, max_size=24)),
)
def test_stations_within_placeholder_invariant_holds_for_any_valid_input(
    lon: float, lat: float, radius: float, limit: int, region: str | None
) -> None:
    sql, args = build_stations_within(lon=lon, lat=lat, radius_m=radius, limit=limit, region_code=region)
    assert_well_formed(sql, args)
    # 取值一律走绑定参数：构造出的 SQL 里不该出现任何字符串字面量。
    # （不能拿 `value in sql` 判断——'1' 这种取值天然出现在 $1 / LIMIT $4 里，那是占位符不是数据。）
    assert "'" not in sql


@settings(max_examples=40, deadline=None)
@given(bad=st.floats(allow_nan=False).filter(lambda v: not math.isfinite(v) or v <= 0 or v > MAX_RADIUS_M))
def test_any_illegal_radius_is_rejected(bad: float) -> None:
    with pytest.raises(GeoArgumentError):
        check_radius(bad)
