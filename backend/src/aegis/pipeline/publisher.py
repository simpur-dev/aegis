"""预警发布：平台唯一的「下发 + 广播」实现。

为什么必须有这一层：`HazardStages._execute`、工作流 `publish_warning`、助手的
`warning.publish` / `warning.resend` 是三条入口。升级前主链路会广播、工作流那条不广播，
助手再写一条就是第三份语义。现在三者都调 `release()`；读视图写哪一份由调用方决定
（主链路写自己的段缓存，工作流与助手写 `store.warnings`），发布动作本身只有一处。

边界：这里不生成预警（`WarningService` 负责）、不读写存储、不发站内通知。
撤销与改级是纯领域操作（`mark_revoked` / `change_level`），返回新记录由调用方 `put`——
Publisher 反握 `WarningPort` 会让主链路那条只写段缓存的路径无从装配。
"""

from __future__ import annotations

from aegis.bus import subjects
from aegis.bus.gateway import AgentGateway
from aegis.domain.enums import Action, RiskLevel
from aegis.domain.messages import WarningRecord, make_event, now_iso
from aegis.services.response.delivery import DeliveryDispatcher


class WarningPublisher:
    def __init__(self, *, dispatcher: DeliveryDispatcher | None, gateway: AgentGateway) -> None:
        self._dispatcher = dispatcher
        self._gateway = gateway

    @property
    def dispatcher_available(self) -> bool:
        """没有交付腿时只标发布、不伪造回执——这条降级口径必须如实外露。"""
        return self._dispatcher is not None

    async def release(
        self,
        record: WarningRecord,
        *,
        trace: str | None = None,
        drill: bool = False,
    ) -> WarningRecord:
        """下发并广播。`AllChannelsFailedError` 抛给调用方，由它按自己的链路语义记账。

        主链路要把失败写进 `ChainResult.errors`，动作要变成一次 4xx，两边不该被 Publisher 吞掉。
        """
        if self._dispatcher is None:
            record.released_at = now_iso()
        else:
            await self._dispatcher.dispatch(record)
        await self._broadcast(record, trace or record.trace_id, drill=drill)
        return record

    async def _broadcast(self, record: WarningRecord, trace: str, *, drill: bool) -> None:
        """与升级前 `HazardStages._broadcast` 逐字同形：告警台账与 SSE 都认这个 payload。"""
        event = make_event(
            source="platform.pipeline",
            action=Action.EXECUTE_ACK.value,
            trace_id=trace,
            target=f"platform.broadcast_{int(record.risk_level)}",
            payload={
                "warning_id": record.warning_id,
                "risk_level": int(record.risk_level),
                "regions": record.region_codes,
                "title": record.title_zh,
                "drill": bool(drill),
            },
        )
        await self._gateway.publish_to(subjects.alert(int(record.risk_level), record.region_codes[0]), event)

    # ---------- 控制动作：纯领域操作，不碰 I/O ----------

    def publishable(self, record: WarningRecord) -> str | None:
        """返回不可发布的错误码；None 表示可以首次发布。已发布的走 `resend`，不静默重发。"""
        if record.revoked_at:
            return "E_WARNING_REVOKED"
        if record.released_at:
            return "E_WARNING_ALREADY_RELEASED"
        return None

    def resendable(self, record: WarningRecord) -> str | None:
        if record.revoked_at:
            return "E_WARNING_REVOKED"
        if not record.released_at:
            return "E_WARNING_NOT_RELEASED"
        return None

    def mark_revoked(self, record: WarningRecord) -> WarningRecord:
        return record.model_copy(update={"revoked_at": now_iso()})

    def change_level(self, record: WarningRecord, level: int) -> WarningRecord:
        return record.model_copy(update={"risk_level": RiskLevel(int(level))})
