"""ClickHouse 写入器：服务端分钟物化的平台侧 sink（write-behind，绝不在请求路径上）。

前提说明（不装作它是异步的）：`clickhouse-connect` 是**同步** HTTP 客户端。本模块所有会阻塞的
调用——批量 `insert`、建表 `command`、`close`——一律用 `asyncio.to_thread` 投递到默认执行器的
工作线程，事件循环里不做任何 I/O。`ingest()` 只做入队（`BoundedBatcher.offer` 是同步且 O(1) 的
deque 追加），因此摄取回路在弱网/慢盘下也不会卡住链路。

批量 insert 走列式（`column_oriented=True`）路径：`data` 是"每列一条序列"，与 ClickHouse 原生
按列压缩/编码的写入模型一致，比逐行省内存、更快。

失败处理：任何一次 `insert` 失败都被收敛为类型化的 `AnalyticsSinkError`；后台循环吞掉错误并把
批次回灌缓冲头部（保序、可重试），只有显式 `flush()` 才把错误上抛给调用方。链路侧因此"永不因
分析后端的抖动而崩溃"，故障面通过 `stats()` 可见（failures / retries / dropped_* / loss_rate /
last_error）。
"""

from __future__ import annotations

import asyncio
import importlib
import logging
from collections.abc import Sequence
from typing import Protocol

from aegis.analytics.buffer import (
    DEFAULT_BUFFER_LIMIT,
    DEFAULT_CLOSE_TIMEOUT_SECONDS,
    DEFAULT_FLUSH_INTERVAL_SECONDS,
    DEFAULT_MAX_BATCH,
    BoundedBatcher,
)
from aegis.analytics.port import AnalyticsSinkError, DropPolicy, FactRow
from aegis.analytics.sql import DEFAULT_DATABASE, FACT_COLUMNS, FACT_TABLE, build_clickhouse_ddl

logger = logging.getLogger("aegis.analytics.clickhouse")

DEFAULT_DATABASE_NAME = DEFAULT_DATABASE


class ClickHouseClient(Protocol):
    """`clickhouse-connect` 客户端的最小子集；单测注入同签名替身，集成测试注入真客户端。"""

    def insert(
        self,
        table: str,
        data: Sequence[Sequence[object]],
        *,
        column_names: Sequence[str] | None = ...,
        database: str | None = ...,
        column_oriented: bool = ...,
    ) -> object: ...

    def command(self, query: str, *args: object, **kwargs: object) -> object: ...

    def close(self) -> None: ...


def connect_clickhouse(
    *,
    host: str = "127.0.0.1",
    port: int = 8123,
    database: str = DEFAULT_DATABASE_NAME,
    username: str = "default",
    password: str = "",
    secure: bool = False,
    connect_timeout: float = 10.0,
) -> ClickHouseClient:
    """clickhouse-connect 同步客户端工厂（返回的对象交给 sink 后，真正的读写都在工作线程）。

    **会阻塞**（建连接池）：只应在进程启动/重连时调用，不要在请求路径里调用；如需在协程中获取，
    用 `await asyncio.to_thread(connect_clickhouse, ...)`。驱动用 `importlib` 延迟加载——OLAP 是
    可选子系统，import `aegis.analytics` 时不应把 clickhouse-connect（及其 numpy 依赖）拉进进程。
    """
    try:
        module = importlib.import_module("clickhouse_connect")
    except ImportError as exc:  # 环境缺依赖：不是逻辑错误，给出可诊断的类型化错误
        raise AnalyticsSinkError(f"未安装 clickhouse-connect，无法连接 ClickHouse: {exc}", retryable=False) from exc
    client = module.get_client(
        host=host,
        port=port,
        database=database,
        username=username,
        password=password,
        secure=secure,
        connect_timeout=int(connect_timeout),
    )
    return client


def _backoff(attempt: int, base: float, cap: float) -> float:
    """第 attempt 次（从 1 起）重试前的封顶指数退避秒数。"""
    return min(base * (2 ** (attempt - 1)), cap)


def create_schema(client: ClickHouseClient, *, database: str = DEFAULT_DATABASE_NAME) -> tuple[str, ...]:
    """逐条执行建表 DDL（幂等）。**同步阻塞**：由 `ClickHouseSink.apply_schema` 放进工作线程。"""
    statements = build_clickhouse_ddl(database)
    for statement in statements:
        client.command(statement)
    return statements


