"""DDL 常量：ClickHouse 服务侧分钟物化（明细 MergeTree + 聚合表 + 物化视图）与 DuckDB 边缘表。

这里只放"文本"（可 import、可单测、零 I/O），运行时由 `clickhouse_sink` / `duckdb_warehouse`
放进工作线程执行。聚合口径的**唯一真源是下面的 `AGGREGATE_STATES`**：聚合表的列定义、物化视图
的 SELECT、以及从明细/聚合表回算的查询都从它渲染，改口径不会出现"两份实现漂移"。

`insert_columns()` 直接复用 `port.FACT_COLUMNS`（列顺序即写入顺序），并由单测断言
"明细 DDL 里的列名集合 == FACT_COLUMNS"，杜绝 DDL 与行形状漂移。
"""

from __future__ import annotations

from dataclasses import dataclass

from aegis.analytics.port import FACT_COLUMNS

DEFAULT_DATABASE = "aegis"
FACT_TABLE = "analytics_fact"
MINUTE_TABLE = "fact_minute_agg"
MINUTE_MV = "fact_minute_mv"

# 明细事实表的物化列（服务端计算，不参与 insert）：
#   minute  —— observed_at 截断到分钟，是 ORDER BY 的第一主键；
#   measure —— 按 kind 选出参与聚合的数值量（遥测 value / 时延 latency_ms / 灾害 risk_level）。
_FACT_MATERIALIZED = """    minute DateTime MATERIALIZED toStartOfMinute(observed_at),
    measure Nullable(Float64) MATERIALIZED CASE kind
        WHEN 'telemetry' THEN value
        WHEN 'latency' THEN latency_ms
        ELSE risk_level::Nullable(Float64)
    END"""

_FACT_COLUMN_DDL = """    event_id String,
    kind LowCardinality(String),
    region_code LowCardinality(String),
    station_id LowCardinality(String),
    metric LowCardinality(String),
    hazard_type LowCardinality(String),
    unit LowCardinality(String),
    quality_flag LowCardinality(String),
    value Nullable(Float64),
    risk_level Nullable(Int8),
    latency_ms Nullable(Float64),
    trace_id String,
    lat Nullable(Float64),
    lon Nullable(Float64),
    observed_at DateTime64(3, 'UTC'),
    ingested_at DateTime64(3, 'UTC'),"""

# 明细表的可写入列块（16 列，逐行一名）：`fact_column_names()` 从这里解析，与 FACT_COLUMNS 对账。
FACT_COLUMN_DDL = _FACT_COLUMN_DDL


@dataclass(frozen=True, slots=True)
class AggregateSpec:
    """一个分钟聚合列的四种写法，放在一起以免它们各漂各的。

    `state` 进物化视图的 SELECT；`agg_type` 是聚合表列类型；`merge` 从聚合表读终值；
    `detail` 从明细表直接算终值（回算/对账用）。
    """

    column: str
    state: str
    agg_type: str
    merge: str
    detail: str


# 聚合口径的唯一真源。
#
# `agg_type` 必须等于**服务端自己推断出来的类型**（对真 ClickHouse 量过才看清：26.9 会报
# "Conversion from AggregateFunction(sum, Nullable(Float64)) to AggregateFunction(sum, Float64)
# is not supported"）。`measure` 是 Nullable，所以 sum/max/min/quantile 的状态参数都带 Nullable；
# 写成非空类型时 MV 根本写不进目标表，而替身驱动的测试发现不了这一点。
#
# `merge`/`detail` 两侧统一 `ifNull(..., 0)`：与 `port.materialize_minutes` 同口径——
# 一个桶里全是缺测时 total/peak/floor/p95 取 0.0、measured 取 0，两侧对账才有意义。
AGGREGATE_STATES: tuple[AggregateSpec, ...] = (
    AggregateSpec("cnt", "countState()", "AggregateFunction(count)", "countMerge(cnt)", "count()"),
    AggregateSpec(
        "total", "sumState(measure)", "AggregateFunction(sum, Nullable(Float64))", "ifNull(sumMerge(total), 0)", "ifNull(sum(measure), 0)"
    ),
    AggregateSpec(
        "peak", "maxState(measure)", "AggregateFunction(max, Nullable(Float64))", "ifNull(maxMerge(peak), 0)", "ifNull(max(measure), 0)"
    ),
    AggregateSpec(
        "floor", "minState(measure)", "AggregateFunction(min, Nullable(Float64))", "ifNull(minMerge(floor), 0)", "ifNull(min(measure), 0)"
    ),
    AggregateSpec(
        "measured",
        "countState(measure)",
        "AggregateFunction(count, Nullable(Float64))",
        "countMerge(measured)",
        "count(measure)",
    ),
    AggregateSpec(
        "p95",
        "quantileState(0.95)(measure)",
        "AggregateFunction(quantile(0.95), Nullable(Float64))",
        "ifNull(quantileMerge(0.95)(p95), 0)",
        "ifNull(quantile(0.95)(measure), 0)",
    ),
)

