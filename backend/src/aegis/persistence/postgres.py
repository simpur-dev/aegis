"""PostgreSQL 17 + PostGIS + pgvector 持久层：`StoreProtocol` 的实现，组合 rows 与 WriteBuffer。

与内存实现的分工（本模块的核心架构决定）：

- **读路径留在内存读模型**（`aegis.storage.store.PlatformStore`）。运行态存储的读接口全是同步的
  （`telemetry.query()` / `warnings.get()` / `chains.latest()`，见 api 与 connectors 的调用点），
  把同步读改成 asyncpg 只能靠阻塞事件循环实现 —— 那才是热路径上真正不可接受的事。
  读模型语义与替换前逐字节一致，因此调用点零改动。
- **写路径先落读模型、再把行参数投给 WriteBuffer**，由后台任务批量落库。
  库不可达（高原弱网、边缘断连）时预警链路照跑，恢复后按批次补齐；
  DB 侧以契约 ID 为键幂等吸收重放，所以"至少一次"投递不会放大计数。
- Postgres 因此是**持久事实与审计面**，不是读路径的运行时依赖。

SQL 文本里的标识符只来自代码常量（表名、`rows.*_COLUMNS`），所有取值一律走 `$n` 占位符。
"""

from __future__ import annotations

import hashlib
import importlib
import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import orjson

from aegis.config import Settings, get_settings
from aegis.domain.messages import StandardizedTaskUnit, TelemetryReading, WarningRecord, utc_now
from aegis.persistence import accuracy, geo, rows, vectors
from aegis.persistence.dsn import dsn_label, normalize_dsn, redact_dsn
from aegis.persistence.errors import (
    ConnectionFailedError,
    MappingError,
    MigrationError,
    NotReadyError,
    QueryFailedError,
    VectorEmbeddingError,
    describe,
)
from aegis.persistence.write_buffer import (
    DEFAULT_BACKOFF_BASE_MS,
    DEFAULT_BACKOFF_CAP_MS,
    DEFAULT_BATCH_ROWS,
    DEFAULT_CAPACITY_ROWS,
    DEFAULT_FLUSH_INTERVAL_MS,
    DEFAULT_JITTER_RATIO,
    WriteBuffer,
    WriteRequest,
)
from aegis.storage.store import (
    BoundedCollection,
    PlatformStore,
    StoreProtocol,
    TaskPort,
    TelemetryPort,
    WarningPort,
    station_ledger_row,
)
from aegis.workflow.model import WorkflowDef, WorkflowInstance

if TYPE_CHECKING:  # 仅用于类型标注：ChainResult 的载荷只在写出时按方法调用取用
    from aegis.pipeline.chain import ChainResult

log = logging.getLogger("aegis.persistence")

SQL_DIR = Path(__file__).resolve().parent / "sql"
MIGRATIONS_TABLE = (
    "CREATE TABLE IF NOT EXISTS schema_migrations ("
    " name text PRIMARY KEY,"
    " checksum text NOT NULL,"
    " applied_at timestamptz NOT NULL DEFAULT now())"
)

KIND_TELEMETRY = "telemetry"
KIND_TASKS = "tasks"
KIND_WARNING = "warning"
KIND_CHAIN = "chain"
KIND_WORKFLOW_DEF = "workflow_definition"
KIND_WORKFLOW_INSTANCE = "workflow_instance"

TELEMETRY_TABLE = "telemetry_readings"
TASKS_TABLE = "standardized_task_units"
WARNINGS_TABLE = "warnings"
RECEIPTS_TABLE = "warning_receipts"
CHAINS_TABLE = "chain_runs"
STATIONS_TABLE = "monitoring_stations"
WF_DEF_TABLE = "workflow_definitions"
WF_INSTANCE_TABLE = "workflow_instances"
WF_NODE_TABLE = "workflow_node_states"


def _columns(clause: Sequence[str]) -> str:
    return ", ".join(clause)


