"""边缘侧 DuckDB 单文件仓库：高原弱网断网可写、离线可算、复网按 Parquet 交接。

为什么需要它：台站丢连接时，本地 `.duckdb` 文件就是唯一的分析数据面——分钟汇总、bbox/半径
空间查询都能**零网络**离线跑；平台侧靠 ClickHouse 物化，边缘侧靠它，二者口径同源（`port.sql`）。

线程模型（对齐"DuckDB 连接不可跨线程共享"）：
- `duckdb` 延迟 import；所有阻塞 I/O（建表、写、查、导出、关闭）都投递到一条**专用单线程执行器**，
  因此事件循环永不被嵌入式库阻塞（与 ClickHouse sink 同样的非阻塞约束）；
- 单 worker = 单持有者线程：连接在 worker 上惰性创建并复用，绝不跨线程共享句柄；
- `stats()` 是纯内存快照、不触库（可在事件循环上直接调用）。

spatial 是**可选**增强，不是依赖（诚实设计：无网且无缓存的机器装不上扩展）：
- 核心分析（写入、bbox、半径、分钟汇总、Parquet）只用标量 `lat/lon` + SQL，不碰扩展；
- 半径用自持的 haversine（大圆距离）算，避免依赖 `ST_Distance_Sphere` 的纬度 cos 修正；
- 仅点-多边形包含判定（`within_polygon`）用扩展；扩展不可用时该查询显式抛
  `SpatialUnavailableError`，其余空间查询照常工作。`spatial_ready/spatial_reason` 经 `stats()` 可见。
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import logging
import threading
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any

from aegis.analytics.buffer import (
    DEFAULT_BUFFER_LIMIT,
    DEFAULT_CLOSE_TIMEOUT_SECONDS,
    DEFAULT_FLUSH_INTERVAL_SECONDS,
    DEFAULT_MAX_BATCH,
    BoundedBatcher,
)
from aegis.analytics.port import (
    FACT_COLUMNS,
    DropPolicy,
    FactRow,
    MinuteFact,
    SpatialUnavailableError,
    WarehouseError,
    as_utc,
)
from aegis.analytics.sql import DUCKDB_EDGE_SETTINGS, DUCKDB_FACTS_DDL, DUCKDB_FACTS_TABLE, duckdb_minute_select

logger = logging.getLogger("aegis.analytics.duckdb")

TABLE = DUCKDB_FACTS_TABLE
_EARTH_RADIUS_M = 6_371_008.8
_PLACEHOLDERS = ", ".join(["?"] * len(FACT_COLUMNS))
_INSERT_COLUMNS = ", ".join(FACT_COLUMNS)

# 大圆距离（haversine）：全用乘法，规避 `^`（DuckDB 里是异或/幂的歧义），口径自持不依赖扩展。
# 距离在子查询里算、别名在外层过滤/排序——只用命名参数 $name，避免与位置参数混用。
_RADIUS_INNER = f"""
    SELECT event_id, region_code, kind, station_id, metric, hazard_type, value, risk_level, latency_ms,
           observed_at, lat, lon,
           ($earth_r * 2.0 * ASIN(LEAST(1.0, SQRT(
               SIN(RADIANS($lat - lat) / 2.0) * SIN(RADIANS($lat - lat) / 2.0)
               + COS(RADIANS($lat)) * COS(RADIANS(lat))
               * SIN(RADIANS($lon - lon) / 2.0) * SIN(RADIANS($lon - lon) / 2.0)
           )))) AS distance_m
    FROM {TABLE}
    WHERE lat IS NOT NULL AND lon IS NOT NULL
"""

_WITHIN_SQL = f"""
SELECT event_id, region_code, kind, station_id, metric, hazard_type, value, risk_level, latency_ms, observed_at
FROM {TABLE}
WHERE lat IS NOT NULL AND lon IS NOT NULL
  AND ST_Contains(ST_GeomFromText(?), ST_MakePoint(lon, lat)::GEOMETRY)
