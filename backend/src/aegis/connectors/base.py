"""数据接入抽象与摄取服务（L1 感知层 → L2 数据层 → 总线）。

并发策略：多数据源 gather 并行采集，单源独立超时与失败隔离——
一个源断连（高原弱网常见）不拖垮整轮摄取，失败计数进入可观测指标。
"""

from __future__ import annotations

import abc
import asyncio
import logging
import math
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from time import perf_counter

from aegis.bus.gateway import AgentGateway
from aegis.config import Settings, get_settings
from aegis.connectors.trace import (
    HISTORY_LIMIT,
    SAMPLE_LIMIT,
    AnalyticsReceipt,
    Issue,
    RoundDetail,
    RoundList,
    RoundRecord,
    SourceProbe,
    SourceTrace,
    Stage,
    Status,
    exception_issue,
    interleave_samples,
    quality_counts,
    reading_sample,
    source_probe,
)
from aegis.domain.enums import Action
from aegis.domain.messages import TelemetryReading, make_event, new_trace_id, now_iso, parse_iso, utc_now
from aegis.errors import AegisError
from aegis.observability.tracer import Tracer
from aegis.storage.store import StoreProtocol

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
        store: StoreProtocol,
        tracer: Tracer,
        settings: Settings | None = None,
        sources: list[DataSource] | None = None,
        per_source_timeout_ms: int = 5_000,
        sla_ingest_seconds: float | None = None,
        on_readings: Callable[[Sequence[TelemetryReading]], Awaitable[AnalyticsReceipt | None]] | None = None,
    ) -> None:
        self._gateway = gateway
        self._store = store
        self._tracer = tracer
        self._settings = settings or get_settings()
        self._sources: list[DataSource] = list(sources or [])
        self._timeout_s = per_source_timeout_ms / 1000
        self._sla = sla_ingest_seconds if sla_ingest_seconds is not None else self._settings.sla_ingest_seconds
        # 旁路观察点：分析层需要读数事实，但摄取语义不该因为多一个消费者而改变。
        self._on_readings = on_readings
        self._history: deque[RoundRecord] = deque(maxlen=HISTORY_LIMIT)

    def recent_rounds(self, limit: int) -> RoundList:
        records = list(reversed(self._history))[:limit]
        return RoundList(
            capacity=HISTORY_LIMIT,
            sources=[source.name for source in self._sources],
            items=[record.summary() for record in records],
        )

    def round_detail(self, round_id: str) -> RoundDetail | None:
        record = next((row for row in reversed(self._history) if row.detail.id == round_id), None)
        return record.snapshot() if record else None

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
        sources = self.sources
        if not sources:
            return IngestReport()

        trace = trace_id or new_trace_id()
        started = perf_counter()
        report = IngestReport()
        collect, normalize, store, durability, publish, analytics = (
            Stage(key=key) for key in ("collect", "normalize", "store", "durability", "publish", "analytics")
        )
        detail = RoundDetail(
            id=new_trace_id(), trace_id=trace, started_at=now_iso(), duration_ms=0, status="empty",
            sources=len(sources), readings=0, published=0,
            stages=[collect, normalize, store, durability, publish, analytics], source_details=[],
        )
        record = RoundRecord(detail)
        try:
            readings = await self._collect_round(sources, report, detail, collect, normalize)
            if not readings:
                if collect.status == "failed" or normalize.status == "failed":
                    detail.status = "failed"
                elif collect.status == "partial" or normalize.status == "partial":
                    detail.status = "partial"
                return report
            await self._store_round(readings, report, record, store, durability)
            available_at = utc_now()
            report.max_ingest_latency_seconds = self._record_latency(readings, trace, available_at)
            store.results["max_observed_to_query_seconds"] = round(report.max_ingest_latency_seconds, 3)
            await self._publish_round(readings, trace, report, publish)
            await self._analytics_round(readings, analytics)
            detail.status = "partial" if any(stage.status in ("partial", "failed") for stage in detail.stages) else "ok"
        except Exception:
            detail.status = "failed"
            raise
        finally:
            detail.readings = report.readings
            detail.published = report.published
            detail.duration_ms = _elapsed(started)
            self._history.append(record)

        return report

    async def _collect_round(
        self, sources: list[DataSource], report: IngestReport, detail: RoundDetail, collect: Stage, normalize: Stage,
    ) -> list[TelemetryReading]:
        started = perf_counter()
        collect.inputs = {"sources": len(sources), "timeout_ms": int(self._timeout_s * 1000)}
        results = await asyncio.gather(*(self._collect(source) for source in sources), return_exceptions=True)
        collect.duration_ms = _elapsed(started)
        all_readings: list[TelemetryReading] = []
        candidates = 0
        validated_ms = 0.0
        for source, source_result in zip(sources, results, strict=True):
            if isinstance(source_result, BaseException):
                outcome, probe, duration, failure = [], SourceProbe(inputs={"connector": source.name}), collect.duration_ms, source_result
            else:
                outcome, probe, duration, failure = source_result
            if failure:
                report.sources_failed[source.name] = str(failure)
                log.warning("数据源采集失败", extra={"source": source.name, "error": str(failure)})
                message = f"{source.name} 采集超时" if isinstance(failure, TimeoutError) else f"{source.name} 采集或解析失败"
                issue = exception_issue(failure, message)
                collect.issues.append(issue)
            else:
                report.sources_ok.append(source.name)
                issue = None
            candidates += len(outcome)
            validation_start = perf_counter()
            accepted = self._validate(outcome, probe)
            validated_ms += (perf_counter() - validation_start) * 1000
            all_readings.extend(accepted)
            status: Status = "failed" if failure else "partial" if probe.issues else "ok" if accepted else "empty"
            detail.source_details.append(SourceTrace(
                name=source.name, status=status, duration_ms=duration, received=probe.received, normalized=len(accepted),
                inputs=probe.inputs, input_samples=probe.input_samples,
                result_samples=[reading_sample(row) for row in accepted[:SAMPLE_LIMIT]],
                issues=[*probe.issues.values(), *([issue] if issue else [])],
            ))
            normalize.issues.extend(
                item.model_copy(update={"message": f"{source.name} · {item.message}"}) for item in probe.issues.values()
            )
        raw_units = {row.inputs.get("raw_unit") for row in detail.source_details}
        raw_items = (
            sum(row.received or 0 for row in detail.source_details)
            if len(raw_units) == 1 and None not in raw_units and all(row.received is not None for row in detail.source_details) else None
        )
        collect.results = {
            "completed_sources": len(report.sources_ok), "failed_sources": len(report.sources_failed),
        }
        if raw_items is not None:
            collect.results.update(raw_items=raw_items, raw_unit=next(iter(raw_units)))
        collect.result_samples = interleave_samples([source.input_samples for source in detail.source_details])
        collect.status = ("partial" if report.sources_ok else "failed") if report.sources_failed else "ok"
        normalize.inputs = {"candidate_readings": candidates}
        normalize.input_samples = collect.result_samples
        normalize.result_samples = interleave_samples([source.result_samples for source in detail.source_details])
        normalize.duration_ms = round(validated_ms, 2) if candidates else None
        normalize.results = {
            "normalized": len(all_readings), "rejected_values": sum(
                issue.count for issue in normalize.issues if issue.code in ("invalid_value", "non_finite")
            ), **quality_counts(all_readings),
        }
        normalize.status = (
            "skipped" if collect.status == "failed" else
            "failed" if candidates and not all_readings else
            "partial" if normalize.issues else "ok" if all_readings else "empty"
        )
        return all_readings

    async def _collect(self, source: DataSource) -> tuple[list[TelemetryReading], SourceProbe, float, Exception | None]:
        started = perf_counter()
        probe = SourceProbe(inputs={"connector": source.name})
        token = source_probe.set(probe)
        try:
            readings = await asyncio.wait_for(source.collect(), timeout=self._timeout_s)
            return readings, probe, _elapsed(started), None
        except TimeoutError:
            return [], probe, _elapsed(started), TimeoutError(f"数据源采集超时: {source.name} (>{self._timeout_s}s)")
        except Exception as exc:
            return [], probe, _elapsed(started), exc
        finally:
            source_probe.reset(token)

    @staticmethod
    def _validate(readings: list[TelemetryReading], probe: SourceProbe) -> list[TelemetryReading]:
        accepted = []
        for reading in readings:
            if not math.isfinite(reading.value):
                probe.note("non_finite", "读数不是有限数值", **reading_sample(reading))
                continue
            try:
                timestamps = (parse_iso(reading.observed_at), parse_iso(reading.ingested_at))
            except ValueError:
                timestamps = ()
            if not timestamps or any(moment.utcoffset() is None for moment in timestamps):
                probe.note("invalid_time", "时间戳无效或缺少时区", **reading_sample(reading))
                continue
            accepted.append(reading)
        return accepted

    async def _store_round(
        self, readings: list[TelemetryReading], report: IngestReport, record: RoundRecord, stage: Stage, durability: Stage,
    ) -> None:
        started = perf_counter()
        stage.inputs = {"readings": len(readings)}
        stage.input_samples = [reading_sample(row) for row in readings[:SAMPLE_LIMIT]]
        port = self._store.telemetry
        tracked_write = getattr(port, "add_with_receipt", None)
        try:
            if tracked_write is None:
                report.readings = await port.add(readings)
            else:
                receipt = record.track_write(len(readings))
                durability.inputs = {"readings": len(readings), "async": True}
                durability.input_samples = stage.input_samples
                report.readings = await tracked_write(readings, receipt)
            stage.results = {"queryable": report.readings, "store": "postgres" if record.write else "memory"}
            stage.result_samples = stage.input_samples if report.readings == len(readings) else []
            stage.status = "ok" if report.readings == len(readings) else "partial"
            if report.readings < len(readings):
                stage.issues.append(Issue(
                    code="short_store_write", message="查询视图未接收整批读数", count=len(readings) - report.readings,
                ))
        except Exception as exc:
            stage.status = "failed"
            stage.issues.append(exception_issue(exc, "写入遥测查询视图失败"))
            raise
        finally:
            stage.duration_ms = _elapsed(started)

    async def _publish_round(self, readings: list[TelemetryReading], trace_id: str, report: IngestReport, stage: Stage) -> None:
        started = perf_counter()
        stage.inputs = {"readings": len(readings), "action": Action.TELEMETRY_READING.value, "transport": self._settings.bus_backend}
        stage.input_samples = [reading_sample(row) for row in readings[:SAMPLE_LIMIT]]
        report.published = await self._publish(readings, trace_id, stage)
        stage.results = {"published": report.published, "failed": len(readings) - report.published}
        stage.status = "ok" if report.published == len(readings) else "partial" if report.published else "failed"
        stage.duration_ms = _elapsed(started)

    async def _publish(self, readings: list[TelemetryReading], trace_id: str, stage: Stage) -> int:
        async def _one(reading: TelemetryReading) -> tuple[str | None, Issue | None]:
            try:
                message = make_event(
                    source=f"platform.connector_{_safe(reading.station_id)}",
                    action=Action.TELEMETRY_READING.value,
                    trace_id=trace_id,
                    target="platform.telemetry_sink",
                    payload=reading.model_dump(),
                )
                await self._gateway.publish_telemetry(reading, message)
                return message.msg_id, None
            except AegisError as exc:
                log.warning("遥测发布被拒", extra={"error": exc.message, "station": reading.station_id})
                return None, exception_issue(exc, "网关拒绝遥测消息")
            except Exception as exc:
                # 传输层的 RuntimeError（如 NATS 断连重连窗口）不是 AegisError——但隔离承诺
                # 是按"单条读数"立的：一条发不出去只丢这一条的回执，不炸整轮 gather。
                log.warning("遥测发布失败（传输层）", extra={"error": f"{type(exc).__name__}: {exc}", "station": reading.station_id})
                return None, exception_issue(exc, "遥测消息构造或传输失败")

        outcomes = await asyncio.gather(*(_one(r) for r in readings))
        probe = SourceProbe()
        successes = 0
        for reading, (msg_id, issue) in zip(readings, outcomes, strict=True):
            if issue:
                probe.note(issue.code, issue.message, **reading_sample(reading))
            else:
                successes += 1
                if len(stage.result_samples) < SAMPLE_LIMIT:
                    stage.result_samples.append({**reading_sample(reading), "message_id": msg_id})
        stage.issues = list(probe.issues.values())
        return successes

    async def _analytics_round(self, readings: list[TelemetryReading], stage: Stage) -> None:
        if self._on_readings is None:
            stage.results = {"enabled": False}
            return
        started = perf_counter()
        stage.inputs = {"readings": len(readings)}
        stage.input_samples = [reading_sample(row) for row in readings[:SAMPLE_LIMIT]]
        try:
            receipt = await self._on_readings(readings)
            accepted = receipt.accepted if receipt is not None else None
            stage.results = {"enabled": True, "accepted": accepted}
            stage.status = "ok" if accepted is None or accepted == len(readings) else "partial" if accepted else "failed"
            if receipt is not None and receipt.error_type:
                stage.issues.append(Issue(code=receipt.error_type, message="分析消费者接收失败", count=len(readings)))
            elif accepted is not None and accepted < len(readings):
                stage.issues.append(Issue(code="analytics_short_write", message="分析消费者未接收整批数据", count=len(readings) - accepted))
        except Exception as exc:
            stage.status = "failed"
            stage.issues.append(exception_issue(exc, "分析消费者接收失败"))
            log.warning("分析消费者接收失败", exc_info=True)
        finally:
            stage.duration_ms = _elapsed(started)

    def _record_latency(self, readings: list[TelemetryReading], trace_id: str, available_at: datetime) -> float:
        """接入时延（≤5min 指标）：读数观测时刻 → 入库可查时刻。"""
        worst = 0.0
        for reading in readings:
            latency = max((available_at - parse_iso(reading.ingested_at)).total_seconds(), 0.0)
            observed_latency = max((available_at - parse_iso(reading.observed_at)).total_seconds(), 0.0)
            self._tracer.record("ingest_end_to_end_seconds", observed_latency, trace_id=trace_id)
            self._tracer.record("ingest_store_to_query_ms", latency * 1000, trace_id=trace_id)
            worst = max(worst, observed_latency)
        if worst > self._sla:
            log.warning("接入时延超 SLA", extra={"seconds": round(worst, 2), "sla": self._sla})
        return worst

    async def run_loop(self, stop: asyncio.Event, *, interval_seconds: float | None = None) -> int:
        """周期摄取。返回执行轮次，供测试与优雅关停使用。

        每轮自带异常守护：一轮失败（总线断连、存储瞬时故障）记数后继续下一轮，
        **不允许任务带着未处理异常死掉**——那是"平台活着但再也不进数"的静默停摆
        （对照 `_escalate_forever` 同一口径；此前 run_loop 无守护，实测口径不一致）。
        """
        period = interval_seconds or self._settings.simulator_interval_seconds
        rounds = 0
        failures = 0
        while not stop.is_set():
            try:
                await self.ingest_once()
            except Exception as exc:
                failures += 1
                log.exception("摄取轮次失败（第 %d 次）：%s: %s", failures, type(exc).__name__, exc)
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


def _elapsed(started: float) -> float:
    return round((perf_counter() - started) * 1000, 2)


__all__ = ["DataSource", "IngestReport", "IngestService", "now_iso"]