def _placeholders(count: int) -> str:
    return ", ".join(f"${index + 1}" for index in range(count))


def insert_ignore(table: str, columns: Sequence[str], conflict: Sequence[str]) -> str:
    """幂等插入：重复的自然键直接吸收（弱网重传的正常形态，不是错误）。"""
    return f"INSERT INTO {table} ({_columns(columns)}) VALUES ({_placeholders(len(columns))}) ON CONFLICT ({_columns(conflict)}) DO NOTHING"


def insert_upsert(table: str, columns: Sequence[str], conflict: Sequence[str], updating: Sequence[str]) -> str:
    """幂等 upsert：后到的同一契约 ID 用新值刷新观测列。"""
    assignments = ", ".join(f"{name} = EXCLUDED.{name}" for name in updating)
    head = f"INSERT INTO {table} ({_columns(columns)}) VALUES ({_placeholders(len(columns))})"
    return f"{head} ON CONFLICT ({_columns(conflict)}) DO UPDATE SET {assignments}"


def _refreshable(columns: Sequence[str], keep: Sequence[str] = ()) -> list[str]:
    return [name for name in columns if name not in keep]


SQL_TASK_UPSERT = insert_upsert(
    TASKS_TABLE, rows.TASK_COLUMNS, ("task_unit_id",), _refreshable(rows.TASK_COLUMNS, ("task_unit_id", "created_at"))
)
SQL_WARNING_UPSERT = insert_upsert(
    WARNINGS_TABLE,
    rows.WARNING_COLUMNS,
    ("warning_id",),
    _refreshable(rows.WARNING_COLUMNS, ("warning_id", "event_id", "generated_at")),
)
SQL_RECEIPT_IGNORE = insert_ignore(RECEIPTS_TABLE, rows.RECEIPT_COLUMNS, ("warning_id", "channel", "attempted_at"))
SQL_CHAIN_UPSERT = insert_upsert(
    CHAINS_TABLE, rows.CHAIN_COLUMNS, ("trace_id", "event_id"), _refreshable(rows.CHAIN_COLUMNS, ("trace_id", "event_id"))
)
SQL_STATION_UPSERT = insert_upsert(STATIONS_TABLE, rows.STATION_COLUMNS, ("station_id",), _refreshable(rows.STATION_COLUMNS))
SQL_WF_DEF_UPSERT = insert_upsert(WF_DEF_TABLE, rows.WORKFLOW_DEF_COLUMNS, ("workflow_id",), _refreshable(rows.WORKFLOW_DEF_COLUMNS))
SQL_WF_INSTANCE_UPSERT = insert_upsert(
    WF_INSTANCE_TABLE, rows.WORKFLOW_INSTANCE_COLUMNS, ("instance_id",), _refreshable(rows.WORKFLOW_INSTANCE_COLUMNS)
)
SQL_NODE_STATE_UPSERT = insert_upsert(
    WF_NODE_TABLE, rows.NODE_STATE_COLUMNS, ("instance_id", "node_id"), _refreshable(rows.NODE_STATE_COLUMNS, ("instance_id", "node_id"))
)
SQL_TASK_EMBEDDING = "UPDATE standardized_task_units SET embedding = $1::vector, embedded_model = $2 WHERE task_unit_id = $3"

# 遥测走 COPY 到临时暂存表再一次性插入：单批常见数百行，executemany 是逐批参数往返，
# COPY 是流式单命令（本机实测见集成测试报告）；幂等由 INSERT..SELECT..ON CONFLICT 保住，
# 同一语句内也吸收批内重复。低频实体仍用 executemany：行数小，多一次解析比建暂存表更便宜。
SQL_TELEMETRY_STAGED = (
    f"INSERT INTO {TELEMETRY_TABLE} ({_columns(rows.TELEMETRY_COLUMNS)})\n"
    f"SELECT {_columns(rows.TELEMETRY_COLUMNS)} FROM _stg_telemetry\n"
    "ON CONFLICT (station_id, metric, observed_at) DO NOTHING"
)
# LIKE 只带走 id 的 NOT NULL，不带走它的 IDENTITY 生成子句：COPY 不供 id，
# 因此暂存侧必须放开该约束，真实 id 仍由目标表 GENERATED ALWAYS AS IDENTITY 产生。
SQL_TELEMETRY_STAGING_DDL = (
    "CREATE TEMP TABLE IF NOT EXISTS _stg_telemetry (LIKE telemetry_readings INCLUDING DEFAULTS) ON COMMIT DROP",
    "ALTER TABLE _stg_telemetry ALTER COLUMN id DROP NOT NULL",
)


