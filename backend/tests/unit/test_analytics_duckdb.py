"""DuckDB 边缘仓测试：单文件离线可用、写入幂等、bbox/半径查询、Parquet 交接，以及 rollup 与纯函数同源。

这里跑的是**真 DuckDB**（tmp_path 下的单文件），因为边缘侧的全部价值就是"断网也能算"：
mock 掉驱动等于把这个模块的存在理由测了个寂寞。spatial 扩展可能因无网而装不上，
所以点-多边形只验证"能力位与错误口径一致"，不把网络可达性当测试前提。
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from aegis.analytics.duckdb_warehouse import DuckDbWarehouse
from aegis.analytics.port import (
    AnalyticsSink,
    FactRow,
    SpatialUnavailableError,
    WarehouseError,
    materialize_minutes,
)

BASE = datetime(2026, 9, 30, 3, 0, tzinfo=UTC)
LHASA = (91.12, 29.65)  # (lon, lat)


def fact(
    index: int,
    *,
    kind: str = "telemetry",
    value: float | None = 1.0,
    observed: datetime | None = None,
    region: str = "540121",
    lat: float | None = None,
    lon: float | None = None,
    risk: int | None = None,
) -> FactRow:
    return FactRow(
        event_id=f"evt-{index}",
        kind=kind,  # type: ignore[arg-type]
        observed_at=observed or BASE,
        region_code=region,
        station_id=f"ST-{index}",
        metric="rainfall_mm" if kind == "telemetry" else "sync_agent_to_gateway_ms",
        value=value,
        risk_level=risk,
        latency_ms=value if kind == "latency" else None,
        lat=lat,
        lon=lon,
    )


@pytest.fixture
def warehouse(tmp_path: Path) -> Any:
    instance = DuckDbWarehouse(tmp_path / "edge.duckdb", allow_spatial=False)
    yield instance
    asyncio.run(instance.close(0))


def run(coro: Any) -> Any:
    return asyncio.run(coro)


# --------------------------------------------------------------------- 构造与打开


class TestOpen:
    def test_threads_must_be_at_least_one(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="threads"):
            DuckDbWarehouse(tmp_path / "x.duckdb", threads=0)

    def test_path_is_created_with_parents(self, tmp_path: Path) -> None:
        target = tmp_path / "nested" / "deeper" / "edge.duckdb"
        instance = DuckDbWarehouse(target, allow_spatial=False)
        run(instance.apply_schema())
        assert target.exists()

    def test_edge_settings_are_applied(self, tmp_path: Path) -> None:
        """小内存边缘机：DuckDB 必须按下发值限住，否则会与采集/推理抢内存。"""
        instance = DuckDbWarehouse(tmp_path / "lim.duckdb", memory_limit="48MB", threads=1, allow_spatial=False)
        stats = instance.stats()
        assert stats["memory_limit"] == "48MB"
        assert stats["threads"] == 1
        assert stats["backend"] == "duckdb"

    def test_schema_is_idempotent(self, warehouse: DuckDbWarehouse) -> None:
        run(warehouse.apply_schema())
        run(warehouse.apply_schema())
        assert run(warehouse.count()) == 0

    def test_implements_the_narrow_port(self, warehouse: DuckDbWarehouse) -> None:
        assert isinstance(warehouse, AnalyticsSink)


# --------------------------------------------------------------------- 写入与幂等


class TestWrites:
    def test_append_then_count(self, warehouse: DuckDbWarehouse) -> None:
        assert run(warehouse.append([fact(1), fact(2)])) == 2
        assert run(warehouse.count()) == 2

    def test_empty_append_is_a_noop(self, warehouse: DuckDbWarehouse) -> None:
        assert run(warehouse.append([])) == 0
        assert run(warehouse.count()) == 0

    def test_duplicate_event_ids_land_once(self, warehouse: DuckDbWarehouse) -> None:
        """弱网重放的同一条读数只能落一次（PRIMARY KEY + INSERT OR IGNORE）。"""
        run(warehouse.append([fact(1)]))
        run(warehouse.append([fact(1)]))
        assert run(warehouse.count()) == 1

    def test_submitted_counter_is_not_the_landed_count(self, warehouse: DuckDbWarehouse) -> None:
        """stats 里叫 submitted：把"提交过"说成"写入了"会在重放场景下谎报数据量。"""
        run(warehouse.append([fact(1), fact(1), fact(2)]))
        assert warehouse.stats()["submitted"] == 3
        assert run(warehouse.count()) == 2

    def test_ingest_then_flush_routes_through_the_batcher(self, warehouse: DuckDbWarehouse) -> None:
        async def drive() -> None:
            accepted = await warehouse.ingest([fact(i) for i in range(1, 6)])
            assert accepted == 5
            assert warehouse.stats()["buffered"] == 5
            assert await warehouse.count() == 0
            await warehouse.flush()

        run(drive())
        assert run(warehouse.count()) == 5
        assert warehouse.stats()["inserted"] == 5

    def test_non_finite_values_are_stored_as_null_not_poisoned(self, warehouse: DuckDbWarehouse) -> None:
        run(warehouse.append([fact(1, value=float("nan"), lat=29.6, lon=91.1), fact(2, value=5.0, lat=29.6, lon=91.1)]))
        rows = run(warehouse.bbox_query(min_lon=-180, max_lon=180, min_lat=-90, max_lat=90))
        values = {row["value"] for row in rows}
        assert None in values
        assert 5.0 in values
        assert not any(isinstance(v, float) and math.isnan(v) for v in values if v is not None)


# --------------------------------------------------------------------- 空间查询


class TestScalarSpatial:
    """bbox / 半径只用标量 lat/lon：无扩展、无网络也能算，这是边缘侧的默认能力。"""

    def test_bbox_filters_by_inclusive_bounds(self, warehouse: DuckDbWarehouse) -> None:
        rows = [
            fact(1, lat=29.60, lon=91.10),
            fact(2, lat=29.70, lon=91.20),
            fact(3, lat=30.10, lon=92.00),
        ]
        run(warehouse.append(rows))
        inside = run(warehouse.bbox_query(min_lon=91.0, max_lon=91.3, min_lat=29.5, max_lat=29.7))
        assert {row["event_id"] for row in inside} == {"evt-1", "evt-2"}

    def test_bbox_requires_both_coordinates(self, warehouse: DuckDbWarehouse) -> None:
        run(warehouse.append([fact(1, lat=29.6), fact(2, lat=29.6, lon=91.1)]))
        rows = run(warehouse.bbox_query(min_lon=90.0, max_lon=92.0, min_lat=29.0, max_lat=30.0))
        assert [row["event_id"] for row in rows] == ["evt-2"]

    def test_bbox_kind_region_since_and_limit(self, warehouse: DuckDbWarehouse) -> None:
        run(
            warehouse.append(
                [
                    fact(1, lat=29.6, lon=91.1, region="540121"),
                    fact(2, lat=29.6, lon=91.1, region="540122"),
                    fact(3, kind="hazard", risk=2, lat=29.6, lon=91.1, region="540121"),
                ]
            )
        )
        box = {"min_lon": 91.0, "max_lon": 91.2, "min_lat": 29.5, "max_lat": 29.7}
        assert len(run(warehouse.bbox_query(**box))) == 3  # type: ignore[arg-type]
        assert len(run(warehouse.bbox_query(**box, region_code="540121"))) == 2  # type: ignore[arg-type]
        assert len(run(warehouse.bbox_query(**box, kind="hazard"))) == 1  # type: ignore[arg-type]
        assert len(run(warehouse.bbox_query(**box, since=BASE + timedelta(minutes=5)))) == 0  # type: ignore[arg-type]
        assert len(run(warehouse.bbox_query(**box, since=BASE))) == 3  # type: ignore[arg-type]
        assert len(run(warehouse.bbox_query(**box, limit=1))) == 1  # type: ignore[arg-type]

    def test_bbox_limit_must_be_positive(self, warehouse: DuckDbWarehouse) -> None:
        with pytest.raises(ValueError, match="limit"):
            run(warehouse.bbox_query(min_lon=0, max_lon=1, min_lat=0, max_lat=1, limit=0))

    def test_radius_orders_by_distance_and_cuts_at_the_edge(self, warehouse: DuckDbWarehouse) -> None:
        run(
            warehouse.append(
                [
                    fact(1, lat=LHASA[1] + 0.01, lon=LHASA[0]),
                    fact(2, lat=LHASA[1], lon=LHASA[0]),
                    fact(3, lat=LHASA[1] + 0.5, lon=LHASA[0]),
                    fact(4),  # 无经纬度：不参与半径检索
                ]
            )
        )
        rows = run(warehouse.radius_query(LHASA[0], LHASA[1], 3_000.0))
        assert [row["event_id"] for row in rows] == ["evt-2", "evt-1"]
        assert rows[0]["distance_m"] == pytest.approx(0.0, abs=1e-6)

    def test_radius_matches_an_independent_haversine(self, warehouse: DuckDbWarehouse) -> None:
        """口径自证的唯一办法：与测试里独立实现的大圆距离对齐（1% 内）。"""
        far_lat, far_lon = LHASA[1] + 0.25, LHASA[0] + 0.25
        run(warehouse.append([fact(1, lat=far_lat, lon=far_lon)]))
        rows = run(warehouse.radius_query(LHASA[0], LHASA[1], 100_000.0))
        expected = _haversine_m(LHASA[1], LHASA[0], far_lat, far_lon)
        assert rows[0]["distance_m"] == pytest.approx(expected, rel=0.01)

    def test_radius_negative_is_rejected(self, warehouse: DuckDbWarehouse) -> None:
        with pytest.raises(ValueError, match="radius_m"):
            run(warehouse.radius_query(LHASA[0], LHASA[1], -1.0))

    def test_radius_zero_keeps_only_the_exact_point(self, warehouse: DuckDbWarehouse) -> None:
        run(warehouse.append([fact(1, lat=LHASA[1], lon=LHASA[0]), fact(2, lat=LHASA[1] + 0.01, lon=LHASA[0])]))
        assert [row["event_id"] for row in run(warehouse.radius_query(LHASA[0], LHASA[1], 0.0))] == ["evt-1"]


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6_371_008.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return r * 2 * math.asin(math.sqrt(a))


# --------------------------------------------------------------------- spatial 能力位


class TestSpatialCapability:
    def test_disabled_spatial_fails_explicitly(self, warehouse: DuckDbWarehouse) -> None:
        assert warehouse.spatial_ready is False
        with pytest.raises(SpatialUnavailableError):
            run(warehouse.within_polygon("POLYGON((91 29, 92 29, 92 30, 91 30, 91 29))"))

    def test_core_queries_survive_without_spatial(self, warehouse: DuckDbWarehouse) -> None:
        """扩展装不上是常态（离线机），但 bbox/半径/汇总必须照常——这是把 spatial 设为可选的理由。"""
        run(warehouse.append([fact(1, lat=LHASA[1], lon=LHASA[0])]))
        assert len(run(warehouse.bbox_query(min_lon=91.0, max_lon=91.2, min_lat=29.5, max_lat=29.7))) == 1
        assert len(run(warehouse.radius_query(LHASA[0], LHASA[1], 1_000.0))) == 1
        assert len(run(warehouse.minute_rollup())) == 1

    def test_stats_exposes_the_reason_when_unavailable(self, warehouse: DuckDbWarehouse) -> None:
        assert warehouse.stats()["spatial_ready"] is False

    def test_spatial_flag_and_query_behaviour_agree(self, tmp_path: Path) -> None:
        """允许尝试加载扩展的真实环境：能力位必须与查询实际行为一致，不能各说各话。"""
        instance = DuckDbWarehouse(tmp_path / "sp.duckdb", allow_spatial=True)

        async def drive() -> str:
            await instance.apply_schema()
            ready = instance.spatial_ready
            try:
                await instance.within_polygon("POLYGON((91 29, 92 29, 92 30, 91 30, 91 29))")
            except SpatialUnavailableError:
                return f"raised:{ready}"
            return f"ok:{ready}"

        outcome = run(drive())
        run(instance.close(0))
        assert outcome in ("raised:False", "ok:True"), outcome


# --------------------------------------------------------------------- 分钟 rollup 与纯函数同源


class TestRollupConsistency:
    def test_rollup_matches_the_pure_function(self, warehouse: DuckDbWarehouse) -> None:
        """跨端对账的前提：DuckDB SQL 物化与 Python 纯函数在同一批行上必须给出同一批分钟行。"""
        rows = [
            fact(1, value=2.0, observed=BASE, region="540121"),
            fact(2, value=4.0, observed=BASE + timedelta(seconds=20), region="540121"),
            fact(3, value=8.0, observed=BASE + timedelta(seconds=59), region="540121"),
            fact(4, value=1.0, observed=BASE + timedelta(minutes=1), region="540121"),
            fact(5, value=3.0, observed=BASE, region="540122"),
        ]
        run(warehouse.append(rows))
        sql_facts = run(warehouse.minute_rollup())
        py_facts = materialize_minutes(rows)

        assert [f.key for f in sql_facts] == [f.key for f in py_facts]
        for sql_fact, py_fact in zip(sql_facts, py_facts, strict=True):
            assert sql_fact.count == py_fact.count
            assert sql_fact.total == pytest.approx(py_fact.total)
            assert sql_fact.peak == pytest.approx(py_fact.peak)
            assert sql_fact.floor == pytest.approx(py_fact.floor)
            assert sql_fact.mean == pytest.approx(py_fact.mean)
            assert sql_fact.skipped_missing == py_fact.skipped_missing

    def test_rollup_declares_p95_as_not_produced_on_the_edge(self, warehouse: DuckDbWarehouse) -> None:
        """诚实边界：边缘 SQL 汇总不算分位数，p95 恒为 0 而非"看起来像 0"的假数据。"""
        run(warehouse.append([fact(i, value=float(i)) for i in range(1, 4)]))
        (rollup,) = run(warehouse.minute_rollup())
        assert rollup.p95 == 0.0
        assert rollup.worst_risk_level is None

    def test_rollup_filters(self, warehouse: DuckDbWarehouse) -> None:
        run(
            warehouse.append(
                [
                    fact(1, value=1.0, region="540121"),
                    fact(2, value=2.0, region="540122"),
                    fact(3, kind="latency", value=9.0, region="540121"),
                ]
            )
        )
        assert len(run(warehouse.minute_rollup())) == 3
        assert len(run(warehouse.minute_rollup(region_code="540121"))) == 2
        assert len(run(warehouse.minute_rollup(kind="latency"))) == 1
        assert len(run(warehouse.minute_rollup(since=BASE + timedelta(minutes=1)))) == 0

    def test_missing_measurements_are_counted_as_skipped(self, warehouse: DuckDbWarehouse) -> None:
        run(warehouse.append([fact(1, value=2.0), fact(2, value=None)]))
        (rollup,) = run(warehouse.minute_rollup())
        assert rollup.count == 2
        assert rollup.skipped_missing == 1
        assert rollup.measured == 1
        assert rollup.total == pytest.approx(2.0)


# --------------------------------------------------------------------- Parquet 交接


class TestParquetHandover:
    def test_export_then_read_back(self, warehouse: DuckDbWarehouse, tmp_path: Path) -> None:
        """复网交接：单文件仓导出 Parquet，平台侧按同一份文件对账条数。"""
        run(warehouse.append([fact(i, value=float(i)) for i in range(1, 6)]))
        dest = tmp_path / "outbox" / "handover.parquet"
        exported = run(warehouse.export_parquet(dest))
        assert exported == 5
        assert dest.exists()
        assert run(warehouse.read_parquet_count(dest)) == 5

    def test_export_of_an_empty_warehouse_produces_an_empty_file(self, warehouse: DuckDbWarehouse, tmp_path: Path) -> None:
        dest = tmp_path / "empty.parquet"
        assert run(warehouse.export_parquet(dest)) == 0
        assert run(warehouse.read_parquet_count(dest)) == 0

    def test_path_with_a_single_quote_is_escaped_not_injected(self, warehouse: DuckDbWarehouse, tmp_path: Path) -> None:
        """COPY 的目标只能拼字符串字面量：引号必须转义，否则文件名里一个单引号就是 SQL 注入面。"""
        dest = tmp_path / "we'o'd.parquet"
        run(warehouse.append([fact(1)]))
        assert run(warehouse.export_parquet(dest)) == 1
        assert run(warehouse.read_parquet_count(dest)) == 1


# --------------------------------------------------------------------- 关停与线程模型


class TestCloseAndThreading:
    def test_close_is_idempotent(self, tmp_path: Path) -> None:
        instance = DuckDbWarehouse(tmp_path / "c.duckdb", allow_spatial=False)

        async def drive() -> None:
            await instance.append([fact(1)])
            await instance.close(1_000)
            await instance.close(1_000)

        run(drive())
        assert instance.stats()["closed"] is True

    def test_queries_after_close_raise_a_typed_error(self, tmp_path: Path) -> None:
        instance = DuckDbWarehouse(tmp_path / "q.duckdb", allow_spatial=False)
        run(instance.append([fact(1)]))
        run(instance.close(0))
        with pytest.raises(WarehouseError, match="已关闭"):
            run(instance.count())

    def test_append_after_close_raises(self, tmp_path: Path) -> None:
        instance = DuckDbWarehouse(tmp_path / "a.duckdb", allow_spatial=False)
        run(instance.close(0))
        with pytest.raises(WarehouseError):
            run(instance.append([fact(1)]))

    def test_close_with_grace_flushes_buffered_rows_to_the_file(self, tmp_path: Path) -> None:
        """回归：关停原先先置 `_closed` 再排空，而 `_run` 以此字为闸，导致"优雅退出"必丢整批缓冲。

        这里不仅看计数，还把文件重新打开查一次行数——优雅退出的真正承诺是数据落进了单文件。
        """
        target = tmp_path / "g.duckdb"
        instance = DuckDbWarehouse(target, allow_spatial=False)

        async def drive() -> tuple[int, int]:
            await instance.ingest([fact(i) for i in range(1, 4)])
            await instance.close(2_000)
            stats = instance.stats()
            return stats["inserted"], stats["dropped_closed"]

        inserted, dropped = run(drive())
        assert inserted == 3
        assert dropped == 0

        reopened = DuckDbWarehouse(target, allow_spatial=False)
        assert run(reopened.count()) == 3
        run(reopened.close(0))

    def test_close_with_zero_grace_accounts_unflushed_rows(self, tmp_path: Path) -> None:
        instance = DuckDbWarehouse(tmp_path / "z.duckdb", allow_spatial=False)

        async def drive() -> dict[str, Any]:
            await instance.ingest([fact(i) for i in range(1, 4)])
            await instance.close(0)
            return instance.stats()

        stats = run(drive())
        assert stats["dropped_closed"] == 3
        assert stats["buffered"] == 0

    def test_all_database_work_happens_on_the_worker_thread(self, tmp_path: Path) -> None:
        """DuckDB 连接不得跨线程共享：所有阻塞调用必须收敛到那条单线程执行器。"""
        instance = DuckDbWarehouse(tmp_path / "t.duckdb", allow_spatial=False)
        main = threading.get_ident()
        ticks = 0

        async def beat() -> None:
            nonlocal ticks
            while True:
                await asyncio.sleep(0.005)
                ticks += 1

        async def drive() -> int:
            pacer = asyncio.create_task(beat())
            try:
                await instance.append([fact(i, value=float(i)) for i in range(1, 50)])
                await instance.count()
                await instance.minute_rollup()
            finally:
                pacer.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await pacer
            return instance._owner_thread or 0

        owner = run(drive())
        assert owner != main
        assert ticks >= 1
        run(instance.close(0))


# --------------------------------------------------------------------- 驱动 API 契约


class TestDriverApiContract:
    """把"我们依赖的驱动签名"钉成测试：升级 duckdb 时先在这里红，而不是在边缘机上红。

    本用例是真实运行暴露出来的回归——duckdb 的连接没有 `transaction()` 上下文管理器，
    只有 begin/commit/rollback，mock 掉驱动的测试永远发现不了。
    """

    def test_transaction_primitives_exist_on_the_connection(self) -> None:
        import duckdb

        connection = duckdb.connect(":memory:")
        try:
            for name in ("begin", "commit", "rollback", "executemany", "execute", "close"):
                assert callable(getattr(connection, name, None)), name
            assert not hasattr(connection, "transaction"), "duckdb 若新增了 transaction()，应改用官方上下文管理器"
        finally:
            connection.close()

    def test_insert_or_ignore_is_supported(self, tmp_path: Path) -> None:
        """`INSERT OR IGNORE` 是幂等去重的落地手段，方言不支持就等于重复上报会写脏数据。"""
        import duckdb

        connection = duckdb.connect(str(tmp_path / "dialect.duckdb"))
        try:
            connection.execute("CREATE TABLE t (id VARCHAR PRIMARY KEY)")
            connection.execute("INSERT OR IGNORE INTO t VALUES ('a')")
            connection.execute("INSERT OR IGNORE INTO t VALUES ('a')")
            assert int(connection.execute("SELECT count(*) FROM t").fetchone()[0]) == 1
        finally:
            connection.close()


# --------------------------------------------------------------------- 断网可恢复


class TestOfflinePersistence:
    def test_file_survives_reopen(self, tmp_path: Path) -> None:
        """边缘自治的落点：进程重启（或断网期间重启）后，同一个文件里的数据仍可查询。"""
        target = tmp_path / "persist.duckdb"
        first = DuckDbWarehouse(target, allow_spatial=False)
        run(first.append([fact(i, value=float(i), observed=BASE) for i in range(1, 6)]))
        run(first.close(1_000))

        second = DuckDbWarehouse(target, allow_spatial=False)
        assert run(second.count()) == 5
        rollup = run(second.minute_rollup())
        assert rollup[0].count == 5
        assert rollup[0].total == pytest.approx(15.0)
        run(second.close(0))


class TestGeometryErrorContract:
    """几何写错时的收口口径：驱动异常必须变成本模块的定型错误，且不把整段几何抄进日志。

    真点-多边形判定在 `tests/integration/test_analytics_duckdb_spatial_live.py` 里跑；
    这里只钉错误映射，所以故意不依赖 spatial（手动置能力位 + 注入必抛的驱动），
    离线装不上扩展的机器同样要有这条保障。
    """

    def test_driver_parse_error_becomes_typed_error(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        wkt = "POLYGON ((91 29, 91.1"
        instance = DuckDbWarehouse(tmp_path / "geo.duckdb", allow_spatial=False)

        class _Boom:
            def execute(self, *_args: Any, **_kwargs: Any) -> Any:
                raise RuntimeError(f"Invalid Input Error: Expected number at position '21' near: '{wkt}'")

        run(instance.apply_schema())
        monkeypatch.setattr(instance, "_spatial_ready", True)
        monkeypatch.setattr(instance, "_con", _Boom())
        with pytest.raises(WarehouseError) as caught:
            run(instance.within_polygon(wkt))
        message = str(caught.value)
        assert wkt not in message, f"整段几何被抄进错误信息：{message}"
        assert len(message) <= 220
        assert caught.value.detail["geometry_chars"] == len(wkt)
        assert caught.value.retryable is False
        run(instance.close(0))
