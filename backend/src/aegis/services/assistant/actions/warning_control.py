"""L3 预警控制动作：首次发布 / 撤销 / 改级 / 静默 / 重发。

五个动作共用一份「取记录 → 判状态 → 领域操作 → 覆盖读视图」骨架：
- 发布与重发走 `PublisherPort.release`，与主链路、工作流画布同一条路径（含交付回执与广播）；
- 撤销与改级走 Publisher 的纯领域操作，发布事实（`released_at` / 回执）不改写；
- 静默走 `AlertService.silence`，只推迟告警升级，不改预警本身。

依赖全部以 Protocol 注入：动作层不 import `pipeline`/`bus`，也不自己拼广播或触达回执。
"""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from aegis.domain.messages import WarningRecord
from aegis.domain.operator import OperatorRole
from aegis.services.alerting.service import AlertService
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
from aegis.services.response.delivery import AllChannelsFailedError
from aegis.storage.store import WarningPort

_WARNING_ID = r"^wrn_[0-9a-f]{6,24}$"


class PublisherPort(Protocol):
    """`WarningPublisher` 的动作侧只读面：避免助手层反向依赖 pipeline/bus。"""

    async def release(self, record: WarningRecord, *, trace: str | None = None, drill: bool = False) -> WarningRecord: ...

    def publishable(self, record: WarningRecord) -> str | None: ...

    def resendable(self, record: WarningRecord) -> str | None: ...

    def mark_revoked(self, record: WarningRecord) -> WarningRecord: ...

    def change_level(self, record: WarningRecord, level: int) -> WarningRecord: ...


class WarningTargetParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    warning_id: str = Field(pattern=_WARNING_ID)


class WarningPublishParams(WarningTargetParams):
    pass


class WarningRevokeParams(WarningTargetParams):
    reason: str = Field(default="", max_length=200)


class WarningChangeLevelParams(WarningTargetParams):
    level: int = Field(ge=1, le=4, description="预警等级 1..4（5=NONE 不作为发布等级）")


class WarningSilenceParams(WarningTargetParams):
    minutes: int = Field(default=30, ge=1, le=240)


class WarningResendParams(WarningTargetParams):
    pass


class _WarningControlAction(Action):
    """公共骨架：目标解析与影响预览只有一份，子类只写自己那一步状态迁移。"""

    def __init__(self, warnings: WarningPort, publisher: PublisherPort) -> None:
        self._warnings = warnings
        self._publisher = publisher

    def _record(self, warning_id: str) -> WarningRecord:
        record = self._warnings.get(warning_id)
        if record is None:
            raise ActionError("E_TARGET_NOT_FOUND", f"预警不存在：{warning_id}（读视图只保留本进程最近记录）")
        return record

    async def resolve(self, params: BaseModel, ctx: ActionContext) -> ResolvedTargets:
        assert isinstance(params, WarningTargetParams)
        record = self._record(params.warning_id)
        return ResolvedTargets((TargetRef("warning", record.warning_id, record.title_zh),))

    async def preview(self, targets: ResolvedTargets, params: BaseModel, ctx: ActionContext) -> ImpactPreview:
        assert isinstance(params, WarningTargetParams)
        record = self._warnings.get(params.warning_id)
        notes = [f"目标 {params.warning_id}；当前状态 {record.status}"] if record else [f"目标 {params.warning_id} 不在读视图"]
        return ImpactPreview(
            targets=targets.items,
            audience_count=len(record.audiences) if record else 0,
            channels=tuple(record.channels) if record else (),
            irreversible=(f"执行「{self.spec.title}」后写入预警台账；已产生的触达回执与指标不回滚",),
            warnings=tuple(notes),
        )

    async def _save(self, record: WarningRecord) -> None:
        await self._warnings.put(record)

    @staticmethod
    def _brief(record: WarningRecord) -> dict[str, Any]:
        return {
            "warning_id": record.warning_id,
            "status": record.status,
            "risk_level": int(record.risk_level),
            "released_at": record.released_at,
            "revoked_at": record.revoked_at,
            "deliveries": len(record.deliveries),
        }


class WarningPublish(_WarningControlAction):
    spec = ActionSpec(
        name="warning.publish",
        title="发布预警",
        domain=ActionDomain.WARNING,
        required_role=OperatorRole.LEAD,
        params=WarningPublishParams,
        needs_confirm=True,
        writes=True,
        previewable=True,
        dual_review=True,
        example="把 wrn_abcdef123456 发出去",
    )

    async def execute(self, targets: ResolvedTargets, params: BaseModel, ctx: ActionContext) -> ActionResult:
        assert isinstance(params, WarningPublishParams)
        record = self._record(params.warning_id)
        if (conflict := self._publisher.publishable(record)) is not None:
            raise ActionError("E_ACTION_CONFLICT", f"该预警不能首次发布（{conflict}）；已发布请用「重发」")
        try:
            published = await self._publisher.release(record)
        except AllChannelsFailedError as exc:
            raise ActionError("E_ACTION_EXECUTION_FAILED", f"全部通道失败：{exc.message}") from exc
        await self._save(published)
        return ActionResult(data={"warning": self._brief(published)}, summary=f"预警 {record.warning_id} 已发布")


