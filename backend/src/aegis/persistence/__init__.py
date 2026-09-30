"""生产持久层：PostgreSQL 17 + PostGIS + pgvector，替换内存运行态存储。

分层（依赖只向下，任何文件都不导入 aegis.api / aegis.services）：

    errors.py      类型化错误面（驱动异常不外泄）
    dsn.py         连接串归一化与口令脱敏（纯函数）
    rows.py        领域记录 <-> 关系行的纯映射（无 I/O，可在无库环境下单测）
    write_buffer.py 有界异步批处理与背压（无 SQL，退避重试）
    postgres.py    StoreProtocol 实现体：组合 rows 与 WriteBuffer，惰性建池
    vectors.py     余弦 top-k 查询构造与执行（窄查询函数）
    geo.py         站点半径检索与灾害轨迹面包含统计（GEOGRAPHY 口径）
    sql/           幂等 DDL，按文件名序由 postgres.apply_migrations 应用
    replay.py      预警准确率的离线索引量测（口径唯一事实源）

替换内存存储的前提是 `storage/store.py` 模块头的承诺：接口保持不变，换实现即可。
读写分工的理由见 `postgres.py` 模块头。
"""

from __future__ import annotations

from aegis.persistence.dsn import dsn_label, normalize_dsn, redact_dsn
from aegis.persistence.errors import (
    ConnectionFailedError,
    DsnError,
    MappingError,
    MigrationError,
    NotReadyError,
    PersistenceError,
    QueryFailedError,
    ReplayDatasetError,
    WriteFailedError,
)
from aegis.persistence.geo import stations_in_polygon, stations_within, trace_summary
from aegis.persistence.postgres import (
    PostgresStore,
    StoreProtocol,
    apply_migrations,
    insert_ignore,
    insert_upsert,
    migration_files,
)
from aegis.persistence.replay import (
    Confusion,
    ReplayCase,
    ReplayDataset,
    ReplayReport,
    load_dataset,
    measure,
    parse_dataset,
    replay,
    tally,
)
from aegis.persistence.vectors import VectorHit, build_top_k, search_top_k
from aegis.persistence.write_buffer import BufferCounters, WriteBuffer, WriteRequest, backoff_delay_ms

__all__ = [
    "BufferCounters",
    "Confusion",
    "ConnectionFailedError",
    "DsnError",
    "MappingError",
    "MigrationError",
    "NotReadyError",
    "PersistenceError",
    "PostgresStore",
    "QueryFailedError",
    "ReplayCase",
    "ReplayDataset",
    "ReplayDatasetError",
    "ReplayReport",
    "StoreProtocol",
    "VectorHit",
    "WriteBuffer",
    "WriteFailedError",
    "WriteRequest",
    "apply_migrations",
    "backoff_delay_ms",
    "build_top_k",
    "dsn_label",
    "insert_ignore",
    "insert_upsert",
    "load_dataset",
    "measure",
    "migration_files",
    "normalize_dsn",
    "parse_dataset",
    "redact_dsn",
    "replay",
    "search_top_k",
    "stations_in_polygon",
    "stations_within",
    "tally",
    "trace_summary",
]