# 分钟聚合的分组键：与明细表 ORDER BY 一致（minute 优先、region_code 次之），再加 kind 区分三类事实。
MINUTE_KEYS = ("minute", "region_code", "kind")

# --------------------------------------------------------------------------- ClickHouse

DATABASE_DDL = "CREATE DATABASE IF NOT EXISTS {database}"

# ORDER BY (minute, region_code)：主查询形态是"按区域取一段分钟区间"，minute 优先让时间窗扫描
# 落在连续 granule 上、region_code 次之给出每区域局部性；station/metric 刻意不进排序键，
# 保持排序键低基数（ClickHouse 反模式：高基数排序键会拖慢 merge 并放大 part 元数据）。
FACT_DDL = f"""CREATE TABLE IF NOT EXISTS {{database}}.{FACT_TABLE} (
{_FACT_COLUMN_DDL}
{_FACT_MATERIALIZED}
) ENGINE = MergeTree
PARTITION BY toYYYYMM(observed_at)
ORDER BY (minute, region_code)
TTL toDateTime(observed_at) + INTERVAL 90 DAY DELETE
SETTINGS index_granularity = 8192"""

MINUTE_DDL = f"""CREATE TABLE IF NOT EXISTS {{database}}.{MINUTE_TABLE} (
    minute DateTime,
    region_code LowCardinality(String),
    kind LowCardinality(String),
{{agg_columns}}
) ENGINE = AggregatingMergeTree
PARTITION BY toYYYYMM(minute)
ORDER BY ({", ".join(MINUTE_KEYS)})
TTL toDateTime(minute) + INTERVAL 400 DAY DELETE"""

MV_DDL = f"""CREATE MATERIALIZED VIEW IF NOT EXISTS {{database}}.{MINUTE_MV}
TO {{database}}.{MINUTE_TABLE} AS
SELECT {", ".join(MINUTE_KEYS)}, {{mv_states}}
FROM {{database}}.{FACT_TABLE}
GROUP BY {", ".join(MINUTE_KEYS)}"""


def _agg_column_defs() -> str:
    return ",\n".join(f"    {spec.column} {spec.agg_type}" for spec in AGGREGATE_STATES)


# 目标表形态的 MV 按**列名**对齐 SELECT 输出：少了 AS 别名，真服务端会直接报
# THERE_IS_NO_COLUMN（`countState()` 不是列名）。别名与 `MINUTE_TABLE` 的列定义同源于
# AGGREGATE_STATES，所以两者不会各写一份而漂移。
def _mv_state_columns() -> str:
    return ", ".join(f"{spec.state} AS {spec.column}" for spec in AGGREGATE_STATES)


def minute_select_from_detail(database: str = DEFAULT_DATABASE) -> str:
    """从明细表直接回算分钟终值（历史回填 / 对账 / 集成测试比对）。

    刻意用普通聚合函数而不是 `-State`：状态列的序列化 clickhouse-connect 读不了
    （`AggregateFunction(count) deserialization not supported`），回算要拿到能直接读的值。
    """
    columns = ", ".join(f"{spec.detail} AS {spec.column}" for spec in AGGREGATE_STATES)
    return f"SELECT {', '.join(MINUTE_KEYS)}, {columns} FROM {database}.{FACT_TABLE} GROUP BY {', '.join(MINUTE_KEYS)}"


def minute_select_from_agg(database: str = DEFAULT_DATABASE) -> str:
    """从聚合表读分钟值（读时合并 -State/-Merge），是查询侧默认出口。"""
    merges = ", ".join(f"{spec.merge} AS {spec.column}" for spec in AGGREGATE_STATES)
    return f"SELECT {', '.join(MINUTE_KEYS)}, {merges} FROM {database}.{MINUTE_TABLE} GROUP BY {', '.join(MINUTE_KEYS)}"