class WarningRevoke(_WarningControlAction):
    spec = ActionSpec(
        name="warning.revoke",
        title="撤销预警",
        domain=ActionDomain.WARNING,
        required_role=OperatorRole.LEAD,
        params=WarningRevokeParams,
        needs_confirm=True,
        writes=True,
        previewable=True,
        dual_review=True,
        example="把 wrn_abcdef123456 撤销掉",
    )

    async def execute(self, targets: ResolvedTargets, params: BaseModel, ctx: ActionContext) -> ActionResult:
        assert isinstance(params, WarningRevokeParams)
        record = self._record(params.warning_id)
        if record.revoked_at:
            raise ActionError("E_ACTION_CONFLICT", f"预警 {record.warning_id} 已撤销，无需重复操作")
        revoked = self._publisher.mark_revoked(record)
        await self._save(revoked)
        return ActionResult(
            data={"warning": self._brief(revoked), "reason": params.reason},
            summary=f"预警 {record.warning_id} 已撤销（发布事实保留）",
        )


class WarningChangeLevel(_WarningControlAction):
    spec = ActionSpec(
        name="warning.change_level",
        title="调整预警等级",
        domain=ActionDomain.WARNING,
        required_role=OperatorRole.LEAD,
        params=WarningChangeLevelParams,
        needs_confirm=True,
        writes=True,
        previewable=True,
        dual_review=True,
        example="把 wrn_abcdef123456 调整为 2 级",
    )

    async def execute(self, targets: ResolvedTargets, params: BaseModel, ctx: ActionContext) -> ActionResult:
        assert isinstance(params, WarningChangeLevelParams)
        record = self._record(params.warning_id)
        before = int(record.risk_level)
        updated = self._publisher.change_level(record, params.level)
        await self._save(updated)
        return ActionResult(
            data={"warning": self._brief(updated), "before_level": before, "after_level": params.level},
            summary=f"预警 {record.warning_id} 等级 {before} → {params.level}",
        )


class WarningResend(_WarningControlAction):
    spec = ActionSpec(
        name="warning.resend",
        title="重发预警",
        domain=ActionDomain.WARNING,
        required_role=OperatorRole.LEAD,
        params=WarningResendParams,
        needs_confirm=True,
        writes=True,
        previewable=True,
        dual_review=True,
        example="把 wrn_abcdef123456 再发一遍",
    )

    async def execute(self, targets: ResolvedTargets, params: BaseModel, ctx: ActionContext) -> ActionResult:
        assert isinstance(params, WarningResendParams)
        record = self._record(params.warning_id)
        if (conflict := self._publisher.resendable(record)) is not None:
            raise ActionError("E_ACTION_CONFLICT", f"该预警不能重发（{conflict}）；未发布的请用「发布」")
        try:
            published = await self._publisher.release(record)
        except AllChannelsFailedError as exc:
            raise ActionError("E_ACTION_EXECUTION_FAILED", f"全部通道失败：{exc.message}") from exc
        await self._save(published)
        return ActionResult(data={"warning": self._brief(published)}, summary=f"预警 {record.warning_id} 已重发")


class WarningSilence(_WarningControlAction):
    spec = ActionSpec(
        name="warning.silence",
        title="静默预警提醒",
        domain=ActionDomain.WARNING,
        required_role=OperatorRole.LEAD,
        params=WarningSilenceParams,
        needs_confirm=True,
        writes=True,
        previewable=True,
        dual_review=True,
        example="把 wrn_abcdef123456 静默 30 分钟",
    )

    def __init__(self, warnings: WarningPort, publisher: PublisherPort, alerts: AlertService | None) -> None:
        super().__init__(warnings, publisher)
        self._alerts = alerts

    async def execute(self, targets: ResolvedTargets, params: BaseModel, ctx: ActionContext) -> ActionResult:
        assert isinstance(params, WarningSilenceParams)
        record = self._record(params.warning_id)
        if self._alerts is None or not self._alerts.available:
            raise ActionError("E_ALERTS_UNAVAILABLE", "告警台账未装配，无法静默提醒")
        alert = await self._alerts.silence(record.warning_id, params.minutes)
        return ActionResult(
            data={"warning": self._brief(record), "alert": alert, "minutes": params.minutes},
            summary=f"预警 {record.warning_id} 的提醒静默 {params.minutes} 分钟（升级计时推迟，不是取消）",
        )
