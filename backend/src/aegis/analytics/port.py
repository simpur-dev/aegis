"""分析层窄端口：分钟事实行的唯一契约 + 可复现的分钟汇总纯函数。

设计边界（对齐"analytics 是 write-behind 消费者，绝不在请求路径上"）：

- `AnalyticsSink` 是两个后端（ClickHouse 服务侧物化 / DuckDB 边缘仓）共同实现的最小协议：
  `ingest` 受理行、`flush` 显式落盘、`close(grace_ms)` 限时优雅关停、`stats()` 纯内存台账。
  除 `clickhouse_sink` / `duckdb_warehouse` 之外，任何模块都不得直接 import
  `clickhouse_connect` / `duckdb`——驱动只封在这两个实现里。
- `FactRow` 是热路径要落库的"原始事实行"（遥测 / 灾害 / 时延三类共用一张宽事实表），
  `MinuteFact` 是分钟物化后的聚合行。列顺序 `FACT_COLUMNS` 与 DDL 同源，避免漂移。
- 分钟物化 `materialize_minutes` 是**纯函数**：只认 `observed_at`、乱序输入输出稳定、
  同一 `event_id` 只计一次、空窗口不产出行。ClickHouse 用物化视图在服务端做同一件事，
  DuckDB 在边缘侧用 SQL 做同一件事，三者口径必须一致（跨端对账与集成测试都以此为前提）。
- 错误复用 `aegis.errors.ErrorCode`，不新增错误码枚举成员。
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal, Protocol, runtime_checkable

from aegis.domain.messages import TelemetryReading, WarningRecord, parse_iso
from aegis.errors import AegisError, ErrorCode

FactKind = Literal["telemetry", "hazard", "latency"]
DropPolicy = Literal["oldest", "newest"]

# 空输入时指标聚合返回的"未量测"哨兵：调用方据此区分"真的是 0"与"根本没数据"。
NOT_MEASURED = "not_measured"

BUCKET_SECONDS = 60

# 明细事实表的列顺序即写入顺序（ClickHouse 需要显式列名，DuckDB 侧同名同序）。
# 分钟列 `minute` 不在其中：ClickHouse 以 MATERIALIZED 列服务端计算，
# DuckDB 在物化查询里用 date_trunc 现算——两边都不手写，避免口径漂移。
FACT_COLUMNS: tuple[str, ...] = (
    "event_id",
    "kind",
    "region_code",
    "station_id",
    "metric",
    "hazard_type",
    "unit",
    "quality_flag",
    "value",
    "risk_level",
    "latency_ms",
    "trace_id",
    "lat",
    "lon",
    "observed_at",
    "ingested_at",
)


# --------------------------------------------------------------------------- 错误


class AnalyticsSinkError(AegisError):
    """ClickHouse 批量写入在重试预算内仍失败（默认可重试，交由上层降级而非崩溃）。"""

    code = ErrorCode.INTERNAL


class AnalyticsSchemaError(AegisError):
    """记录形状 / 桶参数与目标表或方言不匹配，重试无意义。"""

    code = ErrorCode.SCHEMA_INVALID

    def __init__(self, message: str, *, detail: dict[str, object] | None = None, retryable: bool = False) -> None:
        super().__init__(message, detail=detail, retryable=retryable)


class WarehouseError(AegisError):
    """边缘 DuckDB 仓库错误基类（含运行环境缺少 duckdb 驱动的情形）。"""

    code = ErrorCode.INTERNAL


class SpatialUnavailableError(WarehouseError):
    """spatial 扩展不可用（未装且镜像不可达）时，点-多边形查询显式失败——标量 bbox/半径不受影响。"""

    def __init__(self, message: str, *, detail: dict[str, object] | None = None, retryable: bool = False) -> None:
        super().__init__(message, detail=detail, retryable=retryable)


# --------------------------------------------------------------------- 时间工具


def as_utc(moment: datetime) -> datetime:
    """naive 按 UTC 解释、aware 换算到 UTC：这是跨后端比较的唯一基准（应对时钟偏移/时区）。"""
    return moment.astimezone(UTC) if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def bucket_minute(moment: datetime, *, bucket_seconds: int = BUCKET_SECONDS) -> datetime:
    """把时刻截断到桶起点（UTC、秒以下精度丢弃）。边界规则：秒=0 归入当分钟、59.999 归入当分钟。"""
    if bucket_seconds <= 0:
        raise AnalyticsSchemaError(f"bucket_seconds 必须为正: {bucket_seconds}")
    aware = as_utc(moment)
    epoch = int(aware.timestamp()) // bucket_seconds * bucket_seconds
    return datetime.fromtimestamp(epoch, UTC)


def _finite(value: float | int | None) -> float | None:
    """None / bool / NaN / ±Inf 一律归一为 None：不让非有限值污染 min/max/sum。"""
    if value is None or isinstance(value, bool):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def percentile(values: Sequence[float], q: float) -> float:
    """线性插值分位数（q∈[0,100]）。空输入返回 0.0——与 observability 台账同一口径，但自带不外耦。"""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * (q / 100.0)
    low = int(pos)
    high = min(low + 1, len(ordered) - 1)
    frac = pos - low
    return ordered[low] * (1 - frac) + ordered[high] * frac


# --------------------------------------------------------------------- 事实行


@dataclass(frozen=True, slots=True)
class FactRow:
    """一条原始事实（遥测读数 / 灾害预警 / 时延样本）：分钟物化的输入单元。

    构造点在 `__post_init__` 统一归一：`observed_at`/`ingested_at` 归到 UTC，非有限的
    value/risk/latency 落 None——因此无论来自读数、预警还是时延台账，都满足同一不变式。
    """

    event_id: str
    kind: FactKind
    observed_at: datetime
    region_code: str
    station_id: str = ""
    metric: str = ""
    hazard_type: str = ""
    unit: str = ""
    quality_flag: str = "ok"
    value: float | None = None
    risk_level: int | None = None
    latency_ms: float | None = None
    trace_id: str = ""
    lat: float | None = None
    lon: float | None = None
    ingested_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.kind not in ("telemetry", "hazard", "latency"):
            raise AnalyticsSchemaError(f"未知事实类型: {self.kind!r}")
        object.__setattr__(self, "observed_at", as_utc(self.observed_at))
        ingested = self.ingested_at if self.ingested_at is not None else self.observed_at
        object.__setattr__(self, "ingested_at", as_utc(ingested))
        object.__setattr__(self, "value", _finite(self.value))
        object.__setattr__(self, "latency_ms", _finite(self.latency_ms))
        if self.risk_level is not None and isinstance(self.risk_level, bool):
            object.__setattr__(self, "risk_level", None)
        for name in ("lat", "lon"):
            raw = getattr(self, name)
            if raw is not None:
                object.__setattr__(self, name, _finite(float(raw)))

    @property
    def minute(self) -> datetime:
        return bucket_minute(self.observed_at)

    def measure(self) -> float | None:
        """该事实参与分钟聚合的数值量：遥测取 value、时延取 latency_ms、灾害取 risk_level。"""
        if self.kind == "telemetry":
            return self.value
        if self.kind == "latency":
            return self.latency_ms
        return None if self.risk_level is None else float(self.risk_level)

    def is_spatial(self) -> bool:
        return self.lat is not None and self.lon is not None

    def as_tuple(self) -> tuple[object, ...]:
        """按 `FACT_COLUMNS` 顺序展开为写入行（datetime 保持 UTC aware，两后端直接接受）。"""
        return (
            self.event_id,
            self.kind,
            self.region_code,
            self.station_id,
            self.metric,
            self.hazard_type,
            self.unit,
            self.quality_flag,
            self.value,
            self.risk_level,
            self.latency_ms,
            self.trace_id,
            self.lat,
            self.lon,
            self.observed_at,
            self.ingested_at,
        )

    # --- 领域对象 → 事实行 ---

    @classmethod
    def from_reading(
        cls,
        reading: TelemetryReading,
        *,
        hazard_type: str | None = None,
        lat: float | None = None,
        lon: float | None = None,
    ) -> FactRow:
        """遥测读数 → 事实行；`event_id` 用 (站点:指标:观测时刻) 作幂等去重键。"""
        observed = parse_iso(reading.observed_at)
        return cls(
            event_id=f"{reading.station_id}:{reading.metric}:{observed.isoformat(timespec='milliseconds')}",
            kind="telemetry",
            observed_at=observed,
            ingested_at=parse_iso(reading.ingested_at),
            region_code=reading.region_code,
            station_id=reading.station_id,
            metric=reading.metric,
            hazard_type=hazard_type or "",
            unit=reading.unit,
            quality_flag=reading.quality_flag,
            value=reading.value,
            lat=lat,
            lon=lon,
        )

    @classmethod
    def from_warning(cls, record: WarningRecord) -> Iterator[FactRow]:
        """预警 → 每个受影响区域一条灾害事实（区域趋势分析的天然粒度）。"""
        generated = parse_iso(record.generated_at)
        stamp = generated.isoformat(timespec="milliseconds")
        for region in record.region_codes:
            yield cls(
                event_id=f"{record.warning_id}:{region}:{stamp}",
                kind="hazard",
                observed_at=generated,
                region_code=region,
                hazard_type=record.hazard_type,
                risk_level=int(record.risk_level),
                unit="level",
                trace_id=record.trace_id,
            )

    @classmethod
    def from_latency(
        cls,
        *,
        metric: str,
        latency_ms: float,
        observed_at: datetime | str,
        region_code: str = "",
        station_id: str = "",
        trace_id: str = "",
        event_id: str = "",
    ) -> FactRow:
        """时延台账样本 → 事实行（`metric` 复用 observability 既有指标名，不自造口径）。"""
        moment = parse_iso(observed_at) if isinstance(observed_at, str) else observed_at
        return cls(
            event_id=event_id or f"{metric}:{moment.isoformat(timespec='microseconds')}:{station_id}",
            kind="latency",
            observed_at=moment,
            region_code=region_code,
            station_id=station_id,
            metric=metric,
            latency_ms=latency_ms,
            unit="ms",
            trace_id=trace_id,
        )


# ---------------------------------------------------------------- 分钟物化行


@dataclass(frozen=True, slots=True)
class MinuteFact:
    """一个 (分钟, 区域, 事实类型) 的物化聚合行：ClickHouse 服务端与 DuckDB 边缘侧同形状。

    `worst_risk_level` 的口径提醒：`RiskLevel` 是**倒序**枚举（1=红=最危险、5=无风险），
    所以"最严重"取的是数值**最小**者。字段名一律用 worst 而非 max，避免下一个读者又写成 max()。
    """

    minute: datetime
    region_code: str
    kind: FactKind
    count: int
    total: float
    peak: float
    floor: float
    mean: float
    p95: float
    skipped_missing: int
    worst_risk_level: int | None = None

    @property
    def key(self) -> tuple[datetime, str, str]:
        return (self.minute, self.region_code, self.kind)

    @property
    def measured(self) -> int:
        """参与数值聚合的样本数（= count - 缺测），空窗口为 0。"""
        return self.count - self.skipped_missing

    def as_dict(self) -> dict[str, object]:
        return {
            "minute": self.minute,
            "region_code": self.region_code,
            "kind": self.kind,
            "count": self.count,
            "total": self.total,
            "peak": self.peak,
            "floor": self.floor,
            "mean": self.mean,
            "p95": self.p95,
            "skipped_missing": self.skipped_missing,
            "worst_risk_level": self.worst_risk_level,
        }


@dataclass(slots=True)
class _Accumulator:
    """物化中间态（可写）：一个 key 累积的样本值与计数。"""

    count: int = 0
    missing: int = 0
    values: list[float] = field(default_factory=list)
    worst_risk: int | None = None

    def add(self, row: FactRow) -> None:
        self.count += 1
        if row.kind == "hazard" and row.risk_level is not None:
            # 1=红=最危险，故"最严重"是数值最小者：这里必须是 min，写成 max 会把红色预警报成无风险。
            self.worst_risk = row.risk_level if self.worst_risk is None else min(self.worst_risk, row.risk_level)
        measure = row.measure()
        if measure is None:
            self.missing += 1
            return
        self.values.append(measure)

    def to_fact(self, minute: datetime, region_code: str, kind: FactKind) -> MinuteFact:
        count = len(self.values)
        total = math.fsum(self.values)
        peak = max(self.values) if count else 0.0
        floor = min(self.values) if count else 0.0
        return MinuteFact(
            minute=minute,
            region_code=region_code,
            kind=kind,
            count=self.count,
            total=total,
            peak=peak,
            floor=floor,
            mean=total / count if count else 0.0,
            p95=percentile(self.values, 95),
            skipped_missing=self.missing,
            worst_risk_level=self.worst_risk,
        )


def dedupe_rows(rows: Iterable[FactRow]) -> Iterator[FactRow]:
    """按 `event_id` 首次出现去重（保序）：重复事件只计一次。空 event_id 视为各自独立不去重。"""
    seen: set[str] = set()
    for row in rows:
        if row.event_id:
            if row.event_id in seen:
                continue
            seen.add(row.event_id)
        yield row


def materialize_minutes(events: Iterable[FactRow], *, bucket_seconds: int = BUCKET_SECONDS) -> list[MinuteFact]:
    """原始事实行 → 分钟物化行（纯函数、确定性）。

    口径：桶归属只看 `observed_at`（乱序/迟到天然落进正确桶）；同一 `event_id` 只计一次；
    value/latency/risk 非有限或缺失的行计入 `count` 但不参与 sum/min/max/avg/p95（记 `skipped_missing`）；
    空窗口产空列表、不做零填充（把"没数据"读成 0 是分析侧的反模式）。输出按 (分钟, 区域, 类型) 稳定排序。
    """
    accumulators: dict[tuple[datetime, str, str], _Accumulator] = {}
    for row in dedupe_rows(events):
        start = bucket_minute(row.observed_at, bucket_seconds=bucket_seconds)
        key = (start, row.region_code, row.kind)
        accumulators.setdefault(key, _Accumulator()).add(row)
    facts = [
        acc.to_fact(minute, region_code, kind)  # type: ignore[arg-type]
        for (minute, region_code, kind), acc in accumulators.items()
    ]
    facts.sort(key=lambda fact: fact.key)
    return facts


def metrics_from_facts(facts: Sequence[MinuteFact]) -> dict[str, object]:
    """指标聚合：只从给定分钟行计算，绝不臆造精度/成功率。空输入返回零值并带 not_measured 标记。

    `worst_latency_p95_ms` 取 max（时延越大越坏），`worst_risk_level` 取 min（1=红，数值越小越危险）；
    没有灾害事实时风险为 None，而不是伪造一个"等级 0"。
    """
    if not facts:
        return {
            NOT_MEASURED: True,
            "minute_buckets": 0,
            "events": 0,
            "telemetry_samples": 0,
            "hazard_alerts": 0,
            "latency_samples": 0,
            "peak_value": 0.0,
            "worst_latency_p95_ms": 0.0,
            "worst_risk_level": None,
        }
    telemetry = [f for f in facts if f.kind == "telemetry"]
    hazard = [f for f in facts if f.kind == "hazard"]
    latency = [f for f in facts if f.kind == "latency"]
    risk_levels = [f.worst_risk_level for f in hazard if f.worst_risk_level is not None]
    return {
        NOT_MEASURED: False,
        "minute_buckets": len(facts),
        "events": sum(f.count for f in facts),
        "telemetry_samples": sum(f.count for f in telemetry),
        "hazard_alerts": sum(f.count for f in hazard),
        "latency_samples": sum(f.count for f in latency),
        "peak_value": max((f.peak for f in telemetry), default=0.0),
        "worst_latency_p95_ms": max((f.p95 for f in latency), default=0.0),
        "worst_risk_level": min(risk_levels, default=None),
    }


# --------------------------------------------------------------------- 窄端口


@runtime_checkable
class AnalyticsSink(Protocol):
    """分析写入器最小能力集：ClickHouseSink / DuckDbWarehouse / 测试替身都实现它。

    约定：`ingest` 只把行放进有界缓冲、永不抛错也永不等待 I/O（返回受理条数 ≠ 已落库）；
    只有显式 `flush` / `close` 会把类型化错误交给调用方；`stats` 是纯内存台账、不得触网或读盘。
    """

    async def ingest(self, rows: Sequence[FactRow]) -> int: ...

    async def flush(self) -> None: ...

    async def close(self, grace_ms: int) -> None: ...

    def stats(self) -> dict[str, object]: ...


__all__ = [
    "BUCKET_SECONDS",
    "FACT_COLUMNS",
    "NOT_MEASURED",
    "AnalyticsSchemaError",
    "AnalyticsSink",
    "AnalyticsSinkError",
    "DropPolicy",
    "FactKind",
    "FactRow",
    "MinuteFact",
    "SpatialUnavailableError",
    "WarehouseError",
    "as_utc",
    "bucket_minute",
    "dedupe_rows",
    "materialize_minutes",
    "metrics_from_facts",
    "percentile",
]
