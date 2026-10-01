"""真实 ClickHouse 的在线验证（默认跳过）。

起服务（新版镜像的 default 用户强制口令，与生产同形）：
    docker run -d --name aegis-ch -p 8123:8123 -e CLICKHOUSE_PASSWORD=<pwd> \\
        clickhouse/clickhouse-server:latest
然后：
    AEGIS_TEST_CLICKHOUSE_HOST=127.0.0.1 AEGIS_TEST_CLICKHOUSE_PASSWORD=<pwd> \\
        pytest tests/integration/test_analytics_clickhouse_live.py

这里只验"只有真库才能证明的东西"，替身驱动做不到：
1. 建表 DDL 能否在真服务端执行（含 `MATERIALIZED` 列与物化视图的 TO 目标表）；
2. 物化视图写入的聚合状态，读时 `-Merge` 是否给出与明细回算**完全一致**的分钟值；
3. 明细上的服务端聚合是否等于平台 Python 参考实现 `materialize_minutes`（两套独立实现互为对账）；
4. 列式批量 insert 的列序是否与 DDL 对齐（错一列不会报错，只会静默写错数据）。
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from aegis.analytics.clickhouse_sink import ClickHouseSink, connect_clickhouse
from aegis.analytics.port import FactRow, materialize_minutes
from aegis.analytics.sql import MINUTE_MV, minute_select_from_agg, minute_select_from_detail

HOST = os.getenv("AEGIS_TEST_CLICKHOUSE_HOST", "")
PORT = int(os.getenv("AEGIS_TEST_CLICKHOUSE_PORT", "8123") or "8123")
PASSWORD = os.getenv("AEGIS_TEST_CLICKHOUSE_PASSWORD", "")
DATABASE = os.getenv("AEGIS_TEST_CLICKHOUSE_DATABASE", "aegis_itest")

pytestmark = pytest.mark.skipif(not HOST, reason="未设置 AEGIS_TEST_CLICKHOUSE_HOST，跳过真实 ClickHouse 测试")

# 固定在一个整分钟附近，避免跨分钟边界让对账变成时间竞赛
BASE = datetime(2026, 10, 1, 3, 20, 0, tzinfo=UTC)


def row(index: int, *, region: str, kind: str, minute_offset: int = 0, value: float | None = None, risk: int | None = None) -> FactRow:
    return FactRow(
        event_id=f"evt_{region}_{kind}_{index}",
        kind=kind,  # type: ignore[arg-type]
        observed_at=BASE + timedelta(minutes=minute_offset, seconds=index),
        region_code=region,
        station_id=f"ST-{region}",
        metric="rain_10min" if kind == "telemetry" else "",
        hazard_type="debris_flow" if kind == "hazard" else "",
        unit="mm" if kind == "telemetry" else "",
        value=value,
        risk_level=risk,
        latency_ms=float(40 + index) if kind == "latency" else None,
        trace_id=f"trc_{index:016d}",
    )


@pytest.fixture
async def client() -> AsyncIterator[Any]:
    """连到服务器而不是测试库：测试库由 DDL 自己建（`connect` 时绑定不存在的库会直接握手失败）。

    这也是 sink 的真实用法——所有语句都带 `{database}.` 前缀，连接会话不依赖默认库。
    """
    conn = connect_clickhouse(host=HOST, port=PORT, database="default", password=PASSWORD)
    try:
        yield conn
    finally:
        await asyncio.to_thread(conn.command, f"DROP DATABASE IF EXISTS {DATABASE}")
        conn.close()


async def query(client: Any, sql: str) -> list[tuple[Any, ...]]:
    result = await asyncio.to_thread(client.query, sql)
    return [tuple(row_) for row_ in result.result_rows]


def facts() -> list[FactRow]:
    """两个区域、三种事实、含一条缺测读数：覆盖 measure 的三条 CASE 分支与 NULL 跳过。"""
    rows = [
        row(1, region="540121", kind="telemetry", value=10.0),
        row(2, region="540121", kind="telemetry", value=20.0),
        row(3, region="540121", kind="telemetry", value=None),
        row(4, region="540221", kind="telemetry", minute_offset=1, value=7.5),
        row(5, region="540121", kind="hazard", risk=1),
        row(6, region="540121", kind="hazard", risk=3),
        row(7, region="540221", kind="latency", minute_offset=1),
    ]
    return rows


class TestSchemaOnRealServer:
    async def test_ddl_creates_detail_minute_and_view(self, client: Any) -> None:
        sink = ClickHouseSink(client, database=DATABASE)
        applied = await sink.apply_schema()

        assert len(applied) == 4, "建库 + 明细 + 聚合 + 物化视图，四条都必须在真服务端执行过"
        names = {str(r[0]) for r in await query(client, f"SHOW TABLES FROM {DATABASE}")}
        assert {MINUTE_MV, "analytics_fact", "fact_minute_agg"} <= names

    async def test_ddl_is_idempotent(self, client: Any) -> None:
        """重复 apply 是重启路径上的常态：IF NOT EXISTS 不成立就会把启动打断。"""
        sink = ClickHouseSink(client, database=DATABASE)
        await sink.apply_schema()
        await sink.apply_schema()

        count = await query(client, f"SELECT count() FROM system.tables WHERE database = '{DATABASE}'")
        assert int(count[0][0]) == 3

    async def test_minute_and_measure_are_server_computed_not_inserted(self, client: Any) -> None:
        sink = ClickHouseSink(client, database=DATABASE)
        await sink.apply_schema()
        await sink.ingest(facts())
        await sink.flush()

        minutes = await query(client, f"SELECT DISTINCT minute FROM {DATABASE}.analytics_fact ORDER BY minute")
        # 服务端 DateTime 不带 tzinfo（列定义里已声明 'UTC'）：比较前统一挂回 UTC
        assert [m[0].replace(tzinfo=UTC) for m in minutes] == [BASE, BASE + timedelta(minutes=1)]
        # 写入列里没有 minute/measure：它们必须由服务端算出来
        columns = sink.columns
        assert "minute" not in columns and "measure" not in columns


class TestMinuteMaterializationAgreesWithOracle:
    @staticmethod
    async def loaded_sink(client: Any) -> ClickHouseSink:
        sink = ClickHouseSink(client, database=DATABASE, max_batch=3)
        await sink.apply_schema()
        await sink.ingest(facts())
        await sink.flush()
        return sink

    async def test_view_matches_detail_recomputation(self, client: Any) -> None:
        """物化视图与"从明细现算"必须逐键逐值相同：不同就说明 MV 的 GROUP BY 或状态列写歪了。"""
        await (await self.loaded_sink(client)).close(500)

        from_agg = {r[:3]: r[3:] for r in await query(client, minute_select_from_agg(DATABASE))}
        from_detail = {r[:3]: r[3:] for r in await query(client, minute_select_from_detail(DATABASE))}

        assert from_agg, "MV 必须有内容"
        assert set(from_agg) == set(from_detail)
        for key, values in from_agg.items():
            assert tuple(round(float(v), 6) if isinstance(v, float) else v for v in values) == tuple(
                round(float(v), 6) if isinstance(v, float) else v for v in from_detail[key]
            ), key

    async def test_server_aggregates_match_the_python_reference_implementation(self, client: Any) -> None:
        """两套独立实现对账：服务端 AggregateFunction 与平台纯 Python 的分钟物化必须给同一个答案。

        p95 不在比对之列：分位数的插值口径由服务端与 `percentile()` 各自定义，
        比它只会把"实现差异"误报成"数据错了"。
        """
        await (await self.loaded_sink(client)).close(500)
        expected = {f.key: f for f in materialize_minutes(facts())}

        rows = await query(client, minute_select_from_agg(DATABASE))
        assert len(rows) == len(expected), "分钟键集合必须一一对应"

        for minute, region, kind, cnt, total, peak, floor, measured, _p95 in rows:
            fact = expected[(minute.replace(tzinfo=UTC), region, kind)]
            assert int(cnt) == fact.count, (region, kind, "count")
            assert int(measured) == fact.measured, (region, kind, "measured")
            assert round(float(total), 6) == round(fact.total, 6), (region, kind, "total")
            assert round(float(peak), 6) == round(fact.peak, 6), (region, kind, "peak")
            assert round(float(floor), 6) == round(fact.floor, 6), (region, kind, "floor")

    async def test_missing_reading_does_not_dilute_the_average(self, client: Any) -> None:
        """缺测读数进 count 但不进 measured：均值若把它当 0，total/measured 就会偏低。

        聚合表存的是 AggregateFunction 状态，直接 SELECT 列客户端读不了（deserialization
        not supported），必须走 -Merge 出口——这本身也是一条只有真库能教的口径。
        """
        await (await self.loaded_sink(client)).close(500)

        rows = await query(
            client,
            minute_select_from_agg(DATABASE) + " HAVING region_code = '540121' AND kind = 'telemetry'",
        )
        assert len(rows) == 1
        _minute, _region, _kind, cnt, total, _peak, _floor, measured, _p95 = rows[0]
        assert int(cnt) == 3 and int(measured) == 2
        assert float(total) == pytest.approx(30.0)


class TestColumnAlignment:
    async def test_column_oriented_insert_lands_in_the_right_columns(self, client: Any) -> None:
        """列式插入错位不会报错、只会静默写歪，因此逐列点验。"""
        sink = ClickHouseSink(client, database=DATABASE)
        await sink.apply_schema()
        await sink.ingest([row(1, region="540121", kind="telemetry", value=12.5)])
        await sink.flush()
        await sink.close(500)

        got = await query(
            client,
            f"SELECT event_id, kind, region_code, station_id, metric, unit, quality_flag, value, trace_id FROM {DATABASE}.analytics_fact",
        )
        assert got == [
            ("evt_540121_telemetry_1", "telemetry", "540121", "ST-540121", "rain_10min", "mm", "ok", 12.5, "trc_0000000000000001")
        ]

    async def test_stats_report_submitted_rows_not_written_claim(self, client: Any) -> None:
        """台账里 submitted/inserted 必须可核对：把"受理"说成"已落库"是指标注水的开始。"""
        sink = ClickHouseSink(client, database=DATABASE)
        await sink.apply_schema()
        accepted = await sink.ingest(facts())
        await sink.flush()

        stats = sink.stats()
        assert accepted == len(facts())
        assert int(str(stats["inserted"])) == accepted, stats
        await sink.close(500)
