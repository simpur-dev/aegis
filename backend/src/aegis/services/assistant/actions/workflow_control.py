"""L4 工作流控制动作：启动模板 / 人工决策 / 中止实例。

三个动作只把助手指令翻译成 `WorkflowEngine` 已有的三个入口，不另起一套实例台账：
实例是否存在只读 `instance_detail`，节点是否在等人、决策值是否合法仍由引擎判定。
因此「实例不存在」是 404，「状态/选项冲突」是 409，两者不会混成一句"执行失败"。
"""

from __future__ import annotations

import secrets
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
from aegis.workflow.engine import WorkflowValidationError

_INSTANCE_ID = r"^wfi_[0-9a-f]{12}$"
_NODE_ID = r"^[a-z][a-z0-9_-]{0,31}$"
_CHOICE = r"^[a-z][a-z0-9_-]{0,31}$"


class WorkflowPort(Protocol):
    """动作侧只看见引擎的这四个口子；测试可以喂 fake，装配点注入真引擎。"""

    async def start_from_name(self, name: str, *, trace_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]: ...

    async def resume(self, instance_id: str, *, node_id: str, decision: dict[str, Any]) -> dict[str, Any]: ...

    async def abort(self, instance_id: str, *, reason: str = "") -> dict[str, Any]: ...

    def instance_detail(self, instance_id: str) -> dict[str, Any] | None: ...


class WorkflowStartParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=64, description="工作流模板名，见「工作流」页模板列表")


class WorkflowInstanceParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    instance_id: str = Field(pattern=_INSTANCE_ID)


class WorkflowDecideParams(WorkflowInstanceParams):
    node_id: str = Field(pattern=_NODE_ID)
    choice: str = Field(pattern=_CHOICE, description="人工决策值，必须在该节点 pending_options 内")
    comment: str = Field(default="", max_length=200)


class WorkflowAbortParams(WorkflowInstanceParams):
    reason: str = Field(default="", max_length=200)


class _WorkflowAction(Action):
    """三个动作共用的只读目标校验与不可逆预览。"""

    def __init__(self, workflow: WorkflowPort) -> None:
        self._workflow = workflow

    def _instance(self, instance_id: str) -> dict[str, Any]:
        detail = self._workflow.instance_detail(instance_id)
        if detail is None:
            raise ActionError("E_TARGET_NOT_FOUND", f"工作流实例不存在：{instance_id}（引擎只保留本进程实例）")
        return detail

    async def preview(self, targets: ResolvedTargets, params: BaseModel, ctx: ActionContext) -> ImpactPreview:
        return ImpactPreview(
            targets=targets.items,
            irreversible=(f"「{self.spec.title}」一经执行不可撤销；实例状态由工作流引擎落定",),
            warnings=("执行后请到「工作流」页核对实例状态与节点日志",),
        )

    @staticmethod
    def _brief(detail: dict[str, Any]) -> dict[str, Any]:
        keys = ("instance_id", "workflow_id", "workflow_version", "status", "trace_id", "error", "interrupted")
        return {key: detail[key] for key in keys if detail.get(key) is not None}

    @staticmethod
    def _operator(ctx: ActionContext) -> str:
        return ctx.operator.operator_id if ctx.operator else ""


class WorkflowStart(_WorkflowAction):
    spec = ActionSpec(
        name="workflow.start",
        title="启动工作流",
        domain=ActionDomain.WORKFLOW,
        required_role=OperatorRole.LEAD,
        params=WorkflowStartParams,
        needs_confirm=True,
        writes=True,
        previewable=True,
        example="启动工作流 name=report_review",
    )

    async def resolve(self, params: BaseModel, ctx: ActionContext) -> ResolvedTargets:
        assert isinstance(params, WorkflowStartParams)
        return ResolvedTargets((TargetRef("workflow_template", params.name, params.name),))

    async def execute(self, targets: ResolvedTargets, params: BaseModel, ctx: ActionContext) -> ActionResult:
        assert isinstance(params, WorkflowStartParams)
        try:
            detail = await self._workflow.start_from_name(params.name, trace_id=f"trc_{secrets.token_hex(8)}")
        except WorkflowValidationError as exc:
            raise ActionError("E_TARGET_NOT_FOUND", f"工作流模板不存在：{params.name}") from exc
        if not detail:
            raise ActionError("E_ACTION_EXECUTION_FAILED", f"模板 {params.name} 启动后没有返回实例详情")
        return ActionResult(
            data={"instance": self._brief(detail)},
            summary=f"工作流模板 {params.name} 已启动，实例 {detail.get('instance_id', '')}",
        )


class WorkflowDecide(_WorkflowAction):
    spec = ActionSpec(
        name="workflow.decide",
        title="工作流人工决策",
        domain=ActionDomain.WORKFLOW,
        required_role=OperatorRole.LEAD,
        params=WorkflowDecideParams,
        needs_confirm=True,
        writes=True,
        previewable=True,
        example="工作流决策 instance_id=wfi_abcdef123456 node_id=human_review_1 choice=approve",
    )

    async def resolve(self, params: BaseModel, ctx: ActionContext) -> ResolvedTargets:
        assert isinstance(params, WorkflowDecideParams)
        self._instance(params.instance_id)
        return ResolvedTargets((TargetRef("workflow_instance", params.instance_id, params.node_id),))

    async def execute(self, targets: ResolvedTargets, params: BaseModel, ctx: ActionContext) -> ActionResult:
        assert isinstance(params, WorkflowDecideParams)
        self._instance(params.instance_id)
        decision = {"choice": params.choice, "by": self._operator(ctx), "comment": params.comment}
        try:
            detail = await self._workflow.resume(params.instance_id, node_id=params.node_id, decision=decision)
        except WorkflowValidationError as exc:
            raise ActionError("E_ACTION_CONFLICT", str(exc)) from exc
        return ActionResult(
            data={"instance": self._brief(detail), "choice": params.choice, "node_id": params.node_id},
            summary=f"实例 {params.instance_id} 节点 {params.node_id} 已按 {params.choice} 决策",
        )


class WorkflowAbort(_WorkflowAction):
    spec = ActionSpec(
        name="workflow.abort",
        title="中止工作流",
        domain=ActionDomain.WORKFLOW,
        required_role=OperatorRole.LEAD,
        params=WorkflowAbortParams,
        needs_confirm=True,
        writes=True,
        previewable=True,
        example="中止工作流 instance_id=wfi_abcdef123456 reason=误报",
    )

    async def resolve(self, params: BaseModel, ctx: ActionContext) -> ResolvedTargets:
        assert isinstance(params, WorkflowAbortParams)
        self._instance(params.instance_id)
        return ResolvedTargets((TargetRef("workflow_instance", params.instance_id),))

    async def execute(self, targets: ResolvedTargets, params: BaseModel, ctx: ActionContext) -> ActionResult:
        assert isinstance(params, WorkflowAbortParams)
        self._instance(params.instance_id)
        detail = await self._workflow.abort(params.instance_id, reason=params.reason)
        return ActionResult(
            data={"instance": self._brief(detail), "reason": params.reason, "aborted_by": self._operator(ctx)},
            summary=f"实例 {params.instance_id} 已中止",
        )