def migration_files(directory: Path = SQL_DIR) -> list[Path]:
    """迁移脚本按文件名字典序即应用顺序（001_、002_、003_）。"""
    return sorted(path for path in directory.glob("*.sql") if path.is_file())


def checksum(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:32]


# ---------- 运行态存储面 ----------
#
# 面定义在 `aegis.storage.store`（被依赖的一侧），本模块只提供实现：
# 上游按 `StoreProtocol` 编程即可在内存与 PostgreSQL 之间无痛切换。


class _TelemetryFacade:
    """读侧转发内存模型，写侧同时投递缓冲：调用方看不出差别。"""

    def __init__(self, store: PostgresStore, inner: TelemetryPort) -> None:
        self._store = store
        self._inner = inner

    async def add(self, readings: Iterable[TelemetryReading]) -> int:
        items = list(readings)
        self._store.enqueue_mapped(KIND_TELEMETRY, items, rows.telemetry_params)
        return await self._inner.add(items)

    def query(
        self,
        *,
        station_id: str | None = None,
        metric: str | None = None,
        region_code: str | None = None,
        since: datetime | str | None = None,
        until: datetime | str | None = None,
        limit: int = 500,
    ) -> list[TelemetryReading]:
        return self._inner.query(station_id=station_id, metric=metric, region_code=region_code, since=since, until=until, limit=limit)

    @property
    def size(self) -> int:
        return self._inner.size


class _WarningFacade:
    def __init__(self, store: PostgresStore, inner: WarningPort) -> None:
        self._store = store
        self._inner = inner

    async def put(self, record: WarningRecord) -> None:
        # 预警与其回执是一个原子单元：回执表外键指向预警，拆成两次入队就可能被溢出策略切成孤儿
        self._store.enqueue_mapped(KIND_WARNING, [record], lambda item: (rows.warning_params(item), rows.receipt_params(item)))
        await self._inner.put(record)

    def get(self, warning_id: str) -> WarningRecord | None:
        return self._inner.get(warning_id)

    def list(self, *, limit: int = 50, region_code: str | None = None) -> list[WarningRecord]:
        return self._inner.list(limit=limit, region_code=region_code)

    @property
    def size(self) -> int:
        return self._inner.size


class _TaskFacade:
    def __init__(self, store: PostgresStore, inner: TaskPort) -> None:
        self._store = store
        self._inner = inner

    async def put_many(self, units: Iterable[StandardizedTaskUnit]) -> int:
        items = list(units)
        self._store.enqueue_mapped(KIND_TASKS, items, rows.task_params)
        return await self._inner.put_many(items)

    def get(self, task_unit_id: str) -> StandardizedTaskUnit | None:
        return self._inner.get(task_unit_id)

    def by_event(self, event_id: str) -> list[StandardizedTaskUnit]:
        return self._inner.by_event(event_id)

    @property
    def size(self) -> int:
        return self._inner.size


