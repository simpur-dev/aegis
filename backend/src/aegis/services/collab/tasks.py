"""处置空间与任务的业务面。

边界：本服务不生成任务单元（那是链路的 STU）、不发预警、不发通知。
它只把链路产物幂等映射成「空间 + 任务」，并给助手动作 / `/spaces` 提供带乐观锁的状态迁移。
`ensure_from_chain` 挂在 `container.on_result`：写读视图与写业务表在装配层协调，
`PlatformStore` 自己不碰数据库（架构铁律 3）。

内存形态与 PostgreSQL 形态共用 `SupportsHandlingTasks`，差别只有跨重启是否存活，
由 `persistent` 如实外露，不假装。

状态迁移成功后的广播只负责"快"：台账才是真源，前端重连时拉 `/spaces` 对账。
`publish` 是注入的回调而不是直接 import `AgentGateway`——`services/` 不得依赖 `aegis.bus`
（架构铁律的 AST 门禁），subject 也由装配点传入，本层不认识 NATS 主题。
"""

from __future__ import annotations

import secrets
from collections.abc import Awaitable, Callable
from typing import Any

from aegis.domain.chain import ChainResult
from aegis.domain.enums import Action
from aegis.domain.messages import make_event
from aegis.storage.store import SupportsHandlingTasks


class TaskNotFoundError(LookupError):
    def __init__(self, task_id: str) -> None:
        super().__init__(f"处置任务不存在：{task_id}")
        self.task_id = task_id


class SpaceNotFoundError(LookupError):
    def __init__(self, space_id: str) -> None:
        super().__init__(f"处置空间不存在：{space_id}")
        self.space_id = space_id


class TaskNotInSpaceError(LookupError):
    """路径上写的空间与任务不属于同一个空间：不是"找不到"，是"指错了"。"""

    def __init__(self, space_id: str, task_id: str) -> None:
        super().__init__(f"任务 {task_id} 不在空间 {space_id} 内")
        self.space_id = space_id
        self.task_id = task_id


class TaskConflictError(RuntimeError):
    """状态已变或版本被别人抢先改过：这次迁移没有生效。"""

    def __init__(self, task_id: str, status: str) -> None:
        super().__init__(f"任务 {task_id} 当前状态为 {status}，本次操作未生效（可能已被他人处置）")
        self.task_id = task_id
        self.status = status


