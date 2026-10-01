"""DDL 文本与列顺序的对账测试：明细表列名必须与 `FACT_COLUMNS` 一字不差，否则写入静默错位。

这一层没有 I/O，但它守的是**跨端一致性**：Python 纯函数、ClickHouse 物化视图、DuckDB rollup
三处口径必须同源，任何一处漂移都会让"分钟汇总"在对账时撒谎。故这里全部用文本断言，不用容器。
"""

from __future__ import annotations

import pytest

from aegis.analytics.port import FACT_COLUMNS
from aegis.analytics.sql import (
    AGGREGATE_STATES,
    CLICKHOUSE_DDL,
    DATABASE_DDL,
    DEFAULT_DATABASE,
    DUCKDB_EDGE_SETTINGS,
    DUCKDB_FACTS_DDL,
    FACT_COLUMN_DDL,
    FACT_DDL,
    FACT_TABLE,
    MINUTE_KEYS,
    MINUTE_TABLE,
    build_clickhouse_ddl,
    duckdb_minute_select,
    fact_column_names,
    insert_columns,
    minute_select_from_agg,
    minute_select_from_detail,
)

# 明细 DDL 里声明但**不参与 insert** 的服务端物化列。
MATERIALIZED_COLUMNS = ("minute", "measure")


class TestColumnAlignment:
    def test_ddl_column_names_equal_fact_columns(self) -> None:
        """写入列名与行形状必须同源：这里漂移一次，ClickHouse 就会把经纬度写进风险等级。"""
        assert fact_column_names(FACT_COLUMN_DDL) == FACT_COLUMNS

    def test_insert_columns_reuses_port_contract(self) -> None:
        assert insert_columns() is FACT_COLUMNS

    def test_fact_columns_are_unique_and_ordered(self) -> None:
        assert len(FACT_COLUMNS) == len(set(FACT_COLUMNS)) == 16
        assert FACT_COLUMNS[0] == "event_id"
        assert FACT_COLUMNS[-1] == "ingested_at"
        assert FACT_COLUMNS[-2] == "observed_at"

    def test_materialized_columns_are_absent_from_insert_list(self) -> None:
        for name in MATERIALIZED_COLUMNS:
            assert name not in FACT_COLUMNS

    def test_duckdb_ddl_declares_every_fact_column_once(self) -> None:
        body = DUCKDB_FACTS_DDL.split("(", 1)[1].rsplit(")", 1)[0]
        declared = [line.strip().split(maxsplit=1)[0] for line in body.splitlines() if line.strip()]
        assert tuple(declared) == FACT_COLUMNS


class TestClickHouseDdl:
    def test_build_order_is_database_then_detail_then_agg_then_mv(self) -> None:
        statements = build_clickhouse_ddl("x")
        assert len(statements) == 4
        assert statements[0].startswith("CREATE DATABASE")
        assert f"x.{FACT_TABLE}" in statements[1]
        assert f"x.{MINUTE_TABLE}" in statements[2]
        assert "MATERIALIZED VIEW" in statements[3]

    @pytest.mark.parametrize("database", [DEFAULT_DATABASE, "aegis_prod"])
    def test_every_statement_is_idempotent(self, database: str) -> None:
        """全部 IF NOT EXISTS：装配/重启反复执行建表不应失败，这是边缘自治重启的前提。"""
        for statement in build_clickhouse_ddl(database):
            assert "IF NOT EXISTS" in statement

    def test_no_unrendered_placeholders_remain(self) -> None:
        for statement in CLICKHOUSE_DDL:
            for token in ("{database}", "{agg_columns}", "{mv_states}"):
                assert token not in statement

    def test_database_ddl_template_is_the_only_placeholder_form(self) -> None:
        assert DATABASE_DDL.format(database="t") == "CREATE DATABASE IF NOT EXISTS t"

    def test_detail_table_engine_partition_and_ttl(self) -> None:
        assert "ENGINE = MergeTree" in FACT_DDL
        assert "PARTITION BY toYYYYMM(observed_at)" in FACT_DDL
        assert "ORDER BY (minute, region_code)" in FACT_DDL
        assert "INTERVAL 90 DAY DELETE" in FACT_DDL

    def test_minute_keys_lead_the_ordering(self) -> None:
        assert MINUTE_KEYS == ("minute", "region_code", "kind")
        assert f"ORDER BY ({', '.join(MINUTE_KEYS)})" in CLICKHOUSE_DDL[2]

    def test_minute_table_is_aggregating_mergetree(self) -> None:
        assert "ENGINE = AggregatingMergeTree" in CLICKHOUSE_DDL[2]

    def test_mv_writes_into_aggregate_table(self) -> None:
        mv = CLICKHOUSE_DDL[3]
        assert f"TO {DEFAULT_DATABASE}.{MINUTE_TABLE}" in mv
        assert f"FROM {DEFAULT_DATABASE}.{FACT_TABLE}" in mv

    def test_measure_expression_maps_kind_to_column(self) -> None:
        """measure 的 CASE 必须与 `FactRow.measure()` 同口径，否则服务端与 Python 汇总不同值。"""
        fact_ddl = CLICKHOUSE_DDL[1]
        assert "WHEN 'telemetry' THEN value" in fact_ddl
        assert "WHEN 'latency' THEN latency_ms" in fact_ddl
        assert "risk_level::Nullable(Float64)" in fact_ddl
        assert "toStartOfMinute(observed_at)" in fact_ddl

    def test_nullable_value_columns_match_python_optionality(self) -> None:
        fact_ddl = CLICKHOUSE_DDL[1]
        for column in ("value", "risk_level", "latency_ms", "lat", "lon"):
            assert f"{column} Nullable(" in fact_ddl, column


