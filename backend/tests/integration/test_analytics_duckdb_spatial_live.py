"""真 spatial 扩展下的点-多边形判定：与一个独立实现的射线法对账。

单元套件里几乎每条用例都传 `allow_spatial=False`（那是诚实的做法：无网机器装不上扩展，
不能把网络可达性当测试前提）。代价是 `within_polygon` 的**几何语义**从来没被真跑过——
`ST_MakePoint(lon, lat)` 的经纬顺序、环的方向、带洞多边形、无坐标事实是否漏进来，
任何一处写反，现有测试全都照样绿。这台机器上扩展是齐的（`~/.duckdb/extensions/<版本>/…/spatial…`），
所以这条集成用例把真判定跑起来，并拿一个**独立实现**的射线法做参照，而不是自己跟自己对表。

跑法：`uv run pytest -q -m slow tests/integration/test_analytics_duckdb_spatial_live.py`
（不依赖任何外部服务；扩展装不上时整文件跳过，并把跳过原因留在用例里。）
"""

from __future__ import annotations

import math
import random
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from aegis.analytics.duckdb_warehouse import DuckDbWarehouse
from aegis.analytics.port import FactRow, WarehouseError

pytestmark = pytest.mark.slow

BASE = datetime(2026, 9, 30, 5, 0, tzinfo=UTC)

# 一个凹多边形（拉萨附近）：外环 + 一个内环（洞）。"区内但有排除带"这种形状必须能表达。
EXTERIOR = [(90.80, 29.40), (91.55, 29.42), (91.60, 30.05), (91.10, 29.85), (90.70, 29.95)]
HOLE = [(91.05, 29.60), (91.25, 29.60), (91.25, 29.78), (91.05, 29.78)]


def _ring_wkt(ring: list[tuple[float, float]]) -> str:
    closed = ring + ring[:1]
    return "(" + ", ".join(f"{lon:.6f} {lat:.6f}" for lon, lat in closed) + ")"


def polygon_wkt(*, with_hole: bool = True) -> str:
    rings = [_ring_wkt(EXTERIOR)]
    if with_hole:
        rings.append(_ring_wkt(HOLE))
    return f"POLYGON ({', '.join(rings)})"


def in_ring(point: tuple[float, float], ring: list[tuple[float, float]]) -> bool:
    """射线法（独立实现，不用 duckdb 也不用 shapely）：与引擎无关的第二意见。

    y 用半开区间约定，配合取样时剔除边界邻域，避开"点正好压在顶点/边上"的退化情形。
    """
    x, y = point
    hit = False
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            x_hit = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x < x_hit:
                hit = not hit
    return hit


def oracle_inside(point: tuple[float, float]) -> bool:
    return in_ring(point, EXTERIOR) and not in_ring(point, HOLE)


def _distance_to_segment(p: tuple[float, float], a: tuple[float, float], b: tuple[float, float]) -> float:
    px, py = p
    ax, ay = a
    dx, dy = b[0] - ax, b[1] - ay
    length = dx * dx + dy * dy
    t = 0.0 if length == 0.0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length))
    return math.dist((px, py), (ax + t * dx, ay + t * dy))


def _far_from_edges(point: tuple[float, float], *, min_deg: float = 0.01) -> bool:
    for ring in (EXTERIOR, HOLE):
        n = len(ring)
        for i in range(n):
            if _distance_to_segment(point, ring[i], ring[(i + 1) % n]) < min_deg:
                return False
    return True


def _fact(event_id: str, point: tuple[float, float]) -> FactRow:
    lon, lat = point
    return FactRow(
        event_id=event_id,
        kind="telemetry",
        observed_at=BASE,
        region_code="540100",
        station_id=f"ST-{event_id}",
        metric="rainfall_mm",
        value=1.0,
        lat=lat,
        lon=lon,
    )


@pytest.fixture(scope="module")
def sample_points() -> list[tuple[str, tuple[float, float]]]:
    """撒点取 160 个，只留离任何边界都足够远的：边界归属由引擎约定决定，不参与对账。"""
    rng = random.Random(20260930)
    out: list[tuple[str, tuple[float, float]]] = []
    for _ in range(4000):
        if len(out) >= 160:
            break
        lon = 90.60 + rng.random() * 1.10
        lat = 29.30 + rng.random() * 0.85
        if not _far_from_edges((lon, lat)):
            continue
        out.append((f"evt-{len(out):03d}", (lon, lat)))
    return out


def expected_inside(sample_points: list[tuple[str, tuple[float, float]]]) -> set[str]:
    return {event_id for event_id, point in sample_points if oracle_inside(point)}


@pytest.fixture
async def warehouse(tmp_path: Path, sample_points: list[tuple[str, tuple[float, float]]]) -> Any:
    target = DuckDbWarehouse(tmp_path / "edge.duckdb", allow_spatial=True)
    await target.apply_schema()
    if not target.spatial_ready:
        # 离线机装不上扩展是常态：这不是缺陷，但也不能把"跑不了"记成"验过了"。
        pytest.skip(f"spatial 扩展不可用，点-多边形语义无法在线核验：{target.stats().get('spatial_reason')}")
    await target.append([_fact(event_id, point) for event_id, point in sample_points])
    await target.flush()
    yield target
    await target.close(500)


