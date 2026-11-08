"""L3 通知动作：通知值班长 / 群发通知一组席位。

动作层只决定"发给谁、说什么"，落库与广播都在 `Notifier`，这里不碰总线、不碰存储。
`notify.lead` 不给收件人时由 Notifier 从席位台账找 lead/admin，助手不臆造席位 ID。
"""

from __future__ import annotations

from typing import Annotated, Any, Protocol

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

_OPERATOR_ID = r"^[A-Za-z0-9_-]{4,32}$"
OperatorId = Annotated[str, Field(pattern=_OPERATOR_ID)]


class NotifierPort(Protocol):
    async def notify(
        self,
        *,
        recipient: str,
        message: str,
        kind: str,
        sender: str = "",
        space_id: str | None = None,
    ) -> dict[str, Any]: ...

    async def notify_many(
        self,
        recipients: list[str],
        message: str,
        *,
        sender: str = "",
        space_id: str | None = None,
    ) -> list[dict[str, Any]]: ...

    async def lead_ids(self) -> list[str]: ...


class NotifyLeadParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=2, max_length=500)
    operator_id: OperatorId | None = Field(default=None, description="不填则通知当前在册值班长")


class NotifyGroupParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operator_ids: list[OperatorId] = Field(min_length=1, max_length=20)
    message: str = Field(min_length=2, max_length=500)


def _sender(ctx: ActionContext) -> str:
    return ctx.operator.operator_id if ctx.operator is not None else ""


class NotifyLead(Action):
    spec = ActionSpec(
        name="notify.lead",
        title="通知值班长",
        domain=ActionDomain.NOTIFY,
        required_role=OperatorRole.DUTY,
        params=NotifyLeadParams,
        writes=True,
        previewable=True,
        example="通知值班长：通麦镇需要复核",
    )

    def __init__(self, notifier: NotifierPort) -> None:
        self._notifier = notifier

    async def resolve(self, params: BaseModel, ctx: ActionContext) -> ResolvedTargets:
        assert isinstance(params, NotifyLeadParams)
        recipient = params.operator_id or next(iter(await self._notifier.lead_ids()), None)
        if not recipient:
            raise ActionError("E_TARGET_NOT_FOUND", "没有在册的值班长席位，请指定 operator_id")
        return ResolvedTargets((TargetRef("operator", recipient, "值班长"),))

    async def preview(self, targets: ResolvedTargets, params: BaseModel, ctx: ActionContext) -> ImpactPreview:
        assert isinstance(params, NotifyLeadParams)
        return ImpactPreview(
            targets=targets.items,
            channels=("站内通知", "事件流"),
            warnings=(f"将向 {targets.ids[0] if targets.ids else '值班长'} 发送：{params.message[:80]}",),
        )

    async def execute(self, targets: ResolvedTargets, params: BaseModel, ctx: ActionContext) -> ActionResult:
        assert isinstance(params, NotifyLeadParams)
        recipient = targets.ids[0]
        record = await self._notifier.notify(
            recipient=recipient, message=params.message, kind="lead", sender=_sender(ctx), space_id=ctx.space_id
        )
        return ActionResult(data={"notification": record}, summary=f"已通知值班长 {recipient}")


class NotifyGroup(Action):
    spec = ActionSpec(
        name="notify.group",
        title="群发处置通知",
        domain=ActionDomain.NOTIFY,
        required_role=OperatorRole.LEAD,
        params=NotifyGroupParams,
        needs_confirm=True,
        writes=True,
        previewable=True,
        dual_review=True,
        example="群发通知 @lead-01 @lead-02 立即会商",
    )

    def __init__(self, notifier: NotifierPort) -> None:
        self._notifier = notifier

    async def resolve(self, params: BaseModel, ctx: ActionContext) -> ResolvedTargets:
        assert isinstance(params, NotifyGroupParams)
        return ResolvedTargets(tuple(TargetRef("operator", item, "") for item in dict.fromkeys(params.operator_ids)))

    async def preview(self, targets: ResolvedTargets, params: BaseModel, ctx: ActionContext) -> ImpactPreview:
        assert isinstance(params, NotifyGroupParams)
        return ImpactPreview(
            targets=targets.items,
            audience_count=len(set(params.operator_ids)),
            channels=("站内通知", "事件流"),
            irreversible=(f"将向 {len(set(params.operator_ids))} 个席位各写一条通知",),
            warnings=(params.message[:80],),
        )

    async def execute(self, targets: ResolvedTargets, params: BaseModel, ctx: ActionContext) -> ActionResult:
        assert isinstance(params, NotifyGroupParams)
        records = await self._notifier.notify_many(list(params.operator_ids), params.message, sender=_sender(ctx), space_id=ctx.space_id)
        return ActionResult(data={"notifications": records, "count": len(records)}, summary=f"已通知 {len(records)} 个席位")
