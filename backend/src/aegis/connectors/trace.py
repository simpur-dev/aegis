"""有界的遥测接入记录。展示可核对的字段，不保留上游载荷、凭据或堆栈。"""

from __future__ import annotations

import math
import re
from collections import Counter
from contextvars import ContextVar
from dataclasses import dataclass, field
from itertools import islice, zip_longest
from typing import Literal

from pydantic import BaseModel, Field

from aegis.domain.messages import TelemetryReading
from aegis.errors import AegisError
from aegis.persistence.write_buffer import WriteReceipt

SAMPLE_LIMIT = 8
HISTORY_LIMIT = 120
Cell = str | int | float | bool | None
Sample = dict[str, Cell]
Status = Literal["ok", "partial", "failed", "empty", "skipped", "pending"]


@dataclass(frozen=True)
class AnalyticsReceipt:
    accepted: int
    error_type: str | None = None


class Issue(BaseModel):
    code: str
    message: str
    count: int = 1
    samples: list[Sample] = Field(default_factory=list)


class Stage(BaseModel):
    key: str
    status: Status = "skipped"
    duration_ms: float | None = None
    inputs: Sample = Field(default_factory=dict)
    results: Sample = Field(default_factory=dict)
    input_samples: list[Sample] = Field(default_factory=list)
    result_samples: list[Sample] = Field(default_factory=list)
    issues: list[Issue] = Field(default_factory=list)


class SourceTrace(BaseModel):
    name: str
    status: Status
    duration_ms: float
    received: int | None
    normalized: int
    inputs: Sample
    input_samples: list[Sample]
    result_samples: list[Sample]
    issues: list[Issue]


class RoundSummary(BaseModel):
    id: str
    trace_id: str
    started_at: str
    duration_ms: float
    status: Status
    sources: int
    readings: int
    published: int


class RoundDetail(RoundSummary):
    stages: list[Stage]
    source_details: list[SourceTrace]


class RoundList(BaseModel):
    capacity: int
    sources: list[str]
    items: list[RoundSummary]


@dataclass
class RoundRecord:
    detail: RoundDetail
    write: WriteReceipt | None = None
    _write_rows: int = 0

    def track_write(self, rows: int) -> WriteReceipt:
        self._write_rows = rows
        self.write = WriteReceipt()
        return self.write

    def _persistence_status(self, write: WriteReceipt) -> tuple[Status, int, int]:
        pending = max(0, write.queued - write.flushed - write.dropped)
        dropped = self._write_rows - write.queued + write.dropped
        if dropped and not (pending or write.flushed):
            return "failed", pending, dropped
        if dropped or write.retries:
            return "partial", pending, dropped
        return ("pending" if pending else "ok"), pending, dropped

    def _status(self, durability: Status) -> Status:
        if self.detail.status != "ok":
            return self.detail.status
        return "partial" if durability == "failed" else durability

    def summary(self) -> RoundSummary:
        summary = RoundSummary.model_validate(self.detail, from_attributes=True)
        if self.write is not None:
            status, _, _ = self._persistence_status(self.write)
            summary.status = self._status(status)
        return summary

    def snapshot(self) -> RoundDetail:
        detail = self.detail.model_copy(deep=True)
        write = self.write
        if write is None:
            return detail
        stage = next(step for step in detail.stages if step.key == "durability")
        stage.status, pending, dropped = self._persistence_status(write)
        stage.results.update(queued=write.queued, committed=write.flushed, pending=pending, dropped=dropped, retries=write.retries)
        if write.flushed == self._write_rows:
            stage.result_samples = stage.input_samples
        if dropped:
            stage.issues.append(Issue(code="write_dropped", message="映射未通过或写缓冲丢弃", count=dropped))
        if write.last_error:
            stage.issues.append(Issue(code="write_retry", message=f"数据库提交失败，已触发重试（{write.last_error}）", count=write.retries))
        detail.status = self._status(stage.status)
        return detail


def safe_record(**values: object) -> Sample:
    result: Sample = {}
    for key, value in values.items():
        if isinstance(value, str):
            result[key] = value[:96]
        elif value is None or isinstance(value, (int, bool)):
            result[key] = value
        elif isinstance(value, float):
            result[key] = value if math.isfinite(value) else str(value)
        else:
            result[key] = f"<{type(value).__name__}>"
    return result


def reading_sample(reading: TelemetryReading) -> Sample:
    return safe_record(**reading.model_dump())


def interleave_samples(groups: list[list[Sample]]) -> list[Sample]:
    choices = (sample for row in zip_longest(*groups) for sample in row if sample is not None)
    return list(islice(choices, SAMPLE_LIMIT))


def valid_region(value: str) -> bool:
    return re.fullmatch(r"[0-9A-Z]{6,24}", value) is not None


def raw_number(value: object) -> object:
    """保留数值和数字字符串，拒绝值只保留其类型或非有限标记。"""
    if not isinstance(value, str):
        return value
    try:
        float(value)
    except (ValueError, OverflowError):
        return "<非数值字符串>"
    return value


def exception_issue(exc: BaseException, message: str) -> Issue:
    if isinstance(exc, TimeoutError):
        return Issue(code="timeout", message=message)
    code = exc.code.value if isinstance(exc, AegisError) else type(exc).__name__
    return Issue(code=code, message=message)


@dataclass
class SourceProbe:
    inputs: Sample = field(default_factory=dict)
    received: int | None = None
    input_samples: list[Sample] = field(default_factory=list)
    issues: dict[str, Issue] = field(default_factory=dict)

    def capture(self, count: int = 1, **values: object) -> None:
        self.received = (self.received or 0) + count
        if values and len(self.input_samples) < SAMPLE_LIMIT:
            self.input_samples.append(safe_record(**values))

    def note(self, code: str, message: str, *, count: int = 1, **values: object) -> None:
        issue = self.issues.get(code)
        if issue is None:
            issue = self.issues[code] = Issue(code=code, message=message, count=0)
        issue.count += count
        if values and len(issue.samples) < SAMPLE_LIMIT:
            issue.samples.append(safe_record(**values))


source_probe: ContextVar[SourceProbe | None] = ContextVar("telemetry_source_probe", default=None)


def quality_counts(readings: list[TelemetryReading]) -> dict[str, int]:
    flags = Counter(reading.quality_flag for reading in readings)
    return {name: flags[name] for name in ("ok", "suspect", "missing", "drift")}
