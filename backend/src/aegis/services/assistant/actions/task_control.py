"""P1/P2 任务动作：认领 / 确认 / 派单 / 移交 / 结单。

动作层只做三件事：解析目标、给确认预览、把指令交给 `CollabService`。
状态迁移与乐观锁全在服务层 + 台账，这里不写第二份状态机。
"""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from aegis.domain.operator import OperatorRole
from aegis.services.assistant.base import (
    Action,
    ActionContext,
    ActionDomain,
    ActionError,
    ActionResult,
    ActionSpec,
    ImpactPreview,
    ResolvedTargets,
    TargetRef,
)
from aegis.services.collab.tasks import TaskConflictError, TaskNotFoundError

_TASK_ID = r"^stu_[0-9a-f]{16}$"
_OPERATOR_ID = r"^[A-Za-z0-9_-]{4,32}$"


class CollabPort(Protocol):
    """`CollabService` 的动作侧只读面。"""

    async def get(self, task_id: str) -> dict[str, Any]: ...

    async def claim(self, task_id: str, operator_id: str) -> dict[str, Any]: ...

    async def ack(self, task_id: str, operator_id: str) -> dict[str, Any]: ...

    async def assign(self, task_id: str, assignee: str) -> dict[str, Any]: ...

    async def transfer(self, task_id: str, assignee: str) -> dict[str, Any]: ...

    async def close(self, task_id: str) -> dict[str, Any]: ...


class TaskTargetParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(pattern=_TASK_ID, description="处置任务 ID = 任务单元 STU 编号")


class TaskClaimParams(TaskTargetParams):
    pass


class TaskAckParams(TaskTargetParams):
    pass


class TaskAssignParams(TaskTargetParams):
    assignee: str = Field(pattern=_OPERATOR_ID, description="受理人席位 ID")


class TaskTransferParams(TaskTargetParams):
    assignee: str = Field(pattern=_OPERATOR_ID, description="新责任人席位 ID")


class TaskCloseParams(TaskTargetParams):
    pass


def _translate(exc: Exception) -> ActionError:
    """服务层的两个领域异常 → 助手统一错误码，不在每个动作里各写一遍 if。"""
    if isinstance(exc, TaskNotFoundError):
        return ActionError("E_TARGET_NOT_FOUND", str(exc))
    if isinstance(exc, TaskConflictError):
        return ActionError("E_ACTION_CONFLICT", str(exc))
    return ActionError("E_ACTION_EXECUTION_FAILED", str(exc))


class _TaskAction(Action):
    def __init__(self, collab: CollabPort) -> None:
        self._collab = collab

    async def resolve(self, params: BaseModel, ctx: ActionContext) -> ResolvedTargets:
        assert isinstance(params, TaskTargetParams)
        try:
            task = await self._collab.get(params.task_id)
        except Exception as exc:
            raise _translate(exc) from exc
        return ResolvedTargets((TargetRef("task", str(task["task_id"]), str(task.get("status", ""))),))

    async def preview(self, targets: ResolvedTargets, params: BaseModel, ctx: ActionContext) -> ImpactPreview:
        assert isinstance(params, TaskTargetParams)
        try:
            task = await self._collab.get(params.task_id)
        except Exception as exc:
            raise _translate(exc) from exc
        return ImpactPreview(
            targets=targets.items,
            irreversible=(f"执行「{self.spec.title}」后任务状态与 version 会变更",),
            warnings=(f"任务 {params.task_id} 当前 {task['status']}，责任人 {task.get('assignee') or '未指派'}，版本 {task['version']}",),
        )

    async def _call(self, coro: Any) -> dict[str, Any]:
        try:
            return await coro
        except Exception as exc:
            raise _translate(exc) from exc

    @staticmethod
    def _operator(ctx: ActionContext) -> str:
        if ctx.operator is None:
            raise ActionError("E_OPERATOR_REQUIRED", "写动作需要有效席位")
        return ctx.operator.operator_id


class TaskClaim(_TaskAction):
    spec = ActionSpec(
        name="task.claim",
        title="认领处置任务",
        domain=ActionDomain.TASK,
        required_role=OperatorRole.DUTY,
        params=TaskClaimParams,
        writes=True,
        previewable=True,
        example="把 stu_0123456789abcdef 认领",
    )

    async def execute(self, targets: ResolvedTargets, params: BaseModel, ctx: ActionContext) -> ActionResult:
        assert isinstance(params, TaskClaimParams)
        task = await self._call(self._collab.claim(params.task_id, self._operator(ctx)))
        return ActionResult(data={"task": task}, summary=f"任务 {params.task_id} 已认领")


class TaskAck(_TaskAction):
    spec = ActionSpec(
        name="task.ack",
        title="确认任务处置完成",
        domain=ActionDomain.TASK,
        required_role=OperatorRole.DUTY,
        params=TaskAckParams,
        writes=True,
        previewable=True,
        example="把 stu_0123456789abcdef 确认完成",
    )

    async def execute(self, targets: ResolvedTargets, params: BaseModel, ctx: ActionContext) -> ActionResult:
        assert isinstance(params, TaskAckParams)
        task = await self._call(self._collab.ack(params.task_id, self._operator(ctx)))
        return ActionResult(data={"task": task}, summary=f"任务 {params.task_id} 已确认完成")


class TaskAssign(_TaskAction):
    spec = ActionSpec(
        name="task.assign",
        title="派单给指定席位",
        domain=ActionDomain.TASK,
        required_role=OperatorRole.LEAD,
        params=TaskAssignParams,
        needs_confirm=True,
        writes=True,
        previewable=True,
        example="把 stu_0123456789abcdef 派给 lead-01",
    )

    async def execute(self, targets: ResolvedTargets, params: BaseModel, ctx: ActionContext) -> ActionResult:
        assert isinstance(params, TaskAssignParams)
        task = await self._call(self._collab.assign(params.task_id, params.assignee))
        return ActionResult(data={"task": task}, summary=f"任务 {params.task_id} 已派给 {params.assignee}")


class TaskTransfer(_TaskAction):
    spec = ActionSpec(
        name="task.transfer",
        title="移交处置任务",
        domain=ActionDomain.TASK,
        required_role=OperatorRole.LEAD,
        params=TaskTransferParams,
        needs_confirm=True,
        writes=True,
        previewable=True,
        example="把 stu_0123456789abcdef 转给 lead-02",
    )

    async def execute(self, targets: ResolvedTargets, params: BaseModel, ctx: ActionContext) -> ActionResult:
        assert isinstance(params, TaskTransferParams)
        task = await self._call(self._collab.transfer(params.task_id, params.assignee))
        return ActionResult(data={"task": task}, summary=f"任务 {params.task_id} 已转给 {params.assignee}")


class TaskClose(_TaskAction):
    spec = ActionSpec(
        name="task.close",
        title="关闭处置任务",
        domain=ActionDomain.TASK,
        required_role=OperatorRole.DUTY,
        params=TaskCloseParams,
        writes=True,
        previewable=True,
        example="把 stu_0123456789abcdef 结单",
    )

    async def execute(self, targets: ResolvedTargets, params: BaseModel, ctx: ActionContext) -> ActionResult:
        assert isinstance(params, TaskCloseParams)
        task = await self._call(self._collab.close(params.task_id))
        return ActionResult(data={"task": task}, summary=f"任务 {params.task_id} 已关闭")
