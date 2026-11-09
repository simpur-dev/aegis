"""运行态存储：遥测 / 预警 / 任务单元 / 链路结果 的有界内存存储 + 本层的存储面。

内存实现（`PlatformStore`）与持久实现（`aegis.persistence.PostgresStore`）都满足这里声明的
`StoreProtocol`：调用方只依赖面，换后端不改调用方。内存实现保留为**读视图**——
热路径读要零跳数，落库交给持久层的有界写缓冲异步补写。
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Generic, Protocol, TypeVar, runtime_checkable

from aegis.domain.alert import ACTIONABLE_STATUSES, OPEN_STATUSES
from aegis.domain.chain import ChainResult
from aegis.domain.messages import StandardizedTaskUnit, TelemetryReading, WarningRecord, parse_iso
from aegis.domain.operator import Operator
from aegis.domain.site import SiteProfile
from aegis.domain.task import ACKABLE_STATUSES, CLAIMABLE_STATUSES, OPEN_TASK_STATUSES
from aegis.storage.site_profiles import SiteProfileStore

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

    def list(
        self,
        *,
        limit: int = 50,
        region_code: str | None = None,
        released: bool | None = None,
        status: str | None = None,
    ) -> list[WarningRecord]:
        """`status` 读 `WarningRecord.status`（与落库派生列同源）；`released` 只看 `released_at`。

        撤销不改写 `released_at`，所以"已发布"与"已撤销"可以同时成立：两个参数各有用途，
        不能用其中一个代替另一个。
        """
        rows = [self._index[wid] for wid in self._order if wid in self._index]
        if region_code:
            rows = [r for r in rows if region_code in r.region_codes]
        if released is not None:
            rows = [r for r in rows if bool(r.released_at) is released]
        if status is not None:
            rows = [r for r in rows if r.status == status]
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

    def __init__(self, maxlen: int = 2_000, *, audit_maxlen: int = 10_000) -> None:
        self._rows: dict[str, dict[str, Any]] = {}
        self._order: deque[str] = deque(maxlen=maxlen)
        self._idempotency: dict[str, str] = {}
        # 审计流水与动作记录分开有界：审计一行比动作一行小，但写入频次更高（每次写动作两行）。
        self._audit: deque[dict[str, Any]] = deque(maxlen=audit_maxlen)
        # 审计行的自增序号与时间戳：SQL 侧由 bigserial 与 DEFAULT now() 给，
        # 内存侧必须自己补——没有它们，读面（§14.3）连"最近在前"都排不出来。
        self._next_audit_id = 1

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
        review_required: int = 0,
        audit: AuditRecord | None = None,
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
            "review_required": int(review_required),
            "reviewers": [],
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
        # 只有"新建成功"才补审计行：幂等重放路径在上面已提前返回，不会走到这里。
        if audit is not None:
            self._audit.append(audit.as_row())
        return self._public(row), False

    async def confirm_action(self, action_id: str) -> dict[str, Any] | None:
        row = self._rows.get(action_id)
        if row is None or row["status"] != "proposed":
            return None
        if row["expires_at"] is not None and row["expires_at"] < self._now():
            return None
        # 与 SQL 侧同一道闸：复核票不够时不让本人一次确认绕过双人复核。
        if len(row["reviewers"]) < int(row["review_required"]):
            return None
        row["status"] = "executing"
        row["updated_at"] = self._now()
        return self._public(row)

    async def review_action(self, action_id: str, reviewer: dict[str, Any]) -> tuple[dict[str, Any], bool] | None:
        """内存形态的双人复核：与 `persistence/control_actions.py::review_action` 同一套不变量。

        纯 asyncio 单线程下没有真并发，但"两票不同人 + 第二票才迁移"的语义必须逐条对齐，
        否则开发与生产会表现出两套确认规则。
        """
        row = self._rows.get(action_id)
        if row is None or row["status"] != "proposed" or int(row["review_required"]) < 1:
            return None
        if row["expires_at"] is not None and row["expires_at"] < self._now():
            return None
        operator_id = str(reviewer["operator_id"])
        if any(str(vote.get("operator_id")) == operator_id for vote in row["reviewers"]):
            return None
        row["reviewers"] = [*row["reviewers"], dict(reviewer)]
        ready = len(row["reviewers"]) >= int(row["review_required"])
        row["status"] = "executing" if ready else "proposed"
        row["updated_at"] = self._now()
        return self._public(row), ready

    async def mark_action_executed(
        self, action_id: str, result: dict[str, Any], *, audit: AuditRecord | None = None
    ) -> dict[str, Any] | None:
        row = self._rows.get(action_id)
        if row is None or row["status"] != "executing":
            return None
        row["status"] = "executed"
        row["result"] = dict(result)
        row["error"] = None
        row["updated_at"] = self._now()
        if audit is not None:
            self._audit.append(audit.as_row())
        return self._public(row)

    async def mark_action_failed(self, action_id: str, error: dict[str, Any], *, audit: AuditRecord | None = None) -> dict[str, Any] | None:
        row = self._rows.get(action_id)
        if row is None or row["status"] != "executing":
            return None
        row["status"] = "failed"
        row["error"] = dict(error)
        row["updated_at"] = self._now()
        if audit is not None:
            self._audit.append(audit.as_row())
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

    async def record_audit(self, audit: AuditRecord) -> None:
        """独立审计行（不伴随状态迁移）：越权被拒、直接执行写动作的 intent/result 都走这里。

        `id` 与 `ts` 在这里补上——SQL 侧由 bigserial 与 `DEFAULT now()` 给，
        内存侧自己补，否则读面（§14.3）排不出"最近在前"。
        """
        row = audit.as_row()
        row["id"] = self._next_audit_id
        self._next_audit_id += 1
        row["ts"] = self._now().isoformat()
        self._audit.append(row)

    def audit_snapshot(self) -> list[dict[str, Any]]:
        """测试与对账读审计流水：内存形态没有 SQL 可查，给一个只读出口。"""
        return list(self._audit)


class AlertStore:
    """告警实例的内存实现。

    与 `persistence/alerts.py` 同一套状态机与不变量，只是介质不同：
    - 去重键是 `warning_id`：同一预警重复广播不会长出第二张卡；
    - 接手不改 `ack_by` 之外的任何东西，转派只改 `assigned_to`；
    - 升级只命中未确认、非演练、未被稍后提醒挡住的记录。

    它**不提供跨重启存活**：内存形态用于开发与测试，生产用 SQL 那份，
    能力面里如实标注 `alerts_persistent: false`，不假装。
    """

    def __init__(self, maxlen: int = 2_000) -> None:
        self._rows: dict[str, dict[str, Any]] = {}
        self._by_warning: dict[str, str] = {}
        self._order: deque[str] = deque(maxlen=maxlen)

    @staticmethod
    def _now() -> datetime:
        return datetime.now(UTC)

    @staticmethod
    def _iso(value: datetime | None) -> str | None:
        return None if value is None else value.isoformat()

    def _public(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            **row,
            "ack_at": self._iso(row["ack_at"]),
            "snooze_until": self._iso(row["snooze_until"]),
            "escalated_at": self._iso(row["escalated_at"]),
            "created_at": self._iso(row["created_at"]),
        }

    def _evict(self) -> None:
        while len(self._rows) > (self._order.maxlen or 0):
            oldest = self._order.popleft()
            dropped = self._rows.pop(oldest, None)
            if dropped is not None:
                self._by_warning.pop(dropped["warning_id"], None)

    async def raise_alert(
        self,
        *,
        alert_id: str,
        warning_id: str,
        level: int,
        region_code: str,
        drill: bool,
    ) -> tuple[dict[str, Any], bool]:
        existing_id = self._by_warning.get(warning_id)
        if existing_id is not None and existing_id in self._rows:
            return self._public(self._rows[existing_id]), False
        row: dict[str, Any] = {
            "alert_id": alert_id,
            "warning_id": warning_id,
            "level": int(level),
            "region_code": region_code,
            "status": "raised",
            "ack_by": None,
            "ack_at": None,
            "assigned_to": None,
            "snooze_until": None,
            "escalated_at": None,
            "drill": bool(drill),
            "created_at": self._now(),
        }
        self._rows[alert_id] = row
        self._by_warning[warning_id] = alert_id
        self._order.append(alert_id)
        self._evict()
        return self._public(row), True

    async def get_alert(self, alert_id: str) -> dict[str, Any] | None:
        row = self._rows.get(alert_id)
        return None if row is None else self._public(row)

    async def list_alerts(
        self,
        *,
        open_only: bool = True,
        status: str | None = None,
        level: int | None = None,
        drill: bool | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        capped = max(1, min(int(limit), 200))
        rows = [self._rows[aid] for aid in self._order if aid in self._rows]
        if status:
            rows = [row for row in rows if row["status"] == status]
        elif open_only:
            rows = [row for row in rows if row["status"] in OPEN_STATUSES]
        if level is not None:
            rows = [row for row in rows if row["level"] == int(level)]
        if drill is not None:
            rows = [row for row in rows if row["drill"] is bool(drill)]
        rows.sort(key=lambda row: (row["level"], row["created_at"]), reverse=False)
        return [self._public(row) for row in rows[:capped]]

    async def ack_alert(self, alert_id: str, operator_id: str) -> dict[str, Any] | None:
        row = self._rows.get(alert_id)
        if row is None or row["status"] not in ACTIONABLE_STATUSES:
            return None
        now = self._now()
        row["status"] = "acked"
        row["ack_by"] = operator_id
        row["ack_at"] = now
        row["assigned_to"] = row["assigned_to"] or operator_id
        return self._public(row)

    async def transfer_alert(self, alert_id: str, assignee: str) -> dict[str, Any] | None:
        row = self._rows.get(alert_id)
        if row is None or row["status"] not in OPEN_STATUSES:
            return None
        row["assigned_to"] = assignee
        return self._public(row)

    async def snooze_alert(self, alert_id: str, minutes: int) -> dict[str, Any] | None:
        row = self._rows.get(alert_id)
        if row is None or row["status"] not in OPEN_STATUSES:
            return None
        row["snooze_until"] = self._now() + timedelta(minutes=int(minutes))
        return self._public(row)

    async def snooze_by_warning(self, warning_id: str, minutes: int) -> dict[str, Any] | None:
        """按预警静默，与 SQL 版同一不变量：只改 snooze_until，状态不变。"""
        alert_id = self._by_warning.get(warning_id)
        row = self._rows.get(alert_id) if alert_id else None
        if row is None or row["status"] not in OPEN_STATUSES:
            return None
        row["snooze_until"] = self._now() + timedelta(minutes=int(minutes))
        return self._public(row)

    async def escalate_due(self, *, after_minutes: int) -> list[dict[str, Any]]:
        now = self._now()
        cutoff = timedelta(minutes=int(after_minutes))
        escalated: list[dict[str, Any]] = []
        for row in self._rows.values():
            if row["status"] not in {"raised", "delivered"} or row["drill"]:
                continue
            if now - row["created_at"] < cutoff:
                continue
            if row["snooze_until"] is not None and row["snooze_until"] > now:
                continue
            row["status"] = "escalated"
            row["escalated_at"] = now
            escalated.append(self._public(row))
        return escalated


class HandlingTaskStore:
    """处置空间/任务的内存实现：与 `persistence/handling_tasks.py` 同一套不变量。

    `version` 是乐观锁：每次迁移都带 expected_version，命中不上就是"被别人抢先改过"。
    它不提供跨重启存活，能力面用 `collab_persistent: false` 如实标注。
    """

    def __init__(self, maxlen: int = 5_000) -> None:
        self._rows: dict[str, dict[str, Any]] = {}
        self._spaces: dict[str, dict[str, Any]] = {}
        self._space_order: deque[str] = deque(maxlen=maxlen)
        self._by_stu: dict[tuple[str, str], str] = {}
        self._order: deque[str] = deque(maxlen=maxlen)

    @staticmethod
    def _now() -> datetime:
        return datetime.now(UTC)

    @staticmethod
    def _iso(value: Any) -> str | None:
        return None if value is None else value.isoformat()

    def _public(self, row: dict[str, Any]) -> dict[str, Any]:
        return {**row, "updated_at": self._iso(row["updated_at"])}

    def _space_public(self, row: dict[str, Any]) -> dict[str, Any]:
        return {**row, "created_at": self._iso(row["created_at"]), "closed_at": self._iso(row["closed_at"])}

    async def ensure_space(self, *, space_id: str, warning_id: str) -> dict[str, Any]:
        if space_id not in self._spaces:
            # 内存形态也要有界：空间满了就丢最旧那个，并连带清掉它的任务，
            # 否则"任务有界、空间无界"会让长时间演练悄悄吃内存。
            if self._space_order.maxlen and len(self._spaces) >= self._space_order.maxlen:
                dropped = self._space_order.popleft()
                self._spaces.pop(dropped, None)
                for task_id, row in list(self._rows.items()):
                    if row["space_id"] == dropped:
                        self._rows.pop(task_id, None)
                        self._by_stu.pop((dropped, row["stu_id"]), None)
            self._spaces[space_id] = {
                "space_id": space_id,
                "warning_id": warning_id,
                "status": "open",
                "created_at": self._now(),
                "closed_at": None,
            }
            self._space_order.append(space_id)
        return self._space_public(self._spaces[space_id])

    async def get_space(self, space_id: str) -> dict[str, Any] | None:
        row = self._spaces.get(space_id)
        return None if row is None else self._space_public(row)

    async def list_spaces(self, *, open_only: bool = True, limit: int = 50) -> list[dict[str, Any]]:
        rows = [row for row in self._spaces.values() if not open_only or row["status"] == "open"]
        rows.sort(key=lambda row: row["created_at"], reverse=True)
        capped = max(1, min(int(limit), 200))
        summaries: list[dict[str, Any]] = []
        for row in rows[:capped]:
            tasks = [item for item in self._rows.values() if item["space_id"] == row["space_id"]]
            open_count = sum(1 for item in tasks if item["status"] != "closed")
            summaries.append(
                {
                    **self._space_public(row),
                    "tasks_total": len(tasks),
                    "tasks_open": open_count,
                    "tasks_closed": len(tasks) - open_count,
                }
            )
        return summaries

    async def ensure_task(self, *, space_id: str, stu_id: str) -> tuple[dict[str, Any], bool]:
        key = (space_id, stu_id)
        existing = self._by_stu.get(key)
        if existing is not None and existing in self._rows:
            return self._public(self._rows[existing]), False
        row: dict[str, Any] = {
            "task_id": stu_id,
            "space_id": space_id,
            "stu_id": stu_id,
            "assignee": None,
            "status": "pending",
            "version": 1,
            "updated_at": self._now(),
        }
        self._rows[stu_id] = row
        self._by_stu[key] = stu_id
        self._order.append(stu_id)
        while len(self._rows) > (self._order.maxlen or 0):
            dropped = self._rows.pop(self._order.popleft(), None)
            if dropped is not None:
                self._by_stu.pop((dropped["space_id"], dropped["stu_id"]), None)
        return self._public(row), True

    async def get_task(self, task_id: str) -> dict[str, Any] | None:
        row = self._rows.get(task_id)
        return None if row is None else self._public(row)

    async def list_tasks(
        self,
        *,
        space_id: str | None = None,
        assignee: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        rows = [self._rows[tid] for tid in self._order if tid in self._rows]
        if space_id:
            rows = [row for row in rows if row["space_id"] == space_id]
        if assignee:
            rows = [row for row in rows if row["assignee"] == assignee]
        if status:
            rows = [row for row in rows if row["status"] == status]
        rows.sort(key=lambda row: row["updated_at"], reverse=True)
        return [self._public(row) for row in rows[: max(1, min(int(limit), 200))]]

    async def _move(
        self,
        task_id: str,
        *,
        expected_version: int,
        allowed: frozenset[str],
        status: str | None = None,
        assignee: str | None = None,
    ) -> dict[str, Any] | None:
        row = self._rows.get(task_id)
        if row is None or row["status"] not in allowed or row["version"] != int(expected_version):
            return None
        row["version"] += 1
        row["updated_at"] = self._now()
        if status is not None:
            row["status"] = status
        if assignee is not None:
            row["assignee"] = assignee
        return self._public(row)

    async def claim_task(self, task_id: str, operator_id: str, expected_version: int) -> dict[str, Any] | None:
        return await self._move(
            task_id,
            expected_version=expected_version,
            allowed=CLAIMABLE_STATUSES,
            status="claimed",
            assignee=operator_id,
        )

    async def ack_task(self, task_id: str, operator_id: str, expected_version: int) -> dict[str, Any] | None:
        return await self._move(task_id, expected_version=expected_version, allowed=ACKABLE_STATUSES, status="acked")

    async def assign_task(self, task_id: str, assignee: str, expected_version: int) -> dict[str, Any] | None:
        return await self._move(task_id, expected_version=expected_version, allowed=OPEN_TASK_STATUSES, status="claimed", assignee=assignee)

    async def transfer_task(self, task_id: str, assignee: str, expected_version: int) -> dict[str, Any] | None:
        return await self._move(task_id, expected_version=expected_version, allowed=OPEN_TASK_STATUSES, assignee=assignee)

    async def close_task(self, task_id: str, expected_version: int) -> dict[str, Any] | None:
        return await self._move(task_id, expected_version=expected_version, allowed=OPEN_TASK_STATUSES, status="closed")


class NotificationStore:
    """站内通知的内存实现：与 `persistence/notifications.py` 同一套字段与已读语义。"""

    def __init__(self, maxlen: int = 2_000) -> None:
        self._rows: dict[int, dict[str, Any]] = {}
        self._order: deque[int] = deque(maxlen=maxlen)
        self._next_id = 1

    @staticmethod
    def _now() -> datetime:
        return datetime.now(UTC)

    @staticmethod
    def _iso(value: Any) -> str | None:
        return None if value is None else value.isoformat()

    def _public(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            **row,
            "created_at": self._iso(row["created_at"]),
            "read_at": self._iso(row["read_at"]),
            "delivered_at": self._iso(row.get("delivered_at")),
        }

    async def insert_notification(
        self, *, recipient: str, message: str, kind: str, sender: str = "", space_id: str | None = None
    ) -> dict[str, Any]:
        row: dict[str, Any] = {
            "id": self._next_id,
            "recipient": recipient,
            "message": message,
            "kind": kind,
            "sender": sender,
            "space_id": space_id,
            "created_at": self._now(),
            "read_at": None,
            # 投递状态（P4 外部通知，设计 §14.2）：默认"只在站内"。
            "delivery_channel": "in_app",
            "delivery_status": "in_app",
            "delivery_error": None,
            "delivered_at": None,
        }
        self._next_id += 1
        self._rows[row["id"]] = row
        self._order.append(row["id"])
        while len(self._rows) > (self._order.maxlen or 0):
            self._rows.pop(self._order.popleft(), None)
        return self._public(row)

    async def list_notifications(self, *, recipient: str, unread_only: bool = False, limit: int = 50) -> list[dict[str, Any]]:
        rows = [self._rows[nid] for nid in self._order if nid in self._rows]
        rows = [row for row in rows if row["recipient"] == recipient]
        if unread_only:
            rows = [row for row in rows if row["read_at"] is None]
        rows.sort(key=lambda row: row["created_at"], reverse=True)
        return [self._public(row) for row in rows[: max(1, min(int(limit), 200))]]

    async def mark_read(self, notification_id: int, recipient: str) -> dict[str, Any] | None:
        row = self._rows.get(int(notification_id))
        if row is None or row["recipient"] != recipient or row["read_at"] is not None:
            return None
        row["read_at"] = self._now()
        return self._public(row)

    async def set_notification_delivery(self, notification_id: int, *, status: str, error: str | None = None) -> dict[str, Any]:
        """写外部通道的投递结果。站内那一条不因外发失败而回退（设计 §14.2）。"""
        row = self._rows.get(int(notification_id))
        if row is None:
            raise KeyError(f"通知不存在：{notification_id}")
        row["delivery_status"] = status
        row["delivery_error"] = error
        row["delivered_at"] = self._now()
        return self._public(row)


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
        # 告警实例（内存态）：同一预警只留一条，重启即失——能力面用
        # `alerts_persistent: false` 如实标注，不假装。
        self.alerts = AlertStore()
        self.handling_tasks = HandlingTaskStore()
        self.notifications = NotificationStore()
        # 站点维表（内存态）：与 PostgreSQL 那份同构，由 `scripts/import_stations.py` 显式写入。
        # 内存视图没有持久层，导入只活到进程结束——这一条由导入命令在结果里说清楚。
        self._station_dim: dict[str, dict[str, object]] = {}
        # 区域级站点画像（内存态）：静态孕灾属性，由 `scripts/import_site_profiles.py` 显式写入。
        self.site_profiles = SiteProfileStore()

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

    async def upsert_site_profile(self, profile: SiteProfile) -> None:
        self.site_profiles.upsert(profile)

    async def get_site_profile(self, region_code: str) -> SiteProfile | None:
        return self.site_profiles.get(region_code)

    async def list_site_profiles(self, *, region_code: str | None = None, limit: int = 200) -> list[SiteProfile]:
        return self.site_profiles.list(region_code=region_code, limit=limit)

    # ---------- 动作执行记录（可选能力面：SupportsControlActions） ----------

    async def create_action(self, **kwargs: Any) -> tuple[dict[str, Any], bool]:
        return await self.control_actions.create_action(**kwargs)

    async def confirm_action(self, action_id: str) -> dict[str, Any] | None:
        return await self.control_actions.confirm_action(action_id)

    async def review_action(self, action_id: str, reviewer: dict[str, Any]) -> tuple[dict[str, Any], bool] | None:
        return await self.control_actions.review_action(action_id, reviewer)

    async def mark_action_executed(self, action_id: str, result: dict[str, Any], **kwargs: Any) -> dict[str, Any] | None:
        return await self.control_actions.mark_action_executed(action_id, result, **kwargs)

    async def mark_action_failed(self, action_id: str, error: dict[str, Any], **kwargs: Any) -> dict[str, Any] | None:
        return await self.control_actions.mark_action_failed(action_id, error, **kwargs)

    async def record_audit(self, audit: AuditRecord) -> None:
        return await self.control_actions.record_audit(audit)

    def audit_snapshot(self) -> list[dict[str, Any]]:
        return self.control_actions.audit_snapshot()

    async def read_audit(
        self,
        *,
        operator_id: str | None = None,
        action: str | None = None,
        phase: str | None = None,
        space_id: str | None = None,
        action_id: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """审计读面（设计 §14.3）：内存形态也读得到，只是重启即失。

        过滤与排序口径与 SQL 侧一致（最近在前、同值按 id 兜底），免得两套实现给出两种顺序。
        时间过滤用**字符串比较**：`audit_ops.as_row()` 存的时间是 ISO 串，
        同格式的 ISO 串字典序即时间序（同一时区偏移下成立，跨时区不成立——
        而本仓的时间戳统一是 UTC）。
        """
        rows = self.control_actions.audit_snapshot()
        filtered = [
            row
            for row in rows
            if (operator_id is None or row.get("operator_id") == operator_id)
            and (action is None or row.get("action") == action)
            and (phase is None or row.get("phase") == phase)
            and (space_id is None or row.get("space_id") == space_id)
            and (action_id is None or row.get("action_id") == action_id)
            and (since is None or str(row.get("ts") or "") >= str(since))
            and (until is None or str(row.get("ts") or "") <= str(until))
        ]
        filtered.sort(key=lambda row: (str(row.get("ts") or ""), int(row.get("id") or 0)), reverse=True)
        capped = max(1, min(int(limit), 200))
        skip = max(0, int(offset))
        return filtered[skip : skip + capped]

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

    # ---------- 告警实例（可选能力面：SupportsAlerts） ----------

    async def raise_alert(self, **kwargs: Any) -> tuple[dict[str, Any], bool]:
        return await self.alerts.raise_alert(**kwargs)

    async def get_alert(self, alert_id: str) -> dict[str, Any] | None:
        return await self.alerts.get_alert(alert_id)

    async def list_alerts(self, **kwargs: Any) -> list[dict[str, Any]]:
        return await self.alerts.list_alerts(**kwargs)

    async def ack_alert(self, alert_id: str, operator_id: str) -> dict[str, Any] | None:
        return await self.alerts.ack_alert(alert_id, operator_id)

    async def transfer_alert(self, alert_id: str, assignee: str) -> dict[str, Any] | None:
        return await self.alerts.transfer_alert(alert_id, assignee)

    async def snooze_alert(self, alert_id: str, minutes: int) -> dict[str, Any] | None:
        return await self.alerts.snooze_alert(alert_id, minutes)

    async def snooze_by_warning(self, warning_id: str, minutes: int) -> dict[str, Any] | None:
        return await self.alerts.snooze_by_warning(warning_id, minutes)

    async def escalate_due(self, *, after_minutes: int) -> list[dict[str, Any]]:
        return await self.alerts.escalate_due(after_minutes=after_minutes)

    # ---------- 处置任务（可选能力面：SupportsHandlingTasks） ----------

    async def ensure_space(self, *, space_id: str, warning_id: str) -> dict[str, Any]:
        return await self.handling_tasks.ensure_space(space_id=space_id, warning_id=warning_id)

    async def get_space(self, space_id: str) -> dict[str, Any] | None:
        return await self.handling_tasks.get_space(space_id)

    async def list_spaces(self, *, open_only: bool = True, limit: int = 50) -> list[dict[str, Any]]:
        return await self.handling_tasks.list_spaces(open_only=open_only, limit=limit)

    async def ensure_task(self, *, space_id: str, stu_id: str) -> tuple[dict[str, Any], bool]:
        return await self.handling_tasks.ensure_task(space_id=space_id, stu_id=stu_id)

    async def get_task(self, task_id: str) -> dict[str, Any] | None:
        return await self.handling_tasks.get_task(task_id)

    async def list_tasks(
        self, *, space_id: str | None = None, assignee: str | None = None, status: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        return await self.handling_tasks.list_tasks(space_id=space_id, assignee=assignee, status=status, limit=limit)

    async def claim_task(self, task_id: str, operator_id: str, expected_version: int) -> dict[str, Any] | None:
        return await self.handling_tasks.claim_task(task_id, operator_id, expected_version)

    async def ack_task(self, task_id: str, operator_id: str, expected_version: int) -> dict[str, Any] | None:
        return await self.handling_tasks.ack_task(task_id, operator_id, expected_version)

    async def assign_task(self, task_id: str, assignee: str, expected_version: int) -> dict[str, Any] | None:
        return await self.handling_tasks.assign_task(task_id, assignee, expected_version)

    async def transfer_task(self, task_id: str, assignee: str, expected_version: int) -> dict[str, Any] | None:
        return await self.handling_tasks.transfer_task(task_id, assignee, expected_version)

    async def close_task(self, task_id: str, expected_version: int) -> dict[str, Any] | None:
        return await self.handling_tasks.close_task(task_id, expected_version)

    # ---------- 站内通知（可选能力面：SupportsNotifications） ----------

    async def insert_notification(
        self, *, recipient: str, message: str, kind: str, sender: str = "", space_id: str | None = None
    ) -> dict[str, Any]:
        return await self.notifications.insert_notification(
            recipient=recipient, message=message, kind=kind, sender=sender, space_id=space_id
        )

    async def list_notifications(self, *, recipient: str, unread_only: bool = False, limit: int = 50) -> list[dict[str, Any]]:
        return await self.notifications.list_notifications(recipient=recipient, unread_only=unread_only, limit=limit)

    async def mark_read(self, notification_id: int, recipient: str) -> dict[str, Any] | None:
        return await self.notifications.mark_read(notification_id, recipient)

    async def set_notification_delivery(self, notification_id: int, *, status: str, error: str | None = None) -> dict[str, Any]:
        return await self.notifications.set_notification_delivery(notification_id, status=status, error=error)

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
            "site_profile_count": self.site_profiles.size,
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

    def list(
        self,
        *,
        limit: int = 50,
        region_code: str | None = None,
        released: bool | None = None,
        status: str | None = None,
    ) -> list[WarningRecord]: ...

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

    async def upsert_site_profile(self, profile: SiteProfile) -> None: ...

    async def get_site_profile(self, region_code: str) -> SiteProfile | None: ...

    async def list_site_profiles(self, *, region_code: str | None = None, limit: int = 200) -> list[SiteProfile]: ...

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


@runtime_checkable
class SupportsAuditOps(Protocol):
    """审计读面（P4 读面需求，设计 §14.3）：按人/动作/阶段/空间/时间读流水。

    内存与持久两套都实现——审计在内存形态下也在写（`ControlActionStore._audit`），
    只是重启即失；读面不因为"内存形态数据会丢"就说 503，那会让离线演练查不到账。
    """

    async def read_audit(
        self,
        *,
        operator_id: str | None = None,
        action: str | None = None,
        phase: str | None = None,
        space_id: str | None = None,
        action_id: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]: ...


def audit_read_port(store: object) -> SupportsAuditOps | None:
    return store if isinstance(store, SupportsAuditOps) else None


@runtime_checkable
class SupportsOperatorKeys(Protocol):
    """按人令牌台账的可选能力面：与 `SupportsOperators` 一样**只有持久层实现**。

    内存形态不实现这一面——令牌是"可验证凭据"，重启即失等于"这把钥匙还有效但门锁忘了"，
    比"没有钥匙"更危险。缺这条能力时端点如实 503（与 `OperatorService` 同一口径）。
    """

    async def create_operator_key(self, *, key_id: str, operator_id: str, key_hash: str) -> dict[str, Any]: ...

    async def get_operator_key_by_hash(self, key_hash: str) -> dict[str, Any] | None:
        """按摘要找钥匙。认证路径要用 `active` 与 `key_id`，所以返回原始 dict（含摘要），
        由调用方自己挑字段——读接口那条路不经这里，不会外泄摘要。
        """
        ...

    async def list_operator_keys(self, operator_id: str) -> list[dict[str, Any]]:
        """列某席位的令牌元数据。**不含摘要、不含明文**。"""
        ...

    async def revoke_operator_key(self, key_id: str) -> bool: ...

    async def touch_operator_key(self, key_id: str) -> None: ...

    async def get_key_attempts(self, operator_id: str) -> dict[str, Any] | None: ...

    async def record_key_failure(self, *, operator_id: str, max_attempts: int, lock_minutes: int) -> dict[str, Any]:
        """记一次失败并按阈值锁定：一条语句内完成读改写，且已锁定的窗口不被延长。"""
        ...

    async def clear_key_failures(self, operator_id: str) -> None: ...


def operator_key_port(store: object) -> SupportsOperatorKeys | None:
    return store if isinstance(store, SupportsOperatorKeys) else None


def operator_ledger_port(store: object) -> SupportsOperators | None:
    return store if isinstance(store, SupportsOperators) else None


@dataclass(frozen=True, slots=True)
class AuditRecord:
    """一行 `audit_ops` 审计流水（010 建表，012 起按月分区）。

    这是**服务层与台账层共同认得的行形状**：`services/assistant` 负责填它（执行器第 8 步），
    `storage/` 与 `persistence/` 只负责落库，谁都不许绕过它另拼一份 INSERT 字段清单。
    `before/after` 的口径：`before` 暂空（动作级的前值快照留给以后按需补），`after` 记
    结构化结果 / 错误 / 拒绝原因；`operator_id` 允许 `unknown`（越权匿名），列不加 FK。
    """

    phase: str  # 'intent'（执行前）| 'result'（执行后/被拒）
    action_id: str
    operator_id: str
    role: str
    action: str
    space_id: str | None = None
    target: str = ""
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None
    utterance: str = ""
    resolved_args: dict[str, Any] = field(default_factory=dict)
    idempotency_key: str | None = None
    identity_kind: str = "declared"

    def as_row(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "action_id": self.action_id,
            "operator_id": self.operator_id,
            "role": self.role,
            "action": self.action,
            "space_id": self.space_id,
            "target": self.target,
            "before": self.before,
            "after": self.after,
            "utterance": self.utterance,
            "resolved_args": dict(self.resolved_args),
            "idempotency_key": self.idempotency_key,
            "identity_kind": self.identity_kind,
        }


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
        review_required: int = 0,
        audit: AuditRecord | None = None,
    ) -> tuple[dict[str, Any], bool]: ...

    async def confirm_action(self, action_id: str) -> dict[str, Any] | None: ...

    async def review_action(self, action_id: str, reviewer: dict[str, Any]) -> tuple[dict[str, Any], bool] | None: ...

    async def mark_action_executed(
        self, action_id: str, result: dict[str, Any], *, audit: AuditRecord | None = None
    ) -> dict[str, Any] | None: ...

    async def mark_action_failed(
        self, action_id: str, error: dict[str, Any], *, audit: AuditRecord | None = None
    ) -> dict[str, Any] | None: ...

    async def revoke_action(self, action_id: str) -> dict[str, Any] | None: ...

    async def get_action(self, action_id: str) -> dict[str, Any] | None: ...

    async def list_actions(self, *, operator_id: str | None = None, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]: ...

    async def expire_actions(self) -> int: ...

    async def recover_stuck_actions(self) -> int: ...

    async def record_audit(self, audit: AuditRecord) -> None: ...


def control_action_port(store: object) -> SupportsControlActions | None:
    return store if isinstance(store, SupportsControlActions) else None


@runtime_checkable
class SupportsNotifications(Protocol):
    """站内通知台账：内存与持久两套都实现，语义逐条对齐。"""

    async def insert_notification(
        self, *, recipient: str, message: str, kind: str, sender: str = "", space_id: str | None = None
    ) -> dict[str, Any]: ...

    async def list_notifications(self, *, recipient: str, unread_only: bool = False, limit: int = 50) -> list[dict[str, Any]]: ...

    async def mark_read(self, notification_id: int, recipient: str) -> dict[str, Any] | None: ...

    async def set_notification_delivery(self, notification_id: int, *, status: str, error: str | None = None) -> dict[str, Any]:
        """写外部通道的投递结果（设计 §14.2）。

        `status` 三值：`in_app`（仅站内）/ `delivered`（外发成功）/ `failed`（外发失败）。
        站内那一条**不因外发失败而回退**：真源是站内，外发只是加速器。
        """
        ...


def notification_port(store: object) -> SupportsNotifications | None:
    return store if isinstance(store, SupportsNotifications) else None


@runtime_checkable
class SupportsHandlingTasks(Protocol):
    """处置任务台账：内存与持久两套都实现，语义逐条对齐（含 version 乐观锁）。"""

    async def ensure_space(self, *, space_id: str, warning_id: str) -> dict[str, Any]: ...

    async def get_space(self, space_id: str) -> dict[str, Any] | None: ...

    async def list_spaces(self, *, open_only: bool = True, limit: int = 50) -> list[dict[str, Any]]: ...

    async def ensure_task(self, *, space_id: str, stu_id: str) -> tuple[dict[str, Any], bool]: ...

    async def get_task(self, task_id: str) -> dict[str, Any] | None: ...

    async def list_tasks(
        self, *, space_id: str | None = None, assignee: str | None = None, status: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]: ...

    async def claim_task(self, task_id: str, operator_id: str, expected_version: int) -> dict[str, Any] | None: ...

    async def ack_task(self, task_id: str, operator_id: str, expected_version: int) -> dict[str, Any] | None: ...

    async def assign_task(self, task_id: str, assignee: str, expected_version: int) -> dict[str, Any] | None: ...

    async def transfer_task(self, task_id: str, assignee: str, expected_version: int) -> dict[str, Any] | None: ...

    async def close_task(self, task_id: str, expected_version: int) -> dict[str, Any] | None: ...


def handling_task_port(store: object) -> SupportsHandlingTasks | None:
    return store if isinstance(store, SupportsHandlingTasks) else None


@runtime_checkable
class SupportsAlerts(Protocol):
    """告警台账的可选能力面：内存与持久两套都实现，语义逐条对齐。

    与 `SupportsControlActions` 不同，这里**内存形态也实现**：告警是"看板 + 确认"的
    交互态，开发与演示需要它能跑；代价是重启即失，由 `alerts_persistent` 如实标注。
    """

    async def raise_alert(
        self,
        *,
        alert_id: str,
        warning_id: str,
        level: int,
        region_code: str,
        drill: bool,
    ) -> tuple[dict[str, Any], bool]: ...

    async def get_alert(self, alert_id: str) -> dict[str, Any] | None: ...

    async def list_alerts(
        self,
        *,
        open_only: bool = True,
        status: str | None = None,
        level: int | None = None,
        drill: bool | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]: ...

    async def ack_alert(self, alert_id: str, operator_id: str) -> dict[str, Any] | None: ...

    async def transfer_alert(self, alert_id: str, assignee: str) -> dict[str, Any] | None: ...

    async def snooze_alert(self, alert_id: str, minutes: int) -> dict[str, Any] | None: ...

    async def snooze_by_warning(self, warning_id: str, minutes: int) -> dict[str, Any] | None: ...

    async def escalate_due(self, *, after_minutes: int) -> list[dict[str, Any]]: ...


def alert_ledger_port(store: object) -> SupportsAlerts | None:
    return store if isinstance(store, SupportsAlerts) else None


def _to_iso(moment: datetime | str | None) -> str | None:
    if moment is None:
        return None
    if isinstance(moment, str):
        return parse_iso(moment).isoformat().replace("+00:00", "Z")
    return moment.isoformat().replace("+00:00", "Z")