class TestAggregateStateSingleSource:
    def test_agg_columns_and_states_share_names(self) -> None:
        names = [spec.column for spec in AGGREGATE_STATES]
        assert names == ["cnt", "total", "peak", "floor", "measured", "p95"]
        assert len(names) == len(set(names))

    def test_agg_table_declares_every_state_column(self) -> None:
        agg_ddl = CLICKHOUSE_DDL[2]
        for spec in AGGREGATE_STATES:
            assert f"{spec.column} {spec.agg_type}" in agg_ddl, spec.column

    def test_nullable_measure_forces_nullable_state_types(self) -> None:
        """`measure` 是 Nullable(Float64)，除 countState() 外的状态类型必须带 Nullable。

        真服务端会拒绝把 `AggregateFunction(sum, Nullable(Float64))` 写进
        `AggregateFunction(sum, Float64)` 列，替身驱动发现不了这一点。
        """
        for spec in AGGREGATE_STATES:
            if spec.state.startswith("countState()"):
                continue
            assert "Nullable(Float64)" in spec.agg_type, spec.column

    def test_mv_select_aliases_every_state_to_its_column(self) -> None:
        """`TO 目标表` 的 MV 按列名对齐 SELECT 输出：漏 AS 就是 THERE_IS_NO_COLUMN。"""
        mv = CLICKHOUSE_DDL[3]
        for spec in AGGREGATE_STATES:
            assert f"{spec.state} AS {spec.column}" in mv, spec.column

    def test_detail_recompute_reads_final_values_not_states(self) -> None:
        """回算 SQL 必须用普通聚合函数：clickhouse-connect 读不了 AggregateFunction 列。"""
        detail = minute_select_from_detail()
        for spec in AGGREGATE_STATES:
            assert spec.detail in detail, spec.column
            assert spec.state not in detail, spec.column

    def test_detail_and_merge_sides_share_the_null_convention(self) -> None:
        """空测桶两侧都得给 0：一侧 ifNull 一侧不给，对账就会把口径差异读成数据错。"""
        for spec in AGGREGATE_STATES:
            assert spec.merge.startswith("ifNull") == spec.detail.startswith("ifNull"), spec.column

    def test_mv_select_uses_state_functions(self) -> None:
        mv = CLICKHOUSE_DDL[3]
        for spec in AGGREGATE_STATES:
            assert spec.state in mv, spec.state

    def test_detail_recompute_and_agg_read_are_dual_path(self) -> None:
        """回填/对账走明细回算，查询走聚合读：两条 SQL 都必须在，且分组键一致。"""
        detail = minute_select_from_detail()
        agg = minute_select_from_agg()
        for expr in (detail, agg):
            assert expr.count(", ".join(MINUTE_KEYS)) >= 1
        assert "quantile(0.95)(measure)" in detail
        assert "quantileMerge(0.95)(p95)" in agg

    def test_custom_database_propagates(self) -> None:
        detail = minute_select_from_detail("db2")
        assert "db2.analytics_fact" in detail
        assert "db2.fact_minute_agg" in minute_select_from_agg("db2")


class TestDuckDbSide:
    def test_edge_settings_pin_utc_memory_threads(self) -> None:
        joined = "\n".join(DUCKDB_EDGE_SETTINGS)
        assert "SET TimeZone = 'UTC'" in joined
        assert "{memory_limit}" in joined and "{threads}" in joined
        assert "preserve_insertion_order = false" in joined

    def test_minute_select_matches_clickhouse_keys(self) -> None:
        sql = duckdb_minute_select()
        assert "date_trunc('minute', observed_at)" in sql
        for column in ("cnt", "total", "peak", "floor", "measured"):
            assert f"AS {column}" in sql, column
        assert "GROUP BY minute, region_code, kind" in sql
        assert "ORDER BY minute, region_code, kind" in sql

    def test_minute_select_is_injectable_with_table_name(self) -> None:
        assert "FROM other_table" in duckdb_minute_select("other_table")

    def test_duckdb_measure_case_matches_python_semantics(self) -> None:
        sql = duckdb_minute_select()
        assert "WHEN 'telemetry' THEN value" in sql
        assert "WHEN 'latency' THEN latency_ms" in sql
        assert "risk_level::DOUBLE" in sql

    def test_duckdb_primary_key_is_event_id(self) -> None:
        """幂等去重在 DDL 层兜住：重复上报同一条读数只落一次。"""
        assert "event_id VARCHAR PRIMARY KEY" in DUCKDB_FACTS_DDL
