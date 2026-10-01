"""分析层（analytics）：两条独立 sink、一个窄端口——ClickHouse 服务侧分钟物化 + DuckDB 离线边缘仓。

设计边界：
- `port`：窄端口 `AnalyticsSink` + 分钟事实行 `FactRow`/`MinuteFact` + 可复现的分钟物化纯函数
  （`materialize_minutes` / `metrics_from_facts`）+ 分析侧类型化错误。热路径只依赖这一层。
- `sql`：ClickHouse（明细 MergeTree + AggregatingMergeTree + 物化视图）与 DuckDB 的 DDL 文本常量。
- `buffer`：两个 sink 共用的有界异步批处理（`BatchingPort` 协议 + `BoundedBatcher` 默认实现）。
- `clickhouse_sink`：服务端分钟物化的写入器（同步 clickhouse-connect 全部投递到工作线程，绝不阻塞事件循环）。
- `duckdb_warehouse`：高原弱网边缘侧的单文件 DuckDB 仓库（无网可用：标量 lat/lon + bbox/半径；
  有 spatial 时才用扩展做点-多边形），离线汇总与 Parquet 交接。

驱动依赖（`clickhouse_connect` / `duckdb`）只封装在后两个 sink 里、且延迟 import；其它模块不得直接
import 它们。本包是 write-behind 消费者，任何后端抖动都降级、经 `stats()` 可见，绝不崩溃主链路。
"""

from __future__ import annotations

from aegis.analytics.buffer import BatchingPort, BoundedBatcher, BufferCounters
from aegis.analytics.clickhouse_sink import ClickHouseClient, ClickHouseSink, connect_clickhouse, create_schema
from aegis.analytics.duckdb_warehouse import DuckDbWarehouse
from aegis.analytics.port import (
    BUCKET_SECONDS,
    FACT_COLUMNS,
    NOT_MEASURED,
    AnalyticsSchemaError,
    AnalyticsSink,
    AnalyticsSinkError,
    FactKind,
    FactRow,
    MinuteFact,
    SpatialUnavailableError,
    WarehouseError,
    as_utc,
    bucket_minute,
    dedupe_rows,
    materialize_minutes,
    metrics_from_facts,
    percentile,
)
from aegis.analytics.sql import CLICKHOUSE_DDL, DUCKDB_FACTS_DDL, build_clickhouse_ddl, duckdb_minute_select, insert_columns

__all__ = [
    "BUCKET_SECONDS",
    "CLICKHOUSE_DDL",
    "DUCKDB_FACTS_DDL",
    "FACT_COLUMNS",
    "NOT_MEASURED",
    "AnalyticsSchemaError",
    "AnalyticsSink",
    "AnalyticsSinkError",
    "BatchingPort",
    "BoundedBatcher",
    "BufferCounters",
    "ClickHouseClient",
    "ClickHouseSink",
    "DuckDbWarehouse",
    "FactKind",
    "FactRow",
    "MinuteFact",
    "SpatialUnavailableError",
    "WarehouseError",
    "as_utc",
    "bucket_minute",
    "build_clickhouse_ddl",
    "connect_clickhouse",
    "create_schema",
    "dedupe_rows",
    "duckdb_minute_select",
    "insert_columns",
    "materialize_minutes",
    "metrics_from_facts",
    "percentile",
]