def build_clickhouse_ddl(database: str = DEFAULT_DATABASE) -> tuple[str, ...]:
    """按执行顺序渲染建表语句（全 IF NOT EXISTS，幂等、可反复执行）。"""
    return (
        DATABASE_DDL.format(database=database),
        FACT_DDL.format(database=database),
        MINUTE_DDL.format(database=database, agg_columns=_agg_column_defs()),
        MV_DDL.format(database=database, mv_states=_mv_state_columns()),
    )


CLICKHOUSE_DDL: tuple[str, ...] = build_clickhouse_ddl()


def insert_columns() -> tuple[str, ...]:
    """写入列 = FACT_COLUMNS（明细表不含 MATERIALIZED 列，分钟/measure 由服务端计算）。"""
    return FACT_COLUMNS


def fact_column_names(ddl: str = FACT_COLUMN_DDL) -> tuple[str, ...]:
    """解析明细表可写入列名（逐行一名），供测试断言与 `FACT_COLUMNS` 不漂移。"""
    names: list[str] = []
    for line in ddl.splitlines():
        stripped = line.strip().rstrip(",")
        if stripped:
            names.append(stripped.split(maxsplit=1)[0])
    return tuple(names)


# --------------------------------------------------------------------------- DuckDB

DUCKDB_FACTS_TABLE = "analytics_fact"

# 列顺序与 FACT_COLUMNS 严格同序，令同一条 as_tuple() 行可直接 executemany 写入两端。
DUCKDB_FACTS_DDL = f"""CREATE TABLE IF NOT EXISTS {DUCKDB_FACTS_TABLE} (
    event_id VARCHAR PRIMARY KEY,
    kind VARCHAR NOT NULL,
    region_code VARCHAR NOT NULL,
    station_id VARCHAR NOT NULL,
    metric VARCHAR NOT NULL,
    hazard_type VARCHAR NOT NULL,
    unit VARCHAR NOT NULL,
    quality_flag VARCHAR NOT NULL,
    value DOUBLE,
    risk_level TINYINT,
    latency_ms DOUBLE,
    trace_id VARCHAR NOT NULL,
    lat DOUBLE,
    lon DOUBLE,
    observed_at TIMESTAMPTZ NOT NULL,
    ingested_at TIMESTAMPTZ NOT NULL
)"""

# 小内存边缘机：限制 DuckDB 缓冲与线程，避免与采集/推理抢资源（值由 warehouse 侧按 config 下发）。
DUCKDB_EDGE_SETTINGS: tuple[str, ...] = (
    "SET TimeZone = 'UTC'",
    "SET memory_limit = '{memory_limit}'",
    "SET threads = {threads}",
    "SET preserve_insertion_order = false",
)

_DUCK_MEASURE = """CASE kind
        WHEN 'telemetry' THEN value
        WHEN 'latency' THEN latency_ms
        ELSE risk_level::DOUBLE
    END"""


def duckdb_minute_select(table: str = DUCKDB_FACTS_TABLE) -> str:
    """DuckDB 侧分钟物化：与 ClickHouse 明细回算同一口径（cnt/total/peak/floor/measured）。"""
    return (
        "SELECT date_trunc('minute', observed_at) AS minute, region_code, kind, "
        "count(*) AS cnt, "
        f"sum({_DUCK_MEASURE}) AS total, "
        f"max({_DUCK_MEASURE}) AS peak, "
        f"min({_DUCK_MEASURE}) AS floor, "
        f"count({_DUCK_MEASURE}) AS measured "
        f"FROM {table} "
        "GROUP BY minute, region_code, kind "
        "ORDER BY minute, region_code, kind"
    )


__all__ = [
    "AGGREGATE_STATES",
    "CLICKHOUSE_DDL",
    "DATABASE_DDL",
    "DEFAULT_DATABASE",
    "DUCKDB_EDGE_SETTINGS",
    "DUCKDB_FACTS_DDL",
    "DUCKDB_FACTS_TABLE",
    "FACT_COLUMNS",
    "FACT_COLUMN_DDL",
    "FACT_DDL",
    "FACT_TABLE",
    "MINUTE_DDL",
    "MINUTE_KEYS",
    "MINUTE_MV",
    "MINUTE_TABLE",
    "MV_DDL",
    "build_clickhouse_ddl",
    "duckdb_minute_select",
    "fact_column_names",
    "insert_columns",
    "minute_select_from_agg",
    "minute_select_from_detail",
]