class ClickHouseSink:
    """有界缓冲 + 定时/满批 flush + 退避重试的 ClickHouse sink（实现 `port.AnalyticsSink`）。"""

    def __init__(
        self,
        client: ClickHouseClient,
        *,
        database: str = DEFAULT_DATABASE_NAME,
        table: str = FACT_TABLE,
        columns: Sequence[str] = FACT_COLUMNS,
        max_batch: int = DEFAULT_MAX_BATCH,
        buffer_limit: int = DEFAULT_BUFFER_LIMIT,
        flush_interval: float = DEFAULT_FLUSH_INTERVAL_SECONDS,
        max_attempts: int = 3,
        backoff_base: float = 0.2,
        backoff_cap: float = 5.0,
        drop_policy: DropPolicy = "oldest",
        close_timeout: float = DEFAULT_CLOSE_TIMEOUT_SECONDS,
    ) -> None:
        if max_attempts < 1:
            raise ValueError(f"max_attempts 至少为 1: {max_attempts}")
        if backoff_base <= 0 or backoff_cap < backoff_base:
            raise ValueError(f"退避参数不合法: base={backoff_base} cap={backoff_cap}")

        self._client = client
        self.database = database
        self.table = table
        self.columns = tuple(columns)
        self.max_attempts = max_attempts
        self.backoff_base = backoff_base
        self.backoff_cap = backoff_cap
        self._retries = 0
        self._started = False
        self._closed = False
        self._close_error: BaseException | None = None

        # sink 只把"列式批量 + 退避重试"的落库动作交给共享批处理缓冲；缓冲本身不认识 ClickHouse。
        self._batcher: BoundedBatcher[FactRow] = BoundedBatcher(
            self._send,
            name=f"aegis-ch-sink-{table}",
            max_batch=max_batch,
            buffer_limit=buffer_limit,
            flush_interval=flush_interval,
            drop_policy=drop_policy,
            close_timeout=close_timeout,
        )

    # --- AnalyticsSink 协议实现 ---

    async def ingest(self, rows: Sequence[FactRow]) -> int:
        """受理事实行：只做入队（同步、不 await、不触网）。首次调用惰性拉起后台 flush 循环。"""
        if not self._started and not self._closed:
            await self._batcher.start()
            self._started = True
        return self._batcher.offer(list(rows))

    async def flush(self) -> None:
        """显式落盘点：把缓冲全部写入（分批 + 重试）。最终失败抛 `AnalyticsSinkError`。"""
        await self._batcher.flush()

    async def close(self, grace_ms: int) -> None:
        """幂等优雅关停：在 grace 内尽力 flush → 关闭客户端（关闭也在工作线程）。"""
        if self._closed:
            return
        self._closed = True
        await self._batcher.aclose(grace_ms)
        await asyncio.to_thread(self._close_client)

    def stats(self) -> dict[str, object]:
        """纯内存台账：sink 级元数据 + 共享缓冲计数（不触网、不读盘）。"""
        snapshot: dict[str, object] = {
            "backend": "clickhouse",
            "database": self.database,
            "table": self.table,
            "max_attempts": self.max_attempts,
            "retries": self._retries,
            "closed": self._closed,
            "close_error": type(self._close_error).__name__ if self._close_error is not None else None,
        }
        snapshot.update(self._batcher.stats())
        return snapshot

    # --- 建表（可选，集成/装配时调用）---

    async def apply_schema(self, *, database: str | None = None) -> tuple[str, ...]:
        """在工作线程执行 DDL，返回已执行语句。"""
        db = database or self.database
        return await asyncio.to_thread(create_schema, self._client, database=db)

    # --- 内部：真正的落库动作（工作线程 + 退避重试）---

    async def _send(self, batch: Sequence[FactRow]) -> int:
        rows = list(batch)
        if not rows:
            return 0
        columns = [list(column) for column in zip(*(row.as_tuple() for row in rows), strict=True)]
        for attempt in range(1, self.max_attempts + 1):
            try:
                await asyncio.to_thread(
                    self._client.insert,
                    self.table,
                    columns,
                    column_names=self.columns,
                    database=self.database,
                    column_oriented=True,
                )
                return len(rows)
            except Exception as exc:  # 同步客户端异常类型繁多（网络/HTTP/解析）：统一收敛为类型化错误
                error = AnalyticsSinkError(
                    f"ClickHouse 批量写入失败（第 {attempt}/{self.max_attempts} 次）: {exc}",
                    detail={"batch": len(rows), "table": f"{self.database}.{self.table}", "cause": type(exc).__name__},
                )
                if attempt >= self.max_attempts:
                    # 终态：上抛类型化错误；缓冲会捕获它、把批次回灌队首并计入 failures/last_error。
                    raise error from exc
                self._retries += 1
                delay = _backoff(attempt, self.backoff_base, self.backoff_cap)
                logger.warning(
                    "ClickHouse 写入失败，退避重试", extra={"attempt": attempt, "delay_ms": round(delay * 1000, 1), "batch": len(rows)}
                )
                await asyncio.sleep(delay)
        raise AnalyticsSinkError("ClickHouse 写入循环异常退出")  # pragma: no cover — 循环必然 return 或 raise

    def _close_client(self) -> None:
        try:
            self._client.close()
        except Exception as exc:  # 关闭路径：留痕但不向上抛（调用方已能从 stats 的 close_error 看到）
            self._close_error = exc
            logger.warning("ClickHouse 客户端关闭失败", extra={"err": str(exc)})


__all__ = [
    "ClickHouseClient",
    "ClickHouseSink",
    "connect_clickhouse",
    "create_schema",
]
