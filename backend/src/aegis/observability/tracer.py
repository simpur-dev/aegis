"""时延账本与跨度计时：所有考核指标（≤2s/≤3s/≤10s/≤3min/≤20min）的唯一度量出口。

设计要点：
- 计时统一使用单调时钟（perf_counter），避免系统时钟回拨造成的负值或异常尖峰；
- 样本进入环形账本，按需计算分位数，支持按指标名做阈值判定（breach）；
- 每条样本携带 trace_id，保证可按事件/按智能体归因。
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

from aegis.domain.messages import now_iso


@dataclass(frozen=True, slots=True)
class Sample:
    name: str
    ms: float
    trace_id: str | None
    outcome: str
    at: str
    agent_id: str | None = None
    seq: int = 0


@dataclass(slots=True)
class LatencyStats:
    count: int
    p50: float
    p95: float
    p99: float
    max: float
    mean: float
    budget_ms: float | None = None
    breaches: int = 0

    def as_dict(self) -> dict[str, object]:
        out: dict[str, object] = {
            "count": self.count,
            "p50_ms": round(self.p50, 3),
            "p95_ms": round(self.p95, 3),
            "p99_ms": round(self.p99, 3),
            "max_ms": round(self.max, 3),
            "mean_ms": round(self.mean, 3),
        }
        if self.budget_ms is not None:
            out["budget_ms"] = self.budget_ms
            out["breaches"] = self.breaches
            out["breach_rate"] = round(self.breaches / self.count, 4) if self.count else 0.0
        return out


def percentile(sorted_values: list[float], q: float) -> float:
    """线性插值分位数（q ∈ [0,100]）。空输入返回 0.0。"""
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    pos = (len(sorted_values) - 1) * (q / 100.0)
    low = int(pos)
    high = min(low + 1, len(sorted_values) - 1)
    frac = pos - low
    return sorted_values[low] * (1 - frac) + sorted_values[high] * frac


class LatencyLedger:
    def __init__(self, maxlen: int = 200_000) -> None:
        self._samples: deque[Sample] = deque(maxlen=maxlen)
        self._budgets: dict[str, float] = {}
        self._seq = 0

    @property
    def sequence(self) -> int:
        return self._seq

    def set_budget(self, name: str, budget_ms: float) -> None:
        self._budgets[name] = budget_ms

    def record(
        self,
        name: str,
        ms: float,
        *,
        trace_id: str | None = None,
        outcome: str = "ok",
        agent_id: str | None = None,
    ) -> Sample:
        if ms < 0:
            raise ValueError(f"时延不得为负: name={name} ms={ms}")
        self._seq += 1
        sample = Sample(
            name=name,
            ms=float(ms),
            trace_id=trace_id,
            outcome=outcome,
            at=now_iso(),
            agent_id=agent_id,
            seq=self._seq,
        )
        self._samples.append(sample)
        return sample

    def names(self) -> list[str]:
        return sorted({s.name for s in self._samples})

    def iterate_since(self, index: int) -> tuple[list[Sample], int]:
        """增量导出：返回 seq > index 的新样本与新游标，供 Prometheus 直方图灌入。"""
        return [s for s in self._samples if s.seq > index], self._seq

    def stats(self, name: str, *, outcome: str | None = None) -> LatencyStats:
        values = sorted(s.ms for s in self._samples if s.name == name and (outcome is None or s.outcome == outcome))
        if not values:
            return LatencyStats(count=0, p50=0.0, p95=0.0, p99=0.0, max=0.0, mean=0.0, budget_ms=self._budgets.get(name))
        budget = self._budgets.get(name)
        breaches = sum(1 for v in values if budget is not None and v > budget)
        return LatencyStats(
            count=len(values),
            p50=percentile(values, 50),
            p95=percentile(values, 95),
            p99=percentile(values, 99),
            max=values[-1],
            mean=sum(values) / len(values),
            budget_ms=budget,
            breaches=breaches,
        )

    def by_trace(self, trace_id: str) -> list[Sample]:
        return [s for s in self._samples if s.trace_id == trace_id]

    def by_agent(self, agent_id: str) -> list[Sample]:
        return [s for s in self._samples if s.agent_id == agent_id]

    def snapshot(self) -> dict[str, dict[str, object]]:
        return {name: self.stats(name).as_dict() for name in self.names()}

    def __len__(self) -> int:
        return len(self._samples)


@dataclass
class Span:
    name: str
    trace_id: str | None
    started: float = field(default_factory=time.perf_counter)
    finished_ms: float | None = None


class Tracer:
    def __init__(self, ledger: LatencyLedger | None = None) -> None:
        self.ledger = ledger or LatencyLedger()

    @contextmanager
    def span(self, name: str, *, trace_id: str | None = None, agent_id: str | None = None) -> Iterator[Span]:
        span = Span(name=name, trace_id=trace_id)
        try:
            yield span
        finally:
            span.finished_ms = (time.perf_counter() - span.started) * 1000
            self.ledger.record(name, span.finished_ms, trace_id=trace_id, agent_id=agent_id)

    def record(self, name: str, ms: float, **kw: object) -> Sample:
        return self.ledger.record(name, ms, **kw)  # type: ignore[arg-type]
