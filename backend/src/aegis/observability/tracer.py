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
    # 样本值的单位随指标名后缀（`_ms` 毫秒、`_seconds` 秒）：账本不换算，只在出口声明单位。
    # 字段此前叫 ms，于是 `ingest_end_to_end_seconds` 的秒值一路以"ms"之名穿过导出器与报表。
    value: float
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
    # 预算与样本同单位：由指标名后缀决定（`_ms` 记毫秒，`_seconds` 记秒）。
    # 字段名此前叫 budget_ms，对秒制指标是个假标签——它装的从来不是毫秒。
    budget: float | None = None
    breaches: int = 0
    # 单位由账本自己带上（stats() 里按指标名后缀判定）：出口键名与预算都跟着它走，
    # 调用方不必各自记住"这条指标是秒还是毫秒"。
    unit: str = "ms"

    def as_dict(self) -> dict[str, object]:
        """出口字典：键名不声称单位，单位由 `unit` 字段自己说。

        这里曾经无条件写 `p50_ms`/`budget_ms`，而 `ingest_end_to_end_seconds` 记的是**秒**：
        同一份 JSON 里"0.019"配着键名 `_ms`，读它的人会差 1000 倍。判定不会变红（两侧同为秒才可比），
        骗人的只有键名——指标页、`metrics_report.py` 与助手的 `query.metrics` 三处各自"按后缀猜"，
        猜法还不一致。现在键名中性，单位显式，消费者只剩一种读法。
        """
        if self.unit not in ("ms", "s"):
            raise ValueError(f"未知单位: {self.unit}（只支持 ms / s）")
        out: dict[str, object] = {
            "count": self.count,
            "unit": self.unit,
            "p50": round(self.p50, 3),
            "p95": round(self.p95, 3),
            "p99": round(self.p99, 3),
            "max": round(self.max, 3),
            "mean": round(self.mean, 3),
        }
        if self.budget is not None:
            out["budget"] = self.budget
            out["breaches"] = self.breaches
            out["breach_rate"] = round(self.breaches / self.count, 4) if self.count else 0.0
        return out


#: 秒制指标名的合法后缀：单位就写在名字里，读账本的人与导出器都以此为准。
SECOND_SUFFIXES = ("_seconds", "_s")


def unit_of_metric(name: str) -> str:
    """指标的单位：`*_seconds` / `*_s` 记秒，其余（含无后缀的 `collab_txn`）记毫秒。

    这是"单位"的唯一判定处。此前只有 `register_sla_budgets` 的注释在说这件事，
    而出口 JSON 把秒制指标也塞进 `p50_ms`/`budget_ms`，等于一边声明单位在名字里、
    一边用键名否认它。改名（`_seconds`→`_ms`）会打断告警规则与历史序列，所以改的是出口形状。
    """
    return "s" if name.endswith(SECOND_SUFFIXES) else "ms"


def millis_of(name: str, value: float) -> float:
    """把该指标自己的单位换算成毫秒：给"只吃毫秒"的出口（Prometheus 直方图 `aegis_latency_ms`）用。

    没有这一步，`ingest_end_to_end_seconds` 的 0.007（秒）会被灌进毫秒直方图，
    看板读到的是"0.007 毫秒"——差 1000 倍，而且永远不会报错。
    """
    return value * 1000.0 if unit_of_metric(name) == "s" else value


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

    def set_budget(self, name: str, budget: float) -> None:
        """登记预算；单位与指标自己的单位一致（`*_seconds` 用秒，其余用毫秒）。"""
        self._budgets[name] = budget

    def budget_for(self, name: str) -> float | None:
        """只读：取已登记的预算（无预算返回 None），供跨度助手与指标导出复用。"""
        return self._budgets.get(name)

    def record(
        self,
        name: str,
        value: float,
        *,
        trace_id: str | None = None,
        outcome: str = "ok",
        agent_id: str | None = None,
    ) -> Sample:
        if value < 0:
            raise ValueError(f"时延不得为负: name={name} value={value}")
        self._seq += 1
        sample = Sample(
            name=name,
            value=float(value),
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
        unit = unit_of_metric(name)
        values = sorted(s.value for s in self._samples if s.name == name and (outcome is None or s.outcome == outcome))
        if not values:
            return LatencyStats(
                count=0, p50=0.0, p95=0.0, p99=0.0, max=0.0, mean=0.0, budget=self._budgets.get(name), unit=unit
            )
        budget = self._budgets.get(name)
        breaches = sum(1 for v in values if budget is not None and v > budget)
        return LatencyStats(
            count=len(values),
            p50=percentile(values, 50),
            p95=percentile(values, 95),
            p99=percentile(values, 99),
            max=values[-1],
            mean=sum(values) / len(values),
            budget=budget,
            breaches=breaches,
            unit=unit,
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

    def record(self, name: str, value: float, **kw: object) -> Sample:
        return self.ledger.record(name, value, **kw)  # type: ignore[arg-type]