class CollabService:
    def __init__(
        self,
        ledger: SupportsHandlingTasks | None,
        *,
        persistent: bool = False,
        publish: Callable[[str, Any], Awaitable[None]] | None = None,
        subject: str = "ops.handling.task",
    ) -> None:
        self._ledger = ledger
        self._persistent = persistent
        self._publish = publish
        self._subject = subject

    @property
    def available(self) -> bool:
        return self._ledger is not None

    @property
    def persistent(self) -> bool:
        return self._persistent

    def _require(self) -> SupportsHandlingTasks:
        if self._ledger is None:
            raise RuntimeError("处置任务台账未装配")
        return self._ledger

    async def ensure_from_chain(self, result: ChainResult) -> dict[str, Any] | None:
        """一条预警一个空间；空间里的每个 STU 一条任务。重复链路只幂等 upsert。"""
        ledger = self._ledger
        warning = result.warning
        if ledger is None or warning is None:
            return None
        space_id = warning.warning_id
        await ledger.ensure_space(space_id=space_id, warning_id=space_id)
        created = 0
        for unit in result.task_units:
            _task, is_new = await ledger.ensure_task(space_id=space_id, stu_id=unit.task_unit_id)
            created += int(is_new)
        return {"space_id": space_id, "tasks_total": len(result.task_units), "tasks_created": created}

    # ---------- 读面：`/spaces` 的两个端点只走这里，不自己拼 SQL ----------

    async def list_spaces(self, *, open_only: bool = True, limit: int = 50) -> list[dict[str, Any]]:
        """空间列表带任务计数：SQL 侧一次 GROUP BY 算完，内存侧同语义，不做 N+1。"""
        return await self._require().list_spaces(open_only=open_only, limit=limit)

    async def space_detail(self, space_id: str, *, limit: int = 200) -> dict[str, Any]:
        """空间详情 = 空间 + 任务 + 参与人 + 状态计数，一次读齐供 `HandlingBar` 呈现。"""
        ledger = self._require()
        space = await ledger.get_space(space_id)
        if space is None:
            raise SpaceNotFoundError(space_id)
        tasks = await ledger.list_tasks(space_id=space_id, limit=limit)
        counts: dict[str, int] = {}
        for task in tasks:
            key = str(task["status"])
            counts[key] = counts.get(key, 0) + 1
        participants = sorted({str(task["assignee"]) for task in tasks if task.get("assignee")})
        return {
            "space": space,
            "tasks": tasks,
            "participants": participants,
            "status_counts": counts,
            "tasks_total": len(tasks),
        }

    async def get_task_in_space(self, space_id: str, task_id: str) -> dict[str, Any]:
        """写端点先核对任务确实属于路径里的空间，避免用 A 空间的路径改 B 空间的任务。"""
        record = await self.get(task_id)
        if str(record["space_id"]) != space_id:
            raise TaskNotInSpaceError(space_id, task_id)
        return record

    async def list_tasks(
        self,
        *,
        space_id: str | None = None,
        assignee: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        return await self._require().list_tasks(space_id=space_id, assignee=assignee, status=status, limit=limit)

    async def get(self, task_id: str) -> dict[str, Any]:
        record = await self._require().get_task(task_id)
        if record is None:
            raise TaskNotFoundError(task_id)
        return record

    # ---------- 写面：所有迁移都过这一处，广播也只有这一处 ----------

    async def _move(
        self,
        task_id: str,
        verb: str,
        call: Callable[[int], Awaitable[dict[str, Any] | None]],
        *,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        """读当前 version → 条件 UPDATE。命中 0 行即 409，不覆盖别人的结果。

        `expected_version` 由 REST 层的 `If-Match` / 请求体给出：客户端按自己看到的那版发起，
        服务端就用那一版做条件，绝不"读到一个新版本再偷偷替客户端改主意"。
        助手动作不带版本时（对话里没有版本号）才退回读当前版。
        """
        current = await self.get(task_id)
        version = int(current["version"]) if expected_version is None else int(expected_version)
        updated = await call(version)
        if updated is None:
            raise TaskConflictError(task_id, str(current["status"]))
        await self._broadcast(updated, verb)
        return updated

    async def _broadcast(self, task: dict[str, Any], verb: str) -> None:
        if self._publish is None:
            return
        event = make_event(
            source="platform.collab",
            action=Action.HANDLING_TASK_CHANGED.value,
            trace_id=f"trc_{secrets.token_hex(8)}",
            target="platform.handling_sink",
            payload={
                "verb": verb,
                "space_id": task["space_id"],
                "task_id": task["task_id"],
                "stu_id": task["stu_id"],
                "status": task["status"],
                "assignee": task["assignee"],
                "version": int(task["version"]),
                "updated_at": task["updated_at"],
            },
        )
        await self._publish(self._subject, event)

    async def claim(self, task_id: str, operator_id: str, *, expected_version: int | None = None) -> dict[str, Any]:
        ledger = self._require()
        return await self._move(task_id, "claim", lambda v: ledger.claim_task(task_id, operator_id, v), expected_version=expected_version)

    async def ack(self, task_id: str, operator_id: str, *, expected_version: int | None = None) -> dict[str, Any]:
        ledger = self._require()
        return await self._move(task_id, "ack", lambda v: ledger.ack_task(task_id, operator_id, v), expected_version=expected_version)

    async def assign(self, task_id: str, assignee: str, *, expected_version: int | None = None) -> dict[str, Any]:
        ledger = self._require()
        return await self._move(task_id, "assign", lambda v: ledger.assign_task(task_id, assignee, v), expected_version=expected_version)

    async def transfer(self, task_id: str, assignee: str, *, expected_version: int | None = None) -> dict[str, Any]:
        ledger = self._require()
        return await self._move(
            task_id,
            "transfer",
            lambda v: ledger.transfer_task(task_id, assignee, v),
            expected_version=expected_version,
        )

    async def close(self, task_id: str, *, expected_version: int | None = None) -> dict[str, Any]:
        ledger = self._require()
        return await self._move(task_id, "close", lambda v: ledger.close_task(task_id, v), expected_version=expected_version)

    def describe(self) -> dict[str, Any]:
        return {"available": self.available, "persistent": self.persistent}