ORDER BY event_id
"""

_SELECT_COLS = (
    "event_id, region_code, kind, station_id, metric, hazard_type, value, risk_level, "
    "latency_ms, trace_id, lat, lon, observed_at, ingested_at"
)


def _connect_duckdb(path: Path) -> Any:
    """在 worker 线程内延迟 import duckdb：只接 ClickHouse 的部署不必装它，缺它才报类型化错误。"""
    try:
        module = importlib.import_module("duckdb")
    except ImportError as exc:
        raise WarehouseError(f"未安装 duckdb，无法打开边缘仓库: {exc}", detail={"path": str(path)}, retryable=False) from exc
    return module.connect(str(path))


def _sql_path(path: str | Path) -> str:
    """渲染为 SQL 字符串字面量（正斜杠 + 单引号转义），避免 ATTACH/COPY 拼接注入面。"""
    posix = Path(path).as_posix().replace("'", "''")
    return f"'{posix}'"


class DuckDbWarehouse:
    """单文件 DuckDB 边缘仓（实现 `port.AnalyticsSink`；查询/导出接口均为协程）。"""

    def __init__(
        self,
        path: str | Path,
        *,
        memory_limit: str = "256MB",
        threads: int = 1,
        allow_spatial: bool = True,
        max_batch: int = DEFAULT_MAX_BATCH,
        buffer_limit: int = DEFAULT_BUFFER_LIMIT,
        flush_interval: float = DEFAULT_FLUSH_INTERVAL_SECONDS,
        drop_policy: DropPolicy = "oldest",
        close_timeout: float = DEFAULT_CLOSE_TIMEOUT_SECONDS,
    ) -> None:
        if threads < 1:
            raise ValueError(f"threads 至少为 1: {threads}")
        self.path = Path(path)
        self.memory_limit = memory_limit
        self.threads = threads
        self.allow_spatial = allow_spatial
        self._con: Any = None
        self._owner_thread: int | None = None
        self._ex: ThreadPoolExecutor | None = None
        self._spatial_ready = False
        self._spatial_reason: str | None = None
        self._closed = False
        self._closing = False
        self._write_count = 0
        self._batcher: BoundedBatcher[FactRow] = BoundedBatcher(
            self._send,
            name=f"aegis-duckdb-{self.path.name}",
            max_batch=max_batch,
            buffer_limit=buffer_limit,
            flush_interval=flush_interval,
            drop_policy=drop_policy,
            close_timeout=close_timeout,
        )

    # --- AnalyticsSink ---

    async def ingest(self, rows: Sequence[FactRow]) -> int:
        # 以 `_closing` 而非 `_closed` 为闸：关停已开始就绝不能再建执行器，
        # 否则会留下一条永远不会 shutdown 的工作线程（排空动作自己会走批处理缓冲的拒绝路径）。
        if not self._closing:
            await self._ensure_worker()
        return self._batcher.offer(list(rows))

    async def flush(self) -> None:
        await self._batcher.flush()

    async def close(self, grace_ms: int) -> None:
        """幂等优雅关停：先排空缓冲，再关连接与执行器。

        顺序不能反：`_run` 以 `_closed` 为闸门，先置位会让 grace 窗口里的排空直接撞墙，
        "优雅退出"就退化成"必丢一批数据"。
        """
        if self._closing:
            return
        self._closing = True
        await self._batcher.aclose(grace_ms)
        self._closed = True
        if self._ex is not None:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(self._ex, self._close_conn_sync)
            await asyncio.to_thread(self._ex.shutdown, True)
            self._ex = None

    def stats(self) -> dict[str, object]:
        snapshot: dict[str, object] = {
            "backend": "duckdb",
            "path": str(self.path),
            "memory_limit": self.memory_limit,
            "threads": self.threads,
            "spatial_ready": self._spatial_ready,
            "spatial_reason": self._spatial_reason,
            "submitted": self._write_count,
            "closed": self._closed,
        }
        snapshot.update(self._batcher.stats())
        return snapshot

    @property
    def spatial_ready(self) -> bool:
        return self._spatial_ready

    # --- 直写与查询（协程：阻塞工作交给单线程执行器）---

    async def append(self, rows: Sequence[FactRow]) -> int:
        """提交落库（边缘侧要"当场写稳"）：直写 + 事务。

        返回**提交**条数而非落地条数：`INSERT OR IGNORE` 会让重复 `event_id` 被静默忽略，
        真实落地数以 `count()` 为准（弱网重放时两者必然不等，把它写成"写入"就是说谎）。
        """
        return await self._run(self._write_sync, list(rows))

    async def apply_schema(self) -> None:
        await self._run(self._ensure_open)

    async def count(self) -> int:
        return await self._run(self._count_sync)

    async def bbox_query(
        self,
        *,
        min_lon: float,
        max_lon: float,
        min_lat: float,
        max_lat: float,
        kind: str | None = None,
        region_code: str | None = None,
        since: datetime | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        if limit <= 0:
            raise ValueError(f"limit 必须为正: {limit}")
        return await self._run(
            self._bbox_sync,
            min_lon,
            max_lon,
            min_lat,
            max_lat,
            kind,
            region_code,
            since,
            limit,
        )

    async def radius_query(self, lon: float, lat: float, radius_m: float, *, kind: str | None = None) -> list[dict[str, Any]]:
        if radius_m < 0:
            raise ValueError(f"radius_m 不得为负: {radius_m}")
        return await self._run(self._radius_sync, lon, lat, radius_m, kind)

    async def within_polygon(self, wkt: str) -> list[dict[str, Any]]:
        return await self._run(self._within_sync, wkt)

    async def minute_rollup(
        self,
        *,
        region_code: str | None = None,
        kind: str | None = None,
        since: datetime | None = None,
    ) -> list[MinuteFact]:
        return await self._run(self._rollup_sync, region_code, kind, since)

    async def export_parquet(self, dest: str | Path) -> int:
        return await self._run(self._export_parquet_sync, dest)

    async def read_parquet_count(self, src: str | Path) -> int:
        return await self._run(self._read_parquet_count_sync, src)

    # --- 执行器 / worker 线程桥接 ---

    async def _ensure_worker(self) -> ThreadPoolExecutor:
        if self._ex is None:
            self._ex = ThreadPoolExecutor(max_workers=1, thread_name_prefix="aegis-duckdb")
        return self._ex

    async def _run(self, fn: Any, *args: Any) -> Any:
        if self._closed:
            raise WarehouseError(f"边缘仓库已关闭: {self.path}")
        ex = await self._ensure_worker()
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(ex, fn, *args)

    # --- 以下 *_sync 只在 worker 线程执行，绝不 await ---

    def _ensure_open(self) -> None:
        if self._con is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._con = _connect_duckdb(self.path)
        self._owner_thread = threading.get_ident()
        for statement in DUCKDB_EDGE_SETTINGS:
            self._con.execute(statement.format(memory_limit=self.memory_limit, threads=self.threads))
        self._con.execute(DUCKDB_FACTS_DDL)
        if self.allow_spatial:
            self._load_spatial()

    def _load_spatial(self) -> None:
        """尽力加载 spatial：先 LOAD（发行轮子/缓存），失败再 INSTALL。失败不抛出——核心分析不依赖它。"""
        if self._con is None:  # pragma: no cover - 防御
            return
        for statement in ("LOAD spatial", "INSTALL spatial", "LOAD spatial"):
            try:
                self._con.execute(statement)
            except Exception as exc:  # 记录原因但绝不因缺扩展而让开仓失败
                self._spatial_reason = f"{type(exc).__name__}: {str(exc)[:200]}"
                continue
            self._spatial_ready = True
            self._spatial_reason = None
            logger.info("duckdb spatial 已加载", extra={"path": str(self.path), "step": statement})
            return
        logger.warning("duckdb spatial 不可用，点-多边形查询将显式失败", extra={"reason": self._spatial_reason})

    def _write_sync(self, rows: list[FactRow]) -> int:
        if not rows:
            return 0
        self._ensure_open()
        values = [row.as_tuple() for row in rows]
        con = self._con
        # duckdb 1.5 的连接只有 begin/commit/rollback（没有 transaction() 上下文管理器）：
        # 一批行要么整部落地要么整部回滚，边缘断电重启后不留半批数据。
        con.begin()
        try:
            # INSERT OR IGNORE：PRIMARY KEY(event_id) 令重复事件只落一次（与物化侧去重口径一致）。
            con.executemany(f"INSERT OR IGNORE INTO {TABLE} ({_INSERT_COLUMNS}) VALUES ({_PLACEHOLDERS})", values)
        except Exception:
            con.rollback()
            raise
        con.commit()
        self._write_count += len(rows)
        return len(rows)

    async def _send(self, batch: Sequence[FactRow]) -> int:
        return await self._run(self._write_sync, list(batch))

    def _close_conn_sync(self) -> None:
        if self._con is not None:
            with contextlib.suppress(Exception):
                self._con.close()
            self._con = None

    def _count_sync(self) -> int:
        self._ensure_open()
        return int(self._con.execute(f"SELECT count(*) FROM {TABLE}").fetchone()[0])

    def _bbox_sync(
        self,
        min_lon: float,
        max_lon: float,
        min_lat: float,
        max_lat: float,
        kind: str | None,
        region_code: str | None,
        since: datetime | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        self._ensure_open()
        clauses = ["lat BETWEEN ? AND ?", "lon BETWEEN ? AND ?"]
        params: list[Any] = [min_lat, max_lat, min_lon, max_lon]
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind)
        if region_code is not None:
            clauses.append("region_code = ?")
            params.append(region_code)
        if since is not None:
            clauses.append("observed_at >= ?")
            params.append(as_utc(since))
        params.append(limit)
        sql = f"SELECT {_SELECT_COLS} FROM {TABLE} WHERE {' AND '.join(clauses)} ORDER BY observed_at, event_id LIMIT ?"
        return _rows_as_dicts(self._con.execute(sql, params))

    def _radius_sync(self, lon: float, lat: float, radius_m: float, kind: str | None) -> list[dict[str, Any]]:
        self._ensure_open()
        extra = " AND kind = $kind" if kind is not None else ""
        sql = f"SELECT * FROM ({_RADIUS_INNER}) r WHERE distance_m <= $radius_m{extra} ORDER BY distance_m, event_id"
        params: dict[str, Any] = {"earth_r": _EARTH_RADIUS_M, "lat": float(lat), "lon": float(lon), "radius_m": float(radius_m)}
        if kind is not None:
            params["kind"] = kind
        return _rows_as_dicts(self._con.execute(sql, params))

    def _within_sync(self, wkt: str) -> list[dict[str, Any]]:
        self._ensure_open()
        if not self._spatial_ready:
            raise SpatialUnavailableError(
                "spatial 扩展不可用，无法执行点-多边形包含判定", detail={"reason": self._spatial_reason, "path": str(self.path)}
            )
        return _rows_as_dicts(self._con.execute(_WITHIN_SQL, (wkt,)))

    def _rollup_sync(self, region_code: str | None, kind: str | None, since: datetime | None) -> list[MinuteFact]:
        self._ensure_open()
        clauses: list[str] = []
        params: list[Any] = []
        if region_code is not None:
            clauses.append("region_code = ?")
            params.append(region_code)
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind)
        if since is not None:
            clauses.append("observed_at >= ?")
            params.append(as_utc(since))
        inner = duckdb_minute_select(TABLE)
        outer = f"SELECT * FROM ({inner}) r" + (f" WHERE {' AND '.join(_rollup_filter_terms(clauses))}" if clauses else "")
        facts: list[MinuteFact] = []
        for row in self._con.execute(outer, params).fetchall():
            facts.append(_row_to_minute_fact(row))
        return facts

    def _export_parquet_sync(self, dest: str | Path) -> int:
        self._ensure_open()
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        self._con.execute("CHECKPOINT")
        self._con.execute(f"COPY (SELECT * FROM {TABLE}) TO {_sql_path(dest)} (FORMAT PARQUET)")
        return int(self._con.execute(f"SELECT count(*) FROM {TABLE}").fetchone()[0])

    def _read_parquet_count_sync(self, src: str | Path) -> int:
        self._ensure_open()
        return int(self._con.execute(f"SELECT count(*) FROM read_parquet({_sql_path(src)})").fetchone()[0])


def _rollup_filter_terms(clauses: list[str]) -> list[str]:
    # 内层已按 minute/region/kind 分组，外层过滤沿用同名列。
    return [c.replace("observed_at >=", "minute >=") for c in clauses]


def _row_to_minute_fact(row: Sequence[Any]) -> MinuteFact:
    minute, region_code, kind, cnt, total, peak, floor, measured = row[:8]
    cnt = int(cnt)
    measured = int(measured or 0)
    total = float(total) if total is not None else 0.0
    return MinuteFact(
        minute=as_utc(minute),
        region_code=str(region_code),
        kind=kind,
        count=cnt,
        total=total,
        peak=float(peak) if peak is not None else 0.0,
        floor=float(floor) if floor is not None else 0.0,
        mean=total / measured if measured else 0.0,
        p95=0.0,  # 边缘 rollup 不产出分位数（口径以平台侧物化/纯函数为准）
        skipped_missing=cnt - measured,
        worst_risk_level=None,
    )


def _rows_as_dicts(cursor: Any) -> list[dict[str, Any]]:
    columns = [d[0] for d in cursor.description]
    return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


__all__ = ["DuckDbWarehouse"]
