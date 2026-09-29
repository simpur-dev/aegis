"""运行态存储：遥测 / 预警 / 任务单元 / 链路结果 的有界内存存储。

选型说明：M1 用内存存储换取零外部依赖与可复现的单测；SQLAlchemy/TimescaleDB 持久化
在 M2 与 workflow 引擎一同引入（接口 `StoreProtocol` 保持不变，替换实现即可）。
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Iterable
from datetime import datetime
from typing import Generic, TypeVar

from aegis.domain.messages import StandardizedTaskUnit, TelemetryReading, WarningRecord, parse_iso
from aegis.pipeline.chain import ChainResult

T = TypeVar("T")


class BoundedCollection(Generic[T]):
    """线程安全（单事件循环 + Lock）的有界追加集合，读侧返回快照避免迭代期变更。"""

    def __init__(self, maxlen: int = 10_000) -> None:
        if maxlen <= 0:
            raise ValueError("maxlen 必须为正")
        self._items: deque[T] = deque(maxlen=maxlen)
        self._lock = asyncio.Lock()

    async def add(self, item: T) -> None:
        async with self._lock:
            self._items.append(item)

    async def add_many(self, items: Iterable[T]) -> int:
        async with self._lock:
            count = 0
            for item in items:
                self._items.append(item)
                count += 1
            return count

    def snapshot(self) -> list[T]:
        return list(self._items)

    def latest(self, count: int = 10) -> list[T]:
        if count <= 0:
            raise ValueError("count 必须为正")
        return list(self._items)[-count:]

    def __len__(self) -> int:
        return len(self._items)


class TelemetryStore:
    def __init__(self, maxlen: int = 50_000) -> None:
        self._collection: BoundedCollection[TelemetryReading] = BoundedCollection(maxlen)

    async def add(self, readings: Iterable[TelemetryReading]) -> int:
        return await self._collection.add_many(readings)

    def query(
        self,
        *,
        station_id: str | None = None,
        metric: str | None = None,
        region_code: str | None = None,
        since: datetime | str | None = None,
        until: datetime | str | None = None,
        limit: int = 500,
    ) -> list[TelemetryReading]:
        if limit <= 0:
            raise ValueError("limit 必须为正")
        since_iso = _to_iso(since)
        until_iso = _to_iso(until)
        rows = []
        for reading in self._collection.snapshot():
            if station_id and reading.station_id != station_id:
                continue
            if metric and reading.metric != metric:
                continue
            if region_code and reading.region_code != region_code:
                continue
            if since_iso and reading.observed_at < since_iso:
                continue
            if until_iso and reading.observed_at > until_iso:
                continue
            rows.append(reading)
        return rows[-limit:]

    @property
    def size(self) -> int:
        return len(self._collection)


class WarningStore:
    def __init__(self, maxlen: int = 5_000) -> None:
        self._collection: BoundedCollection[WarningRecord] = BoundedCollection(maxlen)
        self._index: dict[str, WarningRecord] = {}

    async def put(self, record: WarningRecord) -> None:
        await self._collection.add(record)
        self._index[record.warning_id] = record

    def get(self, warning_id: str) -> WarningRecord | None:
        return self._index.get(warning_id)

    def list(self, *, limit: int = 50, region_code: str | None = None) -> list[WarningRecord]:
        rows = self._collection.snapshot()
        if region_code:
            rows = [r for r in rows if region_code in r.region_codes]
        return rows[-limit:]

    @property
    def size(self) -> int:
        return len(self._collection)


class TaskStore:
    def __init__(self, maxlen: int = 20_000) -> None:
        self._collection: BoundedCollection[StandardizedTaskUnit] = BoundedCollection(maxlen)
        self._index: dict[str, StandardizedTaskUnit] = {}

    async def put_many(self, units: Iterable[StandardizedTaskUnit]) -> int:
        items = list(units)
        for unit in items:
            self._index[unit.task_unit_id] = unit
        return await self._collection.add_many(items)

    def get(self, task_unit_id: str) -> StandardizedTaskUnit | None:
        return self._index.get(task_unit_id)

    def by_event(self, event_id: str) -> list[StandardizedTaskUnit]:
        return [u for u in self._collection.snapshot() if u.event_id == event_id]

    @property
    def size(self) -> int:
        return len(self._collection)


class PlatformStore:
    """平台运行态聚合入口。"""

    def __init__(self, *, telemetry_maxlen: int = 50_000) -> None:
        self.telemetry = TelemetryStore(telemetry_maxlen)
        self.warnings = WarningStore()
        self.tasks = TaskStore()
        self.chains: BoundedCollection[ChainResult] = BoundedCollection(2_000)

    async def record_chain(self, result: ChainResult) -> None:
        await self.chains.add(result)
        await self.tasks.put_many(result.task_units)
        if result.warning is not None:
            await self.warnings.put(result.warning)

    def snapshot(self) -> dict[str, object]:
        return {
            "telemetry_count": self.telemetry.size,
            "warning_count": self.warnings.size,
            "task_count": self.tasks.size,
            "chain_count": len(self.chains),
        }


def _to_iso(moment: datetime | str | None) -> str | None:
    if moment is None:
        return None
    if isinstance(moment, str):
        return parse_iso(moment).isoformat().replace("+00:00", "Z")
    return moment.isoformat().replace("+00:00", "Z")
