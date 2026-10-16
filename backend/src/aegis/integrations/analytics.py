"""分析旁路：链路/读数 → 事实行 → sink 入队。热路径只入队，坏了只计数，绝不外抛污染 SLA。（P2 纯搬运：函数体逐字未改。）"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from aegis.config import Settings, get_settings
from aegis.domain.chain import ChainResult
from aegis.domain.messages import TelemetryReading, utc_now
from aegis.integrations.shared import log
from aegis.integrations.state import IntegrationState


def build_analytics(settings: Settings | None = None) -> tuple[Any | None, IntegrationState]:
    """按配置建分析 sink；未启用或后端不可达时返回 (None, 状态)，调用方据此不挂任何旁路。"""
    cfg = settings or get_settings()

    if cfg.analytics_backend == "off":
        return None, IntegrationState(name="analytics", enabled=False, driver="off")

    if cfg.analytics_backend == "duckdb":
        from aegis.analytics.duckdb_warehouse import DuckDbWarehouse

        sink = DuckDbWarehouse(cfg.duckdb_path)
        return sink, IntegrationState(
            name="analytics",
            enabled=True,
            driver="duckdb",
            detail={"path": cfg.duckdb_path, "spatial_ready": sink.spatial_ready},
        )

    from aegis.analytics.clickhouse_sink import ClickHouseSink, connect_clickhouse
    from aegis.analytics.port import AnalyticsSinkError

    try:
        client = connect_clickhouse(
            host=cfg.clickhouse_host,
            port=cfg.clickhouse_port,
            database=cfg.clickhouse_database,
            username=cfg.clickhouse_username,
            password=cfg.clickhouse_password,
            secure=cfg.clickhouse_secure,
        )
    except AnalyticsSinkError as exc:
        log.warning("ClickHouse 不可达，分析旁路关闭", extra={"err": str(exc)[:200]})
        return None, IntegrationState(name="analytics", enabled=False, driver="clickhouse", detail={"error": str(exc)[:200]})

    return (
        ClickHouseSink(client, database=cfg.clickhouse_database),
        IntegrationState(
            name="analytics",
            enabled=True,
            driver="clickhouse",
            detail={"target": f"{cfg.clickhouse_host}:{cfg.clickhouse_port}/{cfg.clickhouse_database}"},
        ),
    )


def chain_facts(result: ChainResult) -> list[Any]:
    """链路结果 → 事实行：灾害事实按受影响区域展开，各段耗时作为时延事实入账。

    时延指标的 metric 名沿用 observability 台账的 `stage_*_ms`，分析侧不自造第二套口径。
    """
    from aegis.analytics.port import FactRow

    rows: list[Any] = []
    if result.warning is not None:
        rows.extend(FactRow.from_warning(result.warning))
    moment = utc_now()
    for stage in result.stages:
        rows.append(
            FactRow.from_latency(
                metric=f"stage_{stage.name}_ms",
                latency_ms=stage.latency_ms,
                observed_at=moment,
                trace_id=result.trace_id,
            )
        )
    return rows


def reading_facts(readings: Sequence[TelemetryReading]) -> list[Any]:
    from aegis.analytics.port import FactRow

    return [FactRow.from_reading(reading) for reading in readings]


class AnalyticsRecorder:
    """链路/摄取结果 → 事实行 → sink 入队。热路径只入队，异常一律不外抛。

    计数留在自己身上：分析旁路坏了不能污染 SLA 量测，但必须能被状态接口看见。
    """

    def __init__(self, sink: Any, *, driver: str, settings: Settings | None = None) -> None:
        self._sink = sink
        self._driver = driver
        self._settings = settings or get_settings()
        self.accepted = 0
        self.short_accepted = 0
        self.errors = 0
        self.last_error: str | None = None
        self.schema_error: str | None = None

    async def open(self) -> None:
        """启动期建表（可选）：把 DDL 留在启动，运行期热路径就不掺任何 I/O。"""
        if not self._settings.analytics_apply_schema or not hasattr(self._sink, "apply_schema"):
            return
        try:
            await self._sink.apply_schema()
        except Exception as exc:
            self.schema_error = f"{type(exc).__name__}: {str(exc)[:200]}"
            log.warning("分析后端建表失败，旁路保持惰性", extra={"err": self.schema_error})

    async def record_chain(self, result: ChainResult) -> None:
        await self._submit(chain_facts(result))

    async def record_readings(self, readings: Sequence[TelemetryReading]) -> None:
        await self._submit(reading_facts(readings))

    async def _submit(self, rows: list[Any]) -> None:
        if not rows:
            return
        try:
            accepted = int(await self._sink.ingest(rows))
        except Exception as exc:  # 分析是消费者：坏了就计数，绝不让链路崩溃
            self.errors += 1
            self.last_error = f"{type(exc).__name__}: {str(exc)[:200]}"
            log.warning("分析事实入队失败，已丢弃本批", extra={"rows": len(rows), "err": self.last_error})
            return
        self.accepted += accepted
        self.short_accepted += max(len(rows) - accepted, 0)

    def state(self) -> IntegrationState:
        detail: dict[str, object] = {"accepted": self.accepted, "not_accepted": self.short_accepted, "errors": self.errors}
        if self.last_error is not None:
            detail["last_error"] = self.last_error
        if self.schema_error is not None:
            detail["schema_error"] = self.schema_error
        try:
            stats = self._sink.stats()
        except Exception as exc:  # 读状态失败不该升级成故障，但也不能谎报健康
            detail["stats_error"] = type(exc).__name__
            return IntegrationState(name="analytics", enabled=True, driver=self._driver, detail=detail)
        if isinstance(stats, dict):
            detail["buffered"] = stats.get("buffered")
            detail["inserted"] = stats.get("inserted")
            detail["dropped"] = int(stats.get("dropped_overflow", 0)) + int(stats.get("dropped_closed", 0))
            detail["loss_rate"] = stats.get("loss_rate")
            if self._driver == "duckdb":
                detail["spatial_ready"] = stats.get("spatial_ready")
        return IntegrationState(name="analytics", enabled=True, driver=self._driver, detail=detail)

    async def close(self) -> None:
        """关停前显式落尽缓冲：分析数据可以晚到，但不能因为进程退出就凭空消失。"""
        grace_ms = int(self._settings.analytics_close_grace_ms)
        try:
            await self._sink.flush()
        except Exception as exc:
            self.errors += 1
            self.last_error = f"flush:{type(exc).__name__}: {str(exc)[:200]}"
            log.warning("分析旁路排空失败", extra={"err": self.last_error})
        try:
            await self._sink.close(grace_ms)
        except Exception as exc:
            log.warning("分析 sink 关停异常", extra={"err": type(exc).__name__})
