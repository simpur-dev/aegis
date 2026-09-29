"""数据接入抽象与摄取服务（L1 感知层 → L2 数据层 → 总线）。

并发策略：多数据源 gather 并行采集，单源独立超时与失败隔离——
一个源断连（高原弱网常见）不拖垮整轮摄取，失败计数进入可观测指标。
"""

from __future__ import annotations

import abc
import asyncio
import logging
from dataclasses import dataclass, field

from aegis.bus.gateway import AgentGateway
from aegis.config import Settings, get_settings
from aegis.domain.enums import Action
from aegis.domain.messages import TelemetryReading, make_event, new_trace_id, now_iso, parse_iso, utc_now
from aegis.errors import AegisError
from aegis.observability.tracer import Tracer
from aegis.storage.store import PlatformStore

log = logging.getLogger("aegis.connectors")


class DataSource(abc.ABC):
    name: str = "source"

    @abc.abstractmethod
    async def collect(self) -> list[TelemetryReading]:
        """返回本轮采集到的读数。实现必须自行处理超时并抛出异常，由摄取服务隔离。"""


@dataclass(slots=True)
class IngestReport:
    readings: int = 0
    sources_ok: list[str] = field(default_factory=list)
    sources_failed: dict[str, str] = field(default_factory=dict)
    published: int = 0
    max_ingest_latency_seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return not self.sources_failed

    def as_dict(self) -> dict[str, object]:
        return {
            "readings": self.readings,
            "sources_ok": self.sources_ok,
            "sources_failed": self.sources_failed,
            "published": self.published,
            "max_ingest_latency_seconds": round(self.max_ingest_latency_seconds, 3),
        }


class IngestService:
    def __init__(
        self,
        *,
        gateway: AgentGateway,
        store: PlatformStore,
        tracer: Tracer,
        settings: Settings | None = None,
        sources: list[DataSource] | None = None,
        per_source_timeout_ms: int = 5_000,
        sla_ingest_seconds: float | None = None,
    ) -> None:
        self._gateway = gateway
        self._store = store
        self._tracer = tracer
        self._settings = settings or get_settings()
        self._sources: list[DataSource] = list(sources or [])
        self._timeout_s = per_source_timeout_ms / 1000
        self._sla = sla_ingest_seconds if sla_ingest_seconds is not None else self._settings.sla_ingest_seconds

    @property
    def sources(self) -> list[DataSource]:
        return list(self._sources)

    def add_source(self, source: DataSource) -> None:
        if any(s.name == source.name for s in self._sources):
            raise ValueError(f"数据源名称重复: {source.name}")
        self._sources.append(source)

    def remove_source(self, name: str) -> bool:
        before = len(self._sources)
        self._sources = [s for s in self._sources if s.name != name]
        return len(self._sources) < before

    async def ingest_once(self, *, trace_id: str | None = None) -> IngestReport:
        if not self._sources:
            return IngestReport()

        trace = trace_id or new_trace_id()
        results = await asyncio.gather(*(self._collect(source) for source in self._sources), return_exceptions=False)

        report = IngestReport()
        all_readings: list[TelemetryReading] = []
        for source, outcome in zip(self._sources, results, strict=True):
            if isinstance(outcome, BaseException):
                report.sources_failed[source.name] = str(outcome)
                log.warning("数据源采集失败", extra={"source": source.name, "error": str(outcome)})
            else:
                report.sources_ok.append(source.name)
                all_readings.extend(outcome)

        if all_readings:
            await self._store.telemetry.add(all_readings)
            report.readings = len(all_readings)
            report.published = await self._publish(all_readings, trace)
            report.max_ingest_latency_seconds = self._record_latency(all_readings, trace)

        return report

    async def _collect(self, source: DataSource) -> list[TelemetryReading]:
        try:
            return await asyncio.wait_for(source.collect(), timeout=self._timeout_s)
        except TimeoutError:
            raise TimeoutError(f"数据源采集超时: {source.name} (>{self._timeout_s}s)") from None

    async def _publish(self, readings: list[TelemetryReading], trace_id: str) -> int:
        async def _one(reading: TelemetryReading) -> bool:
            message = make_event(
                source=f"platform.connector_{_safe(reading.station_id)}",
                action=Action.TELEMETRY_READING.value,
                trace_id=trace_id,
                target="platform.telemetry_sink",
                payload=reading.model_dump(),
            )
            try:
                await self._gateway.publish_telemetry(reading, message)
                return True
            except AegisError as exc:
                log.warning("遥测发布被拒", extra={"error": exc.message, "station": reading.station_id})
                return False

        outcomes = await asyncio.gather(*(_one(r) for r in readings))
        return sum(1 for o in outcomes if o)

    def _record_latency(self, readings: list[TelemetryReading], trace_id: str) -> float:
        """接入时延（≤5min 指标）：读数观测时刻 → 入库可查时刻。"""
        now = utc_now()
        worst = 0.0
        for reading in readings:
            latency = max((now - parse_iso(reading.ingested_at)).total_seconds(), 0.0)
            observed_latency = max((now - parse_iso(reading.observed_at)).total_seconds(), 0.0)
            self._tracer.record("ingest_end_to_end_seconds", observed_latency, trace_id=trace_id)
            self._tracer.record("ingest_store_to_query_ms", latency * 1000, trace_id=trace_id)
            worst = max(worst, observed_latency)
        if worst > self._sla:
            log.warning("接入时延超 SLA", extra={"seconds": round(worst, 2), "sla": self._sla})
        return worst

    async def run_loop(self, stop: asyncio.Event, *, interval_seconds: float | None = None) -> int:
        """周期摄取。返回执行轮次，供测试与优雅关停使用。"""
        period = interval_seconds or self._settings.simulator_interval_seconds
        rounds = 0
        while not stop.is_set():
            await self.ingest_once()
            rounds += 1
            try:
                await asyncio.wait_for(stop.wait(), timeout=period)
            except TimeoutError:
                continue
        return rounds


def _safe(value: str) -> str:
    """把站点 ID 规整为合法 source 片段（小写、点/空格转下划线）。"""
    cleaned = "".join(c if c.isalnum() or c in "_-" else "_" for c in value.lower())
    cleaned = cleaned.strip("_-") or "station"
    if not cleaned[0].isalpha():
        cleaned = f"s_{cleaned}"
    return cleaned[:31]


__all__ = ["DataSource", "IngestReport", "IngestService", "now_iso"]
