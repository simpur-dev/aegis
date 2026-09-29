"""Prometheus 指标导出：网关计数、协同事务、在线智能体、时延直方图。

指标值来自运行时台账的增量灌入（不做二次采样），保证 /metrics 与验收量测报告同源。
"""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest

from aegis.bus.gateway import AgentGateway
from aegis.observability.tracer import Tracer

_BUCKETS_MS = (
    0.5,
    1,
    2.5,
    5,
    10,
    25,
    50,
    100,
    250,
    500,
    1_000,
    2_000,
    5_000,
    10_000,
    30_000,
    60_000,
    120_000,
    180_000,
    300_000,
    600_000,
    1_200_000,
)


class MetricsExporter:
    def __init__(self, gateway: AgentGateway, tracer: Tracer, *, registry: CollectorRegistry | None = None) -> None:
        self._gateway = gateway
        self._tracer = tracer
        self._registry = registry or CollectorRegistry()
        self._events = Counter(
            "aegis_gateway_events_total",
            "网关收发事件计数",
            ["kind"],
            registry=self._registry,
        )
        self._txn = Counter(
            "aegis_collab_txn_total",
            "协同事务计数",
            ["outcome"],
            registry=self._registry,
        )
        self._online = Gauge("aegis_agents_online", "在线智能体数", registry=self._registry)
        self._success_rate = Gauge("aegis_collab_success_rate", "协同成功率", registry=self._registry)
        self._sla = Gauge("aegis_sla_threshold_seconds", "考核指标阈值", ["metric"], registry=self._registry)
        self._latency = Histogram(
            "aegis_latency_ms",
            "关键链路时延（毫秒）",
            ["metric"],
            buckets=_BUCKETS_MS,
            registry=self._registry,
        )
        self._last_counters: dict[str, int] = {}
        self._txn_cursor = 0
        self._latency_cursor = 0

    def set_sla_gauges(self, **values: float) -> None:
        for metric, seconds in values.items():
            self._sla.labels(metric=metric).set(seconds)

    def collect(self) -> bytes:
        for kind, value in self._gateway.counters.items():
            delta = value - self._last_counters.get(kind, 0)
            if delta > 0:
                self._events.labels(kind=kind).inc(delta)
            self._last_counters[kind] = value

        records = self._gateway.transactions
        for record in records[self._txn_cursor :]:
            self._txn.labels(outcome=record.outcome).inc()
        self._txn_cursor = len(records)

        samples, self._latency_cursor = self._tracer.ledger.iterate_since(self._latency_cursor)
        for sample in samples:
            self._latency.labels(metric=sample.name).observe(sample.ms)

        snapshot = self._gateway.snapshot()
        self._online.set(int(snapshot["agents_online"]))
        rate = self._gateway.success_rate()
        self._success_rate.set(rate if rate is not None else 0.0)
        return generate_latest(self._registry)
