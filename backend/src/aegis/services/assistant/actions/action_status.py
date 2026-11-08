"""`action.status`：查一次写动作台账的执行结果。

`GET /api/v1/assistant/actions/{id}` 已有同一份数据，这里把它变成**可对话的动作**：
值班员问「刚才那个发布动作怎么样了」时不必离开助手去找端点。
只读、不确认，直接复用 `SupportsControlActions.get_action`，不新增查询路径。
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
    ResolvedTargets,
    TargetRef,
)


class ActionLedgerPort(Protocol):
    async def get_action(self, action_id: str) -> dict[str, Any] | None: ...


class ActionStatusParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_id: str = Field(pattern=r"^act_[0-9a-f]{6,24}$")


class ActionStatus(Action):
    spec = ActionSpec(
        name="action.status",
        title="查询动作执行状态",
        domain=ActionDomain.QUERY,
        required_role=OperatorRole.DUTY,
        params=ActionStatusParams,
        example="动作状态 act_abc123",
    )

    def __init__(self, ledger: ActionLedgerPort | None) -> None:
        self._ledger = ledger

    async def resolve(self, params: BaseModel, ctx: ActionContext) -> ResolvedTargets:
        assert isinstance(params, ActionStatusParams)
        return ResolvedTargets((TargetRef("action", params.action_id),))

    async def execute(self, targets: ResolvedTargets, params: BaseModel, ctx: ActionContext) -> ActionResult:
        assert isinstance(params, ActionStatusParams)
        if self._ledger is None:
            raise ActionError("E_ACTION_EXECUTION_FAILED", "动作台账未装配")
        record = await self._ledger.get_action(params.action_id)
        if record is None:
            raise ActionError("E_TARGET_NOT_FOUND", f"动作不存在：{params.action_id}")
        return ActionResult(data={"action": record}, summary=f"动作 {params.action_id} 状态 {record.get('status', '')}")