class TestSpatialSemantics:
    async def test_capability_bit_is_on_when_the_extension_loaded(self, warehouse: DuckDbWarehouse) -> None:
        assert warehouse.spatial_ready is True
        assert warehouse.stats()["spatial_reason"] is None

    async def test_within_polygon_matches_an_independent_ray_casting_oracle(
        self, warehouse: DuckDbWarehouse, sample_points: list[tuple[str, tuple[float, float]]]
    ) -> None:
        returned = {row["event_id"] for row in await warehouse.within_polygon(polygon_wkt(with_hole=True))}
        expected = expected_inside(sample_points)
        assert len(expected) >= 20, f"落进多边形内的样本只有 {len(expected)} 个，这条对账没有区分度"
        assert returned == expected, (
            f"引擎与独立射线法不一致：只在引擎里 {sorted(returned - expected)}；只在参照里 {sorted(expected - returned)}"
        )

    async def test_interior_ring_is_what_excludes_the_hole(
        self, warehouse: DuckDbWarehouse, sample_points: list[tuple[str, tuple[float, float]]]
    ) -> None:
        in_hole = {event_id for event_id, point in sample_points if in_ring(point, HOLE)}
        assert in_hole, "样本里没有点落进洞里，这条用例退化成了普通多边形判定"
        with_hole = {row["event_id"] for row in await warehouse.within_polygon(polygon_wkt(with_hole=True))}
        without_hole = {row["event_id"] for row in await warehouse.within_polygon(polygon_wkt(with_hole=False))}
        assert in_hole <= without_hole, "不写内环时洞内的点都没被召回，说明外环判定本身就错了"
        assert not (in_hole & with_hole), "写了内环仍召回洞内的点：环的内外语义与预期相反"

    async def test_lon_lat_order_is_what_we_claim(
        self, warehouse: DuckDbWarehouse, sample_points: list[tuple[str, tuple[float, float]]]
    ) -> None:
        """`ST_MakePoint(lon, lat)` 写反不会报错，只会把点搬到别的国家——所以正反两个框都钉一条。"""
        box = [(90.5, 29.3), (91.7, 29.3), (91.7, 30.2), (90.5, 30.2)]
        box_wkt = f"POLYGON ({_ring_wkt(box)})"
        hits = {row["event_id"] for row in await warehouse.within_polygon(box_wkt)}
        assert hits == {event_id for event_id, point in sample_points if in_ring(point, box)}, "按 (lon, lat) 解释的结果与参照不符"
        assert len(hits) >= 20, f"只有 {len(hits)} 个点落进覆盖全部样本的框里，经纬多半被调换了"

        swapped = "POLYGON ((29.3 90.5, 29.3 91.7, 30.2 91.7, 30.2 90.5, 29.3 90.5))"
        assert await warehouse.within_polygon(swapped) == [], "把 (lat, lon) 当框也能召回 = 字段顺序其实反了"

    async def test_rows_without_coordinates_never_leak_in(self, tmp_path: Path) -> None:
        target = DuckDbWarehouse(tmp_path / "nocoord.duckdb", allow_spatial=True)
        await target.apply_schema()
        if not target.spatial_ready:
            pytest.skip("spatial 扩展不可用")
        try:
            await target.append([_fact("evt-in", (91.10, 29.70)), _fact("evt-in-2", (91.12, 29.72))])
            await target.append([FactRow(event_id="evt-nolat", kind="telemetry", observed_at=BASE, region_code="540100")])
            await target.flush()
            ids = {row["event_id"] for row in await target.within_polygon(polygon_wkt(with_hole=False))}
            assert "evt-nolat" not in ids, "没有坐标的事实被召进了空间结果"
            assert {"evt-in", "evt-in-2"} <= ids
        finally:
            await target.close(500)

    async def test_bad_wkt_surfaces_as_typed_error_without_echoing_the_payload(self, warehouse: DuckDbWarehouse) -> None:
        wkt = "POLYGON ((91 29, 91.1"
        with pytest.raises(WarehouseError) as caught:
            await warehouse.within_polygon(wkt)
        message = str(caught.value)
        assert "91 29, 91.1" not in message, f"整段几何被抄进错误信息（现场多边形可以很长）：{message}"
        assert len(message) <= 220, f"错误信息没收口：{len(message)} 字"
        assert caught.value.detail["geometry_chars"] == len(wkt)
        assert caught.value.detail["driver_error"]

    async def test_single_file_survives_reopen_and_spatial_reloads(
        self, tmp_path: Path, sample_points: list[tuple[str, tuple[float, float]]]
    ) -> None:
        path = tmp_path / "reopen.duckdb"
        first = DuckDbWarehouse(path, allow_spatial=True)
        await first.apply_schema()
        if not first.spatial_ready:
            pytest.skip("spatial 扩展不可用")
        await first.append([_fact(event_id, point) for event_id, point in sample_points])
        await first.flush()
        await first.close(500)

        second = DuckDbWarehouse(path, allow_spatial=True)
        try:
            await second.apply_schema()
            assert second.spatial_ready is True, "重开文件后 spatial 没再加载：下一次判读会静默降级"
            assert await second.count() == len(sample_points)
            rows = {row["event_id"] for row in await second.within_polygon(polygon_wkt(with_hole=True))}
            assert rows == expected_inside(sample_points)
        finally:
            await second.close(500)


class TestSpatialCost:
    async def test_query_cost_is_measured_not_assumed(
        self, warehouse: DuckDbWarehouse, sample_points: list[tuple[str, tuple[float, float]]]
    ) -> None:
        started = time.perf_counter()
        rows = await warehouse.within_polygon(polygon_wkt(with_hole=True))
        elapsed_ms = (time.perf_counter() - started) * 1000
        assert len(rows) >= 1
        print(f"[spatial] 行数={len(sample_points)} 单次点-多边形={elapsed_ms:.1f}ms 命中={len(rows)}")
