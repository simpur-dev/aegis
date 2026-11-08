"""处置空间与任务的业务面。

边界：本服务不生成任务单元（那是链路的 STU）、不发预警、不发通知。
它只把链路产物幂等映射成「空间 + 任务」，并给助手动作提供带乐观锁的状态迁移。
`ensure_from_chain` 挂在 `container.on_result`：写读视图与写业务表在装配层协调，
`PlatformStore` 自己不碰数据库（架构铁律 3）。

内存形态与 PostgreSQL 形态共用 `SupportsHandlingTasks`，差别只有跨重启是否存活，
由 `persistent` 如实外露，不假装。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aegis.domain.chain import ChainResult
from aegis.storage.store import SupportsHandlingTasks


class TaskNotFoundError(LookupError):
    def __init__(self, task_id: str) -> None:
        super().__init__(f"处置任务不存在：{task_id}")
        self.task_id = task_id


class TaskConflictError(RuntimeError):
    """状态已变或版本被别人抢先改过：这次迁移没有生效。"""

    def __init__(self, task_id: str, status: str) -> None:
        super().__init__(f"任务 {task_id} 当前状态为 {status}，本次操作未生效（可能已被他人处置）")
        self.task_id = task_id
        self.status = status


class CollabService:
    def __init__(self, ledger: SupportsHandlingTasks | None, *, persistent: bool = False) -> None:
        self._ledger = ledger
        self._persistent = persistent

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

    async def _move(self, task_id: str, call: Callable[[int], Awaitable[dict[str, Any] | None]]) -> dict[str, Any]:
        """读当前 version → 条件 UPDATE。命中 0 行即 409，不覆盖别人的结果。"""
        current = await self.get(task_id)
        updated = await call(int(current["version"]))
        if updated is None:
            raise TaskConflictError(task_id, str(current["status"]))
        return updated

    async def claim(self, task_id: str, operator_id: str) -> dict[str, Any]:
        ledger = self._require()
        return await self._move(task_id, lambda v: ledger.claim_task(task_id, operator_id, v))

    async def ack(self, task_id: str, operator_id: str) -> dict[str, Any]:
        ledger = self._require()
        return await self._move(task_id, lambda v: ledger.ack_task(task_id, operator_id, v))

    async def assign(self, task_id: str, assignee: str) -> dict[str, Any]:
        ledger = self._require()
        return await self._move(task_id, lambda v: ledger.assign_task(task_id, assignee, v))

    async def transfer(self, task_id: str, assignee: str) -> dict[str, Any]:
        ledger = self._require()
        return await self._move(task_id, lambda v: ledger.transfer_task(task_id, assignee, v))

    async def close(self, task_id: str) -> dict[str, Any]:
        ledger = self._require()
        return await self._move(task_id, lambda v: ledger.close_task(task_id, v))

    def describe(self) -> dict[str, Any]:
        return {"available": self.available, "persistent": self.persistent}
