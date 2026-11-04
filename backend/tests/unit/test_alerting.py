"""告警服务的状态机：去重、确认幂等、转派不覆盖确认人、升级只命中该升的。

内存台账与 SQL 那份共享同一套不变量；这里的行为断言同样适用于生产实现
（SQL 侧另有集成用例核对 `UPDATE ... WHERE` 的原子性）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from aegis.domain.alert import AlertStatus
from aegis.services.alerting import (
    AlertNotFoundError,
    AlertService,
    AlertStateConflictError,
    alert_from_broadcast,
)
from aegis.storage.store import PlatformStore


def _payload(*, warning_id: str = "wrn_" + "a" * 20, level: int = 1, region: str = "540121", drill: bool = False) -> dict:
    """与 `pipeline/stages.py::_broadcast` 同形状的广播 payload。"""
    return {"warning_id": warning_id, "risk_level": level, "regions": [region], "title": "演练", "drill": drill}


@pytest.fixture()
def service() -> AlertService:
    return AlertService(PlatformStore().alerts, persistent=False, escalation_minutes=5)


@pytest.mark.asyncio
async def test_同一预警只落一条账(service: AlertService) -> None:
    first, created_first = await service.raise_from_broadcast(_payload())
    again, created_again = await service.raise_from_broadcast(_payload())
    assert first is not None and again is not None
    assert created_first is True and created_again is False
    assert first["alert_id"] == again["alert_id"]


@pytest.mark.asyncio
async def test_形状不合法的广播被跳过而不是崩(service: AlertService) -> None:
    for bad in (
        {},
        {"warning_id": "wrn_" + "b" * 20, "risk_level": 1},
        {"warning_id": "wrn_" + "c" * 20, "regions": [], "risk_level": 1},
        {"warning_id": "wrn_" + "d" * 20, "regions": ["540121"], "risk_level": 9},
        {"warning_id": "wrn_" + "e" * 20, "regions": ["540121"], "risk_level": "red"},
    ):
        record, created = await service.raise_from_broadcast(bad)
        assert record is None and created is False, bad


@pytest.mark.asyncio
async def test_接手幂等_重复确认不算错(service: AlertService) -> None:
    record, _ = await service.raise_from_broadcast(_payload())
    assert record is not None
    first, already_first = await service.ack(record["alert_id"], "op-duty")
    again, already_again = await service.ack(record["alert_id"], "op-duty")
    assert already_first is False and already_again is True
    assert first["status"] == AlertStatus.ACKED.value == again["status"]
    assert first["ack_by"] == "op-duty" and again["ack_by"] == "op-duty"


@pytest.mark.asyncio
async def test_转派只改处置人_不覆盖确认人(service: AlertService) -> None:
    record, _ = await service.raise_from_broadcast(_payload())
    assert record is not None
    await service.ack(record["alert_id"], "op-duty")
    moved = await service.transfer(record["alert_id"], "op-lead")
    assert moved["assigned_to"] == "op-lead"
    assert moved["ack_by"] == "op-duty"
    assert moved["status"] == AlertStatus.ACKED.value


@pytest.mark.asyncio
async def test_稍后只改提醒时间_状态不动(service: AlertService) -> None:
    record, _ = await service.raise_from_broadcast(_payload())
    assert record is not None
    snoozed = await service.snooze(record["alert_id"], 15)
    assert snoozed["status"] == AlertStatus.RAISED.value
    assert snoozed["snooze_until"] is not None


@pytest.mark.asyncio
async def test_终态不接受处置(service: AlertService) -> None:
    """内存台账没有 close 端点（合并/关闭在 P3）：直接把台账行拨到终态，验证处置层不越过去。"""
    record, _ = await service.raise_from_broadcast(_payload())
    assert record is not None
    ledger = service._require()
    ledger._rows[record["alert_id"]]["status"] = "closed"
    with pytest.raises(AlertStateConflictError):
        await service.transfer(record["alert_id"], "op-lead")
    with pytest.raises(AlertStateConflictError):
        await service.ack(record["alert_id"], "op-duty")


@pytest.mark.asyncio
async def test_不存在的告警_404_语义(service: AlertService) -> None:
    with pytest.raises(AlertNotFoundError):
        await service.get("alt_00000000ffffffff")


@pytest.mark.asyncio
async def test_升级只命中未确认的真实告警(service: AlertService) -> None:
    real, _ = await service.raise_from_broadcast(_payload(warning_id="wrn_" + "1" * 20, drill=False))
    drill, _ = await service.raise_from_broadcast(_payload(warning_id="wrn_" + "2" * 20, drill=True))
    assert real is not None and drill is not None
    await service.ack(drill["alert_id"], "op-duty")

    # 把创建时间拨过升级窗口（服务配的是 5 分钟）：等价于"这条告警已经晾了十分钟"
    ledger = service._require()
    ledger._rows[real["alert_id"]]["created_at"] = datetime.now(UTC) - timedelta(minutes=10)

    escalated = await service.escalate_due()
    ids = {row["alert_id"] for row in escalated}
    assert real["alert_id"] in ids
    assert drill["alert_id"] not in ids

    after = await service.get(real["alert_id"])
    assert after["status"] == AlertStatus.ESCALATED.value
    assert after["escalated_at"] is not None


@pytest.mark.asyncio
async def test_稍后提醒挡住的告警_到期才升级(service: AlertService) -> None:
    record, _ = await service.raise_from_broadcast(_payload(warning_id="wrn_" + "3" * 20))
    assert record is not None
    await service.snooze(record["alert_id"], 30)

    ledger = service._require()
    row = ledger._rows[record["alert_id"]]
    row["created_at"] = datetime.now(UTC) - timedelta(minutes=10)
    assert await service.escalate_due() == []

    # 提醒时间拨到过去：稍后只是推迟升级，不是取消升级
    row["snooze_until"] = datetime.now(UTC) - timedelta(minutes=1)
    escalated = await service.escalate_due()
    assert [item["alert_id"] for item in escalated] == [record["alert_id"]]


def test_广播到领域的纯函数() -> None:
    alert = alert_from_broadcast(_payload(level=2, drill=True))
    assert alert is not None
    assert alert.level == 2 and alert.drill is True and alert.region_code == "540121"
    assert alert.open is True
    assert alert_from_broadcast({"warning_id": "wrn_x"}) is None
