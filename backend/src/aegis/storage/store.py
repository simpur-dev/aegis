"""运行态存储：遥测 / 预警 / 任务单元 / 链路结果 的有界内存存储 + 本层的存储面。

内存实现（`PlatformStore`）与持久实现（`aegis.persistence.PostgresStore`）都满足这里声明的
`StoreProtocol`：调用方只依赖面，换后端不改调用方。内存实现保留为**读视图**——
热路径读要零跳数，落库交给持久层的有界写缓冲异步补写。
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Generic, Protocol, TypeVar, runtime_checkable

from aegis.domain.chain import ChainResult
from aegis.domain.messages import StandardizedTaskUnit, TelemetryReading, WarningRecord, parse_iso
from aegis.domain.operator import Operator

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
        # 站点存在性台账：station_id -> region_code（首次见到该站读数时登记）。
        # 读数被有界淘汰时这条登记**不撤回**——"这个站报过数"是发生过的事实，不该随窗口滑动消失。
        self._stations: dict[str, str] = {}

    async def add(self, readings: Iterable[TelemetryReading]) -> int:
        items = list(readings)
        for reading in items:
            if reading.station_id:
                self._stations.setdefault(reading.station_id, reading.region_code or "")
        return await self._collection.add_many(items)

    def observed_stations(self) -> list[tuple[str, str]]:
        """报过数的站点，按 station_id 升序：这是"清单"的唯一稳定顺序口径。"""
        return sorted(self._stations.items())

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
    """预警读视图。同 ID 覆盖而不是追加：发布/撤回/改级都写同一 `warning_id`，
    追加会让列表里出现两条同 ID 记录（`revoke` 之后前端会看到"已撤回"和"已发布"并存）。
    """

    def __init__(self, maxlen: int = 5_000) -> None:
        self._order: deque[str] = deque(maxlen=maxlen)
        self._index: dict[str, WarningRecord] = {}

    async def put(self, record: WarningRecord) -> None:
        if record.warning_id in self._index:
            self._index[record.warning_id] = record
            return
        self._index[record.warning_id] = record
        self._order.append(record.warning_id)

    def get(self, warning_id: str) -> WarningRecord | None:
        return self._index.get(warning_id)

    def list(self, *, limit: int = 50, region_code: str | None = None, released: bool | None = None) -> list[WarningRecord]:
        """`released` 由 `released_at` 推导：`WarningRecord` 没有 status 字段，也不新增
        （契约不动），"已发布/未发布"本来就只有一个真源。"""
        rows = [self._index[wid] for wid in self._order if wid in self._index]
        if region_code:
            rows = [r for r in rows if region_code in r.region_codes]
        if released is not None:
            rows = [r for r in rows if bool(r.released_at) is released]
        return rows[-limit:]

    @property
    def size(self) -> int:
        return len(self._order)


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

    def list(self, *, limit: int = 50, region_code: str | None = None) -> list[StandardizedTaskUnit]:
        """按最近写入顺序列出任务单元。

        只支持 STU 自身有的字段：STU v1 契约里没有 `status`/`assignee`（处置状态在
        `handling_tasks`），所以这里不造这两个参数——要按它们过滤是 `task.list` 动作的事。
        """
        rows = self._collection.snapshot()
        if region_code:
            rows = [u for u in rows if u.region_code == region_code]
        return rows[-limit:]

    @property
    def size(self) -> int:
        return len(self._collection)


def station_ledger_row(
    station_id: str,
    region_code: str = "",
    *,
    name_zh: str = "",
    hazard_focus: Sequence[str] = (),
    elevation_m: float | None = None,
    lon: float | None = None,
    lat: float | None = None,
    geom: str | None = None,
) -> dict[str, object]:
    """站点清单的统一行形状。

    两个后端（内存视图与 PostgreSQL 维表）必须逐键一致：字段缺失就写 None/空表，
    少一列和多一列都会让前端把"没有坐标"和"这一行不存在"混成同一件事。
    """
    return {
        "station_id": station_id,
        "name_zh": name_zh,
        "region_code": region_code,
        "hazard_focus": list(hazard_focus),
        "elevation_m": elevation_m,
        "lon": lon,
        "lat": lat,
        "geom": geom,
    }


class ControlActionStore:
    """动作执行记录的内存实现。

    与 `persistence/control_actions.py` 同一套状态机与不变量，只是持久介质不同：
    生产用 SQL（跨重启），内存形态用于开发与测试。**它不提供跨重启存活**——
    这一点在能力面里如实说（`control_actions_persistent: false`），不假装。
    """

    def __init__(self, maxlen: int = 2_000) -> None:
        self._rows: dict[str, dict[str, Any]] = {}
        self._order: deque[str] = deque(maxlen=maxlen)
        self._idempotency: dict[str, str] = {}

    @staticmethod
    def _now() -> datetime:
        return datetime.now(UTC)

    @staticmethod
    def _iso(value: datetime | None) -> str | None:
        return None if value is None else value.isoformat()

    def _public(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            **row,
            "expires_at": self._iso(row["expires_at"]),
            "created_at": self._iso(row["created_at"]),
            "updated_at": self._iso(row["updated_at"]),
        }

    async def create_action(
        self,
        *,
        action_id: str,
        operator_id: str,
        action: str,
        space_id: str | None,
        utterance: str,
        resolved_args: dict[str, Any],
        impact_scope: dict[str, Any],
        expires_in_seconds: int,
        idempotency_key: str | None,
    ) -> tuple[dict[str, Any], bool]:
        if idempotency_key and (existing_id := self._idempotency.get(idempotency_key)):
            existing = self._rows.get(existing_id)
            if existing is not None:
                return self._public(existing), True
        now = self._now()
        row: dict[str, Any] = {
            "action_id": action_id,
            "operator_id": operator_id,
            "action": action,
            "space_id": space_id,
            "utterance": utterance[:500],
            "resolved_args": dict(resolved_args),
            "status": "proposed",
            "idempotency_key": idempotency_key,
            "impact_scope": dict(impact_scope),
            "result": None,
            "error": None,
            "expires_at": now + timedelta(seconds=int(expires_in_seconds)),
            "created_at": now,
            "updated_at": now,
        }
        self._rows[action_id] = row
        self._order.append(action_id)
        while len(self._rows) > (self._order.maxlen or 0):
            oldest = self._order.popleft()
            dropped = self._rows.pop(oldest, None)
            if dropped is not None and dropped["idempotency_key"]:
                self._idempotency.pop(dropped["idempotency_key"], None)
        if idempotency_key:
            self._idempotency[idempotency_key] = action_id
        return self._public(row), False

    async def confirm_action(self, action_id: str) -> dict[str, Any] | None:
        row = self._rows.get(action_id)
        if row is None or row["status"] != "proposed":
            return None
        if row["expires_at"] is not None and row["expires_at"] < self._now():
            return None
        row["status"] = "executing"
        row["updated_at"] = self._now()
        return self._public(row)

    async def mark_action_executed(self, action_id: str, result: dict[str, Any]) -> dict[str, Any] | None:
        row = self._rows.get(action_id)
        if row is None or row["status"] != "executing":
            return None
        row["status"] = "executed"
        row["result"] = dict(result)
        row["error"] = None
        row["updated_at"] = self._now()
        return self._public(row)

    async def mark_action_failed(self, action_id: str, error: dict[str, Any]) -> dict[str, Any] | None:
        row = self._rows.get(action_id)
        if row is None or row["status"] != "executing":
            return None
        row["status"] = "failed"
        row["error"] = dict(error)
        row["updated_at"] = self._now()
        return self._public(row)

    async def revoke_action(self, action_id: str) -> dict[str, Any] | None:
        row = self._rows.get(action_id)
        if row is None or row["status"] not in {"proposed", "executed"}:
            return None
        if self._now() - row["updated_at"] > timedelta(seconds=60):
            return None
        row["status"] = "revoked"
        row["updated_at"] = self._now()
        return self._public(row)

    async def get_action(self, action_id: str) -> dict[str, Any] | None:
        row = self._rows.get(action_id)
        return None if row is None else self._public(row)

    async def list_actions(self, *, operator_id: str | None = None, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        capped = max(1, min(int(limit), 200))
        rows = [self._rows[aid] for aid in self._order if aid in self._rows]
        if operator_id:
            rows = [row for row in rows if row["operator_id"] == operator_id]
        if status:
            rows = [row for row in rows if row["status"] == status]
        return [self._public(row) for row in reversed(rows[-capped:])]

    async def expire_actions(self) -> int:
        now = self._now()
        expired = 0
        for row in self._rows.values():
            if row["status"] == "proposed" and row["expires_at"] is not None and row["expires_at"] < now:
                row["status"] = "expired"
                row["updated_at"] = now
                expired += 1
        return expired

    async def recover_stuck_actions(self) -> int:
        now = self._now()
        recovered = 0
        for row in self._rows.values():
            if row["status"] == "executing" and now - row["updated_at"] > timedelta(seconds=600):
                row["status"] = "failed"
                row["error"] = {"code": "process_restart", "message": "执行中进程重启，结果未知"}
                row["updated_at"] = now
                recovered += 1
        return recovered


class PlatformStore:
    """平台运行态聚合入口。"""

    def __init__(self, *, telemetry_maxlen: int = 50_000) -> None:
        self.telemetry = TelemetryStore(telemetry_maxlen)
        self.warnings = WarningStore()
        self.tasks = TaskStore()
        self.chains: BoundedCollection[ChainResult] = BoundedCollection(2_000)
        # 动作执行记录（内存态）：写动作的"待确认 → 执行"在这里落账，重启即失——
        # 能力面用 `control_actions_persistent: false` 如实标注，不假装跨重启。
        self.control_actions = ControlActionStore()
        # 站点维表（内存态）：与 PostgreSQL 那份同构，由 `scripts/import_stations.py` 显式写入。
        # 内存视图没有持久层，导入只活到进程结束——这一条由导入命令在结果里说清楚。
        self._station_dim: dict[str, dict[str, object]] = {}

    async def upsert_station(
        self,
        station_id: str,
        region_code: str,
        lon: float | None,
        lat: float | None,
        *,
        name_zh: str = "",
        hazard_focus: Sequence[str] = (),
        elevation_m: float | None = None,
    ) -> None:
        """登记/更新一个站点。与 PostgresStore 同签名同语义，让同一套台账能导入任一端。

        成对坐标是这里唯一的硬校验：只给一半会被 Postgres 侧拒掉，
        两端都在写入前把关，才不会等到现场导入时才发现一半的行了。
        """
        if not station_id.strip():
            raise ValueError("station_id 不能为空")
        if not region_code.strip():
            raise ValueError("region_code 不能为空")
        if (lon is None) != (lat is None):
            raise ValueError("坐标必须成对给出")
        geom = None if lon is None or lat is None else f"SRID=4326;POINT({lon} {lat})"
        self._station_dim[station_id] = {
            "region_code": region_code,
            "name_zh": name_zh,
            "hazard_focus": list(hazard_focus),
            "elevation_m": elevation_m,
            "lon": lon,
            "lat": lat,
            "geom": geom,
        }

    async def list_stations(self, *, region_code: str | None = None, limit: int = 500) -> list[dict[str, object]]:
        """站点清单（内存视图）：维表行优先，再用"报过数但没登记"的站点补齐。

        维表没有的字段就留 None/空表——绝不拿遥字段拼一个看似完整的站点出来。
        缺坐标的条目由前端归入"未定位"列表，而不是从地图上凭空消失。
        """
        if limit <= 0:
            raise ValueError("limit 必须为正")
        rows: list[dict[str, object]] = []
        seen: set[str] = set()
        for station_id, dim in sorted(self._station_dim.items()):
            if region_code and dim["region_code"] != region_code:
                continue
            seen.add(station_id)
            fields = {key: value for key, value in dim.items() if key != "region_code"}
            rows.append(station_ledger_row(station_id, str(dim["region_code"]), **fields))  # type: ignore[arg-type]
        for station_id, region in self.telemetry.observed_stations():
            if station_id in seen or (region_code and region != region_code):
                continue
            rows.append(station_ledger_row(station_id, region))
        return rows[:limit]

    # ---------- 动作执行记录（可选能力面：SupportsControlActions） ----------

    async def create_action(self, **kwargs: Any) -> tuple[dict[str, Any], bool]:
        return await self.control_actions.create_action(**kwargs)

    async def confirm_action(self, action_id: str) -> dict[str, Any] | None:
        return await self.control_actions.confirm_action(action_id)

    async def mark_action_executed(self, action_id: str, result: dict[str, Any]) -> dict[str, Any] | None:
        return await self.control_actions.mark_action_executed(action_id, result)

    async def mark_action_failed(self, action_id: str, error: dict[str, Any]) -> dict[str, Any] | None:
        return await self.control_actions.mark_action_failed(action_id, error)

    async def revoke_action(self, action_id: str) -> dict[str, Any] | None:
        return await self.control_actions.revoke_action(action_id)

    async def get_action(self, action_id: str) -> dict[str, Any] | None:
        return await self.control_actions.get_action(action_id)

    async def list_actions(self, *, operator_id: str | None = None, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        return await self.control_actions.list_actions(operator_id=operator_id, status=status, limit=limit)

    async def expire_actions(self) -> int:
        return await self.control_actions.expire_actions()

    async def recover_stuck_actions(self) -> int:
        return await self.control_actions.recover_stuck_actions()

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


# ---------- 存储面：调用方只依赖这张面，不依赖具体实现 ----------
#
# 放在这里而不是持久层适配器里，是本模块头那句承诺的兑现：抽象归被依赖的一侧，
# 换成 PostgreSQL 实现时上游代码不必改动，也不该被迫 import 某个具体后端。


class TelemetryPort(Protocol):
    async def add(self, readings: Iterable[TelemetryReading]) -> int: ...

    def query(
        self,
        *,
        station_id: str | None = None,
        metric: str | None = None,
        region_code: str | None = None,
        since: datetime | str | None = None,
        until: datetime | str | None = None,
        limit: int = 500,
    ) -> list[TelemetryReading]: ...

    @property
    def size(self) -> int: ...


class WarningPort(Protocol):
    async def put(self, record: WarningRecord) -> None: ...

    def get(self, warning_id: str) -> WarningRecord | None: ...

    def list(self, *, limit: int = 50, region_code: str | None = None, released: bool | None = None) -> list[WarningRecord]: ...

    @property
    def size(self) -> int: ...


class TaskPort(Protocol):
    async def put_many(self, units: Iterable[StandardizedTaskUnit]) -> int: ...

    def get(self, task_unit_id: str) -> StandardizedTaskUnit | None: ...

    def by_event(self, event_id: str) -> list[StandardizedTaskUnit]: ...

    def list(self, *, limit: int = 50, region_code: str | None = None) -> list[StandardizedTaskUnit]: ...

    @property
    def size(self) -> int: ...


class StoreProtocol(Protocol):
    """存储面。四个子集合声明为只读：调用方只取用、从不替换，
    写成可写属性会让协议变成不变匹配，内存实现反而过不了类型检查。"""

    @property
    def telemetry(self) -> TelemetryPort: ...

    @property
    def warnings(self) -> WarningPort: ...

    @property
    def tasks(self) -> TaskPort: ...

    @property
    def chains(self) -> BoundedCollection[ChainResult]: ...

    async def record_chain(self, result: ChainResult) -> None: ...

    async def list_stations(self, *, region_code: str | None = None, limit: int = 500) -> list[dict[str, object]]: ...

    async def upsert_station(
        self,
        station_id: str,
        region_code: str,
        lon: float | None,
        lat: float | None,
        *,
        name_zh: str = "",
        hazard_focus: Sequence[str] = (),
        elevation_m: float | None = None,
    ) -> None: ...

    def snapshot(self) -> dict[str, object]: ...


@runtime_checkable
class SupportsGeoQueries(Protocol):
    """几何查询能力：半径内站点、面内站点、轨迹面汇总。

    这是一个**可选能力面**，不并进 `StoreProtocol`：内存实现没有 PostGIS，硬要"也能查"
    就得另养一份 haversine / 射线法，于是同一个"这个站在不在范围内"有了两套口径，
    而验收时看不出差别。缺能力必须可判别，而不是被近似值冒充。

    几何口径由 `aegis.persistence.geo` 唯一确定（geography 类型、SRID=4326、经度在前）。
    """

    async def stations_within(self, *, lon: float, lat: float, radius_m: float, limit: int = 50) -> list[dict[str, Any]]: ...

    async def stations_in_polygon(self, *, polygon_wkt: str, region_code: str | None = None) -> list[dict[str, Any]]: ...

    async def hazard_trace_summary(
        self, *, polygon_wkt: str, since: datetime, until: datetime | None = None, hazard_type: str | None = None
    ) -> dict[str, Any]: ...


def geo_query_port(store: object) -> SupportsGeoQueries | None:
    """能力发现：三个几何查询都在才算支持。

    用 `isinstance` 对 runtime_checkable 协议做结构检查，只认方法存在性——签名漂移
    由 `tests/api/test_geo_endpoints.py` 里对着 `PostgresStore` 的那条用例兜住。
    """
    return store if isinstance(store, SupportsGeoQueries) else None


@runtime_checkable
class SupportsRuleLibrary(Protocol):
    """触发条件规则库（批次 C2）的可选能力面。

    不进 `StoreProtocol`：内存读视图没有"版本化阈值"这件事，内置种子就是唯一版本；
    把两个后端的差别变成协议上的一行，比让内存实现假装能查版本要诚实。
    """

    async def trigger_rules(self, *, status: str = "active", hazard_type: str | None = None, limit: int = 200) -> list[dict[str, Any]]: ...


def rule_library_port(store: object) -> SupportsRuleLibrary | None:
    return store if isinstance(store, SupportsRuleLibrary) else None


@runtime_checkable
class SupportsThresholdSuggestions(Protocol):
    """阈值建议台账（§3.5 后半截）的可选能力面：入账、读清单、确认落地、驳回。

    同样不进 `StoreProtocol`——理由与规则库那一面相同：内存读视图里"确认之后写一个新版本"
    没有对应的事实可写。让内存实现假装收下建议，表现就是值班员按了确认、台账上写着已确认，
    而判据一个字都没变。缺这条能力时端点如实 503。
    """

    async def put_threshold_suggestion(self, row: Mapping[str, Any]) -> dict[str, Any]: ...

    async def threshold_suggestions(self, *, status: str = "pending_human_review", limit: int = 100) -> list[dict[str, Any]]: ...

    async def confirm_threshold_suggestion(
        self,
        *,
        suggestion_id: str,
        rule_id: str,
        metric: str,
        threshold: float,
        decided_by: str,
        note: str = "",
        expected_version: int,
    ) -> dict[str, Any]: ...

    async def reject_threshold_suggestion(self, *, suggestion_id: str, decided_by: str, note: str = "") -> dict[str, Any]: ...


def suggestion_ledger_port(store: object) -> SupportsThresholdSuggestions | None:
    return store if isinstance(store, SupportsThresholdSuggestions) else None


@runtime_checkable
class SupportsOperators(Protocol):
    """席位台账的可选能力面：只有持久层实现。

    内存读视图不实现这一面——席位是"谁在班上"的业务真源，重启即失的内存 dict
    会让权限判定在重启后变成"所有人都不存在"。缺这条能力时端点如实 503。
    """

    async def upsert_operator(self, operator: Operator) -> Operator: ...

    async def create_operator(self, operator: Operator) -> Operator | None:
        """不存在才创建；已存在返回 None（并发下也只有一个成功）。"""
        ...

    async def get_operator(self, operator_id: str) -> Operator | None: ...

    async def list_operators(self, *, active_only: bool = True, limit: int = 200) -> list[Operator]: ...


def operator_ledger_port(store: object) -> SupportsOperators | None:
    return store if isinstance(store, SupportsOperators) else None


@runtime_checkable
class SupportsControlActions(Protocol):
    """动作执行记录的可选能力面：只有持久层实现。

    内存形态没有这张表——写动作的"待确认 → 执行"必须跨重启存活，内存 dict 做不到，
    所以缺这条能力时写动作如实拒绝，而不是退化成"重启就丢"的内存 pending。
    """

    async def create_action(
        self,
        *,
        action_id: str,
        operator_id: str,
        action: str,
        space_id: str | None,
        utterance: str,
        resolved_args: dict[str, Any],
        impact_scope: dict[str, Any],
        expires_in_seconds: int,
        idempotency_key: str | None,
    ) -> tuple[dict[str, Any], bool]: ...

    async def confirm_action(self, action_id: str) -> dict[str, Any] | None: ...

    async def mark_action_executed(self, action_id: str, result: dict[str, Any]) -> dict[str, Any] | None: ...

    async def mark_action_failed(self, action_id: str, error: dict[str, Any]) -> dict[str, Any] | None: ...

    async def revoke_action(self, action_id: str) -> dict[str, Any] | None: ...

    async def get_action(self, action_id: str) -> dict[str, Any] | None: ...

    async def list_actions(self, *, operator_id: str | None = None, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]: ...

    async def expire_actions(self) -> int: ...

    async def recover_stuck_actions(self) -> int: ...


def control_action_port(store: object) -> SupportsControlActions | None:
    return store if isinstance(store, SupportsControlActions) else None


def _to_iso(moment: datetime | str | None) -> str | None:
    if moment is None:
        return None
    if isinstance(moment, str):
        return parse_iso(moment).isoformat().replace("+00:00", "Z")
    return moment.isoformat().replace("+00:00", "Z")