class PostgresStore:
    """PostgreSQL 实现体：惰性建池、有界写缓冲、按 kind 分派的批量写入。"""

    def __init__(
        self,
        *,
        dsn: str | None = None,
        settings: Settings | None = None,
        read_model: StoreProtocol | None = None,
        pool_min_size: int = 1,
        pool_max_size: int = 8,
        command_timeout_seconds: float = 30.0,
        capacity_rows: int = DEFAULT_CAPACITY_ROWS,
        batch_rows: int = DEFAULT_BATCH_ROWS,
        flush_interval_ms: float = DEFAULT_FLUSH_INTERVAL_MS,
        backoff_base_ms: float = DEFAULT_BACKOFF_BASE_MS,
        backoff_cap_ms: float = DEFAULT_BACKOFF_CAP_MS,
        jitter_ratio: float = DEFAULT_JITTER_RATIO,
    ) -> None:
        cfg = settings or get_settings()
        self._dsn = normalize_dsn(dsn or cfg.pg_dsn)
        self._pool_min_size = pool_min_size
        self._pool_max_size = pool_max_size
        self._command_timeout = command_timeout_seconds
        self._pool: Any | None = None
        self._connect_errors = 0
        self._mapping_rejected = 0

        self._read = read_model or PlatformStore()
        self._buffer = WriteBuffer(
            self._sink,
            capacity_rows=capacity_rows,
            batch_rows=batch_rows,
            flush_interval_ms=flush_interval_ms,
            backoff_base_ms=backoff_base_ms,
            backoff_cap_ms=backoff_cap_ms,
            jitter_ratio=jitter_ratio,
        )
        self.telemetry: TelemetryPort = _TelemetryFacade(self, self._read.telemetry)
        self.warnings: WarningPort = _WarningFacade(self, self._read.warnings)
        self.tasks: TaskPort = _TaskFacade(self, self._read.tasks)

    # ---------- 读面（同步、非阻塞，与内存实现同语义） ----------

    @property
    def chains(self) -> BoundedCollection[ChainResult]:
        """链路读视图。落库在 `record_chain`：ChainResult 载荷含服务层类型，本层不回读重建。"""
        return self._read.chains

    def snapshot(self) -> dict[str, object]:
        return self._read.snapshot()

    @property
    def dsn_target(self) -> str:
        return dsn_label(self._dsn)

    @property
    def safe_dsn(self) -> str:
        return redact_dsn(self._dsn)

    # ---------- 生命周期 ----------

    async def __aenter__(self) -> PostgresStore:
        await self.connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def connect(self, *, start_buffer: bool = True) -> Any:
        """建立连接池（惰性：只有真要落库时才连库）。已建则原样复用。

        驱动用 importlib 取：asyncpg 属于 pyproject 的 `[postgres]` 可选额外依赖，
        包级静态导入会让未装该额外的部署在 import aegis 时就失败。
        """
        if self._pool is not None:
            return self._pool
        asyncpg = importlib.import_module("asyncpg")

        # 先在一裸连接上把扩展与表建好，再建带编解码器初始化的池。
        # 顺序不能反：vector 类型不存在时 _init_connection 的 register_vector 会以
        # "unknown type: public.vector" 直接失败，而空库首次启动正是边缘站点必经路径。
        try:
            bootstrap = await asyncpg.connect(self._dsn)
            try:
                await apply_migrations(bootstrap)
            finally:
                await bootstrap.close()
        except Exception as exc:
            self._connect_errors += 1
            raise ConnectionFailedError(f"库结构初始化失败：{self.dsn_target}", detail=describe(exc)) from exc

        try:
            self._pool = await asyncpg.create_pool(
                dsn=self._dsn,
                min_size=self._pool_min_size,
                max_size=self._pool_max_size,
                init=_init_connection,
                command_timeout=self._command_timeout,
            )
        except Exception as exc:
            self._connect_errors += 1
            raise ConnectionFailedError(f"连接池建立失败：{self.dsn_target}", detail=describe(exc)) from exc
        if start_buffer:
            self._buffer.start()
        log.info("持久层连接池已建立", extra={"target": self.dsn_target})
        return self._pool

    @property
    def connected(self) -> bool:
        return self._pool is not None

    @property
    def buffer_running(self) -> bool:
        return self._buffer.running

    def require_pool(self) -> Any:
        if self._pool is None:
            raise NotReadyError("连接池尚未建立：先 await store.connect()")
        return self._pool

    def acquire(self) -> Any:
        """借一条池内连接的异步上下文。读侧真库查询需要先 await store.connect()。"""
        return self.require_pool().acquire()

    async def migrate(self) -> list[str]:
        await self.connect()
        return await apply_migrations(self.require_pool(), directory=SQL_DIR)

    async def flush(self, *, timeout_ms: float | None = None) -> int:
        return await self._buffer.flush(timeout_ms=timeout_ms)

    async def close(self, *, grace_ms: float = 2_000.0) -> dict[str, object]:
        metrics = await self._buffer.close(grace_ms=grace_ms)
        pool, self._pool = self._pool, None
        if pool is not None:
            await pool.close()
        return metrics

    # ---------- 写面 ----------

    def enqueue_mapped(self, kind: str, items: Sequence[Any], mapper: Callable[[Any], Any]) -> int:
        """映射 + 入队。映射失败按丢弃计数而不抛出：一条脏数据不得打断预警链路。"""
        params: list[Any] = []
        for item in items:
            try:
                params.append(mapper(item))
            except MappingError as exc:
                self._mapping_rejected += 1
                log.warning("映射被拒，按丢弃计数", extra={"kind": kind, "error": exc.message, "detail": exc.detail})
        return self._buffer.submit(kind, params)

    async def record_chain(self, result: ChainResult) -> None:
        """与内存实现同序：任务 -> 预警 -> 链路，读模型即时可见，落库交后台。"""
        if result.task_units:
            await self.tasks.put_many(result.task_units)
        if result.warning is not None:
            await self.warnings.put(result.warning)
        await self._read.chains.add(result)
        self.enqueue_mapped(KIND_CHAIN, [result], lambda item: rows.chain_row(item, finished_at=utc_now()))

    async def upsert_station(
        self,
        station_id: str,
        region_code: str,
        lon: float | None,
        lat: float | None,
        *,
        name_zh: str = "",
        hazard_focus: Sequence[str] = (),
        elevation_m: float | None = None,
    ) -> None:
        """站点是维表：由接入/运维侧显式写入，不在热路径上，故直接 await 让失败可见。"""
        params = rows.station_params(station_id, region_code, lon, lat, name_zh=name_zh, hazard_focus=hazard_focus, elevation_m=elevation_m)
        async with self.acquire() as conn:
            await conn.execute(SQL_STATION_UPSERT, *params)

    async def update_task_embedding(self, task_unit_id: str, embedding: Sequence[float], *, model: str) -> None:
        """向量回填由检索模块离线调用，不经写缓冲：回填失败必须让调用方看见。

        维度/非有限值改抛 VectorEmbeddingError，与 vectors.search_top_k 的读侧同一口径：
        调用方只需处理一种"这个向量不可用"的错误，恢复动作都是换模型而不是重查。
        """
        try:
            params = rows.task_embedding_params(task_unit_id, embedding, model=model)
        except MappingError as exc:
            raise VectorEmbeddingError(exc.message, detail=exc.detail) from exc
        async with self.acquire() as conn:
            await conn.execute(SQL_TASK_EMBEDDING, *params)

    # ---------- 真库读面（读模型之外的补充查询） ----------

    async def load_warning(self, warning_id: str) -> WarningRecord | None:
        async with self.acquire() as conn:
            row = await conn.fetchrow(f"SELECT * FROM {WARNINGS_TABLE} WHERE warning_id = $1", warning_id)
            if row is None:
                return None
            receipts = await conn.fetch(f"SELECT * FROM {RECEIPTS_TABLE} WHERE warning_id = $1 ORDER BY attempted_at", warning_id)
            return rows.warning_from_row(row, list(receipts))

    async def load_tasks_by_event(self, event_id: str) -> list[StandardizedTaskUnit]:
        async with self.acquire() as conn:
            fetched = await conn.fetch(f"SELECT * FROM {TASKS_TABLE} WHERE event_id = $1 ORDER BY created_at", event_id)
            return [rows.task_from_row(record) for record in fetched]

    async def save_workflow_definition(self, definition: WorkflowDef) -> None:
        async with self.acquire() as conn:
            await conn.execute(SQL_WF_DEF_UPSERT, *rows.workflow_def_params(definition))

    async def load_workflow_definitions(self, *, name: str | None = None, limit: int = 50) -> list[WorkflowDef]:
        if limit <= 0:
            raise QueryFailedError("limit 必须为正", detail={"limit": limit})
        sql = f"SELECT * FROM {WF_DEF_TABLE}"
        args: list[Any] = []
        if name:
            args.append(name)
            sql += " WHERE name = $1"
        args.append(limit)
        sql += f" ORDER BY name, version DESC LIMIT ${len(args)}"
        async with self.acquire() as conn:
            return [rows.workflow_def_from_row(record) for record in await conn.fetch(sql, *args)]

    async def save_workflow_instance(self, instance: WorkflowInstance) -> None:
        """实例与其节点态同事务直写：跨表 FK 要求两者一起落库，不能一半成功。"""
        instance_params = rows.workflow_instance_params(instance)
        node_params = [rows.node_state_params(instance.instance_id, run) for run in instance.nodes.values()]
        async with self.acquire() as conn, conn.transaction():
            await conn.execute(SQL_WF_INSTANCE_UPSERT, *instance_params)
            if node_params:
                await conn.executemany(SQL_NODE_STATE_UPSERT, node_params)

    async def load_workflow_instance(self, instance_id: str) -> WorkflowInstance | None:
        async with self.acquire() as conn:
            row = await conn.fetchrow(f"SELECT * FROM {WF_INSTANCE_TABLE} WHERE instance_id = $1", instance_id)
            if row is None:
                return None
            states = await conn.fetch(f"SELECT * FROM {WF_NODE_TABLE} WHERE instance_id = $1 ORDER BY node_id", instance_id)
            return rows.workflow_instance_from_row(row, list(states))

    async def search_similar_tasks(
        self,
        embedding: Sequence[float],
        *,
        k: int = 10,
        hazard_type: str | None = None,
        region_code: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[vectors.VectorHit]:
        async with self.acquire() as conn:
            return await vectors.search_top_k(
                conn, embedding, k=k, hazard_type=hazard_type, region_code=region_code, since=since, until=until
            )

    async def stations_within(self, *, lon: float, lat: float, radius_m: float, limit: int = 50) -> list[dict[str, Any]]:
        async with self.acquire() as conn:
            return await geo.stations_within(conn, lon=lon, lat=lat, radius_m=radius_m, limit=limit)

    async def put_warning_labels(self, rows: Iterable[Mapping[str, Any]]) -> int:
        """导入现场真值标注（按 case_id 幂等）。

        刻意走借连接直写而不是有界写缓冲：回放是离线路径，导入结果必须立即可见，
        否则"刚导入就回放"会算出一份看着像真的旧数字。
        """
        await self.connect()
        async with self.acquire() as conn:
            return await accuracy.put_labels(conn, rows)

    async def accuracy_replay_cases(
        self,
        *,
        since: datetime,
        until: datetime | None = None,
        window_seconds: int = accuracy.DEFAULT_WINDOW_SECONDS,
        region_code: str | None = None,
    ) -> list[Any]:
        """真值与已落库预警的配对结果；判定口径见 `persistence/replay.case_from_row`。"""
        await self.connect()
        async with self.acquire() as conn:
            return await accuracy.replay_rows(conn, since=since, until=until, window_seconds=window_seconds, region_code=region_code)

    async def list_stations(self, *, region_code: str | None = None, limit: int = 500) -> list[dict[str, object]]:
        """站点清单：维表行（有名称与坐标）优先，再用"报过数但不在维表"的站点补齐。

        合并而不是二选一：维表由运维导入，遥测会自己冒出没登记过的站，只回一边必然对不上真实站数。
        维表缺坐标的站照样出现在清单里（坐标为 None），前端把它们列进"未定位"，
        这比按猜想的坐标把它们画上地图要好——错的坐标会被当成实测证据。
        库不可达时退回读视图：清单是只读事实，不该因一次网络抖动变成 503。
        """
        if limit <= 0:
            raise QueryFailedError("limit 必须为正", detail={"limit": limit})

        rows: list[dict[str, object]] = []
        seen: set[str] = set()
        if self.connected:
            try:
                async with self.acquire() as conn:
                    fetched = await geo.stations_list(conn, region_code=region_code, limit=limit)
            except Exception as exc:
                log.warning("站点维表读取失败，清单退回读视图", extra={"error": f"{type(exc).__name__}: {exc}"})
            else:
                for record in fetched:
                    station_id = str(record["station_id"])
                    seen.add(station_id)
                    rows.append(
                        station_ledger_row(
                            station_id,
                            str(record.get("region_code") or ""),
                            name_zh=str(record.get("name_zh") or ""),
                            hazard_focus=record.get("hazard_focus") or (),
                            elevation_m=record.get("elevation_m"),
                            lon=record.get("lon"),
                            lat=record.get("lat"),
                            geom=record.get("geom"),
                        )
                    )

        for row in await self._read.list_stations(region_code=region_code, limit=limit):
            if row["station_id"] not in seen:
                rows.append(row)
        # 两个来源各排各的序，拼起来就不是序；清单要能逐轮对账，必须按同一口径重排
        rows.sort(key=lambda row: str(row["station_id"]))
        return rows[:limit]

    async def stations_in_polygon(self, *, polygon_wkt: str, region_code: str | None = None) -> list[dict[str, Any]]:
        async with self.acquire() as conn:
            return await geo.stations_in_polygon(conn, polygon_wkt=polygon_wkt, region_code=region_code)

    async def hazard_trace_summary(
        self, *, polygon_wkt: str, since: datetime, until: datetime | None = None, hazard_type: str | None = None
    ) -> dict[str, Any]:
        async with self.acquire() as conn:
            return await geo.trace_summary(conn, polygon_wkt=polygon_wkt, since=since, until=until, hazard_type=hazard_type)

    # ---------- 观测（供 metrics 模块抓取） ----------

    @property
    def metrics(self) -> dict[str, object]:
        return {
            **self._buffer.metrics,
            "depth": self._buffer.depth,
            "capacity": self._buffer.capacity,
            "connected": self.connected,
            "connect_errors": self._connect_errors,
            "mapping_rejected": self._mapping_rejected,
        }

    @property
    def counters(self) -> Any:
        return self._buffer.counters

    # ---------- sink ----------

    async def _sink(self, batch: list[WriteRequest]) -> None:
        """一批写请求落库：单事务、按 kind 分组、组内 executemany 或 COPY。"""
        await self.connect()
        groups: dict[str, list[Any]] = {}
        for request in batch:
            groups.setdefault(request.kind, []).extend(request.rows)
        async with self.acquire() as conn, conn.transaction():
            for kind, payloads in groups.items():
                await _WRITERS[kind](conn, payloads)


async def _write_telemetry(conn: Any, payloads: list[Any]) -> None:
    for ddl in SQL_TELEMETRY_STAGING_DDL:
        await conn.execute(ddl)
    await conn.copy_records_to_table("_stg_telemetry", records=payloads, columns=rows.TELEMETRY_COLUMNS)
    await conn.execute(SQL_TELEMETRY_STAGED)


async def _write_tasks(conn: Any, payloads: list[Any]) -> None:
    await conn.executemany(SQL_TASK_UPSERT, payloads)


async def _write_warning(conn: Any, payloads: list[Any]) -> None:
    await conn.executemany(SQL_WARNING_UPSERT, [payload[0] for payload in payloads])
    receipts = [receipt for payload in payloads for receipt in payload[1]]
    if receipts:
        await conn.executemany(SQL_RECEIPT_IGNORE, receipts)


async def _write_chain(conn: Any, payloads: list[Any]) -> None:
    await conn.executemany(SQL_CHAIN_UPSERT, payloads)


async def _write_workflow_definition(conn: Any, payloads: list[Any]) -> None:
    await conn.executemany(SQL_WF_DEF_UPSERT, payloads)


async def _write_workflow_instance(conn: Any, payloads: list[Any]) -> None:
    for instance_params, node_params in payloads:
        await conn.execute(SQL_WF_INSTANCE_UPSERT, *instance_params)
        if node_params:
            await conn.executemany(SQL_NODE_STATE_UPSERT, node_params)


_WRITERS: dict[str, Callable[[Any, list[Any]], Any]] = {
    KIND_TELEMETRY: _write_telemetry,
    KIND_TASKS: _write_tasks,
    KIND_WARNING: _write_warning,
    KIND_CHAIN: _write_chain,
    KIND_WORKFLOW_DEF: _write_workflow_definition,
    KIND_WORKFLOW_INSTANCE: _write_workflow_instance,
}


async def _init_connection(conn: Any) -> None:
    """建连时的编解码器注册：pgvector 的 vector，以及 jsonb/json/geography 的文本口径。

    geography 只以 EWKT 文本进、以 SQL 侧 ST_* 函数出，解码器不解析二进制 EWKB ——
    本层从不把 geography 原样读回 Python。
    """
    # pgvector 随 numpy 一起被导入：动态导入既保持"可选额外依赖"的语义，也不把驱动类型拖进静态检查
    pgvector_asyncpg = importlib.import_module("pgvector.asyncpg")
    await pgvector_asyncpg.register_vector(conn)

    def _encode(value: object) -> str:
        return orjson.dumps(value).decode()

    for type_name in ("jsonb", "json"):
        await conn.set_type_codec(
            type_name,
            schema="pg_catalog",
            encoder=_encode,
            decoder=lambda text: orjson.loads(text),
            format="text",
        )
    await conn.set_type_codec("geography", schema="public", encoder=lambda text: text, decoder=lambda text: text, format="text")


async def apply_migrations(pool_or_conn: Any, *, directory: Path = SQL_DIR) -> list[str]:
    """按文件名序应用 DDL，返回本次实际应用的文件名；传池则自行借一条连接。

    每个脚本一个事务：DDL 失败整体回滚，不留半套表。
    刻意不用 CONCURRENTLY 建索引 —— 迁移在起服前跑完，此时没有流量可挡。
    """
    files = migration_files(directory)
    if not files:
        raise MigrationError("迁移目录为空", detail={"directory": str(directory)})
    if hasattr(pool_or_conn, "acquire"):
        async with pool_or_conn.acquire() as conn:
            return await _apply_migrations(conn, files)
    return await _apply_migrations(pool_or_conn, files)


async def _apply_migrations(conn: Any, files: Sequence[Path]) -> list[str]:
    await conn.execute(MIGRATIONS_TABLE)
    ledger = {record["name"]: record["checksum"] for record in await conn.fetch("SELECT name, checksum FROM schema_migrations")}
    applied: list[str] = []
    for path in files:
        text = path.read_text(encoding="utf-8")
        digest = checksum(text)
        recorded = ledger.get(path.name)
        if recorded is not None:
            if recorded != digest:
                raise MigrationError("已应用的迁移文件发生漂移", detail={"file": path.name, "expected": recorded, "actual": digest})
            continue
        async with conn.transaction():
            await conn.execute(text)
            await conn.execute("INSERT INTO schema_migrations (name, checksum) VALUES ($1, $2)", path.name, digest)
        applied.append(path.name)
    log.info("迁移完成" if applied else "无待应用迁移", extra={"applied": applied})
    return applied
