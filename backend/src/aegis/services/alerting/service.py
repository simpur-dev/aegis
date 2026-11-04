"""告警：把总线上的一条预警搬成值班员桌上的一张卡。

**边界**（与需求文档 D3 一致）：它不生成预警、也不发预警。生成归 `WarningService`，
发送归 `DeliveryDispatcher`，本服务只订阅既有的 `platform.alert.>` 广播，做四件事：
去重落账、确认、转派、超时升级。

为什么不做第二条发送路径：助手若自己调触达接口，"谁发的"就有两个答案，
而触达回执与 ≤20min 指标只认 `WarningRecord.deliveries`。告警只是**呈现与处置**，
不是发布。

内存形态也实现（`AlertStore`），但重启即失——`persistent` 如实外露，不假装。
"""

from __future__ import annotations

from typing import Any

from aegis.domain.alert import Alert
from aegis.storage.store import SupportsAlerts


class AlertUnavailableError(RuntimeError):
    """当前形态没有告警台账（理论上不会：内存与持久两套都实现，留作装配缺失的出口）。"""


class AlertNotFoundError(LookupError):
    def __init__(self, alert_id: str) -> None:
        super().__init__(f"告警不存在：{alert_id}")
        self.alert_id = alert_id


class AlertStateConflictError(RuntimeError):
    """告警已合并/已关闭，或状态不允许这次处置。"""

    def __init__(self, alert_id: str, status: str) -> None:
        super().__init__(f"告警当前状态为 {status}，不接受这次处置")
        self.alert_id = alert_id
        self.status = status


class AlertService:
    def __init__(
        self,
        ledger: SupportsAlerts | None,
        *,
        persistent: bool = False,
        escalation_minutes: int = 5,
    ) -> None:
        self._ledger = ledger
        self._persistent = persistent
        self._escalation_minutes = max(1, int(escalation_minutes))

    @property
    def available(self) -> bool:
        return self._ledger is not None

    @property
    def persistent(self) -> bool:
        return self._persistent

    @property
    def escalation_minutes(self) -> int:
        return self._escalation_minutes

    def _require(self) -> SupportsAlerts:
        if self._ledger is None:
            raise AlertUnavailableError("告警台账未装配")
        return self._ledger

    # ---------- 写入：来自总线广播 ----------

    async def raise_from_broadcast(self, payload: dict[str, Any]) -> tuple[dict[str, Any] | None, bool]:
        """把广播落成告警。返回 (记录, 是否新建)；形状不合法的广播返回 (None, False)。

        重复广播是常态（每个订阅者都会收到、断线重连后可能补发），
        去重键是 `warning_id`：同一预警只留一条，不改等级、不重置升级计时。
        `drill` 只有明确为真才算演练——缺字段时按真实告警处理，宁可多提醒一次。
        """
        alert = alert_from_broadcast(payload)
        if alert is None:
            return None, False
        return await self._require().raise_alert(
            alert_id=alert.alert_id,
            warning_id=alert.warning_id,
            level=alert.level,
            region_code=alert.region_code,
            drill=alert.drill,
        )

    # ---------- 读 ----------

    async def list_alerts(
        self,
        *,
        open_only: bool = True,
        status: str | None = None,
        level: int | None = None,
        drill: bool | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        return await self._require().list_alerts(
            open_only=open_only,
            status=status,
            level=level,
            drill=drill,
            limit=limit,
        )

    async def get(self, alert_id: str) -> dict[str, Any]:
        record = await self._require().get_alert(alert_id)
        if record is None:
            raise AlertNotFoundError(alert_id)
        return record

    # ---------- 处置 ----------

    async def ack(self, alert_id: str, operator_id: str) -> tuple[dict[str, Any], bool]:
        """接手。返回 (记录, 是否本次才确认)。

        重复确认不报错：值班员双击、两个标签页都点，是现场的常态而不是异常。
        `already=True` 表示这条早已被谁确认过，界面照常显示"已接手"，不弹错误。
        """
        record = await self._require().ack_alert(alert_id, operator_id)
        if record is not None:
            return record, False
        current = await self.get(alert_id)
        status = str(current.get("status", ""))
        if status == "acked":
            return current, True
        raise AlertStateConflictError(alert_id, status)

    async def transfer(self, alert_id: str, assignee: str) -> dict[str, Any]:
        record = await self._require().transfer_alert(alert_id, assignee)
        if record is not None:
            return record
        current = await self.get(alert_id)
        raise AlertStateConflictError(alert_id, str(current.get("status", "")))

    async def snooze(self, alert_id: str, minutes: int) -> dict[str, Any]:
        record = await self._require().snooze_alert(alert_id, max(1, min(int(minutes), 240)))
        if record is not None:
            return record
        current = await self.get(alert_id)
        raise AlertStateConflictError(alert_id, str(current.get("status", "")))

    async def escalate_due(self) -> list[dict[str, Any]]:
        """把超时未确认的真实告警升级。返回**本实例抢到**的那些（多副本幂等）。"""
        return await self._require().escalate_due(after_minutes=self._escalation_minutes)

    # ---------- 便于测试与能力面的静态事实 ----------

    def describe(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "persistent": self.persistent,
            "escalation_minutes": self.escalation_minutes,
        }


def alert_from_broadcast(payload: dict[str, Any]) -> Alert | None:
    """广播 → 领域模型的纯函数（供单测与注入脚本共用，避免两处各解析一遍）。"""
    warning_id = str(payload.get("warning_id") or "").strip()
    regions = payload.get("regions")
    if not warning_id or not isinstance(regions, list) or not regions:
        return None
    level = payload.get("risk_level", payload.get("level"))
    try:
        level_int = int(level)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not 1 <= level_int <= 4:
        return None
    return Alert(
        warning_id=warning_id,
        level=level_int,
        region_code=str(regions[0]),
        drill=payload.get("drill") is True,
    )
