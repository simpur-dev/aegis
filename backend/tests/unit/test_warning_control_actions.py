"""P1/P2 收尾：预警控制面的单一真源与五个 L3 动作。

钉住三件容易回潮的事：
1. `WarningRecord.status` 是派生状态的唯一出处，撤销优先于送达回执、且不改写发布事实；
2. 主链路 / 工作流画布 / 助手动作共用 `WarningPublisher.release()`，没有第二条下发+广播路径；
3. 已发布不能再"首次发布"、草稿不能重发、撤销后两者都拒绝——冲突要显式报，不能静默重发。
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from aegis.domain.enums import RiskLevel
from aegis.domain.messages import DeliveryAttempt, WarningRecord
from aegis.persistence.rows import WARNING_COLUMNS, warning_params, warning_status
from aegis.pipeline.publisher import WarningPublisher
from aegis.services.assistant.actions.warning_control import (
    WarningChangeLevel,
    WarningChangeLevelParams,
    WarningPublish,
    WarningPublishParams,
    WarningResend,
    WarningResendParams,
    WarningRevoke,
    WarningRevokeParams,
    WarningSilence,
    WarningSilenceParams,
)
from aegis.services.assistant.base import ActionContext, ActionDomain, ActionError
from aegis.services.assistant.intent import IntentClassifier
from aegis.services.assistant.registry import ActionRegistry
from aegis.storage.store import WarningStore

WRN = "wrn_abcdef123456"
MOMENT = "2026-11-08T03:00:00Z"


def warning(**overrides: Any) -> WarningRecord:
    base: dict[str, Any] = {
        "warning_id": WRN,
        "event_id": "evt_0123456789ab",
        "trace_id": "trc_0123456789abcdef",
        "hazard_type": "debris_flow",
        "region_codes": ["540121"],
        "risk_level": RiskLevel.RED,
        "title_zh": "泥石流红色预警",
        "body_zh": "请立即转移",
        "channels": ["sms"],
        "audiences": ["residents"],
    }
    base.update(overrides)
    return WarningRecord(**base)


def delivered() -> list[DeliveryAttempt]:
    return [
        DeliveryAttempt(channel="sms", audience_count=500, status="delivered", attempted_at=MOMENT, receipt_at=MOMENT),
        DeliveryAttempt(channel="wechat", audience_count=120, status="delivered", attempted_at=MOMENT, receipt_at=MOMENT),
    ]


class FakeGateway:
    """只记录广播，不连总线；`publish_to` 的调用次数就是发布链被调用的次数。"""

    def __init__(self) -> None:
        self.sent: list[tuple[str, Any]] = []

    async def publish_to(self, subject: str, event: Any) -> None:
        self.sent.append((subject, event))


class FakeDispatcher:
    def __init__(self) -> None:
        self.calls = 0

    async def dispatch(self, record: WarningRecord) -> None:
        self.calls += 1
        record.deliveries = delivered()
        record.released_at = MOMENT


class FakeAlerts:
    available = True
    minutes: int = 0

    async def silence(self, warning_id: str, minutes: int) -> dict[str, Any]:
        self.minutes = minutes
        return {"warning_id": warning_id, "status": "raised", "snooze_until": MOMENT}


def ctx() -> ActionContext:
    return ActionContext(session_id="as_control_test")


def setup() -> tuple[WarningStore, WarningPublisher, FakeGateway]:
    store = WarningStore()
    gateway = FakeGateway()
    publisher = WarningPublisher(dispatcher=None, gateway=gateway)
    return store, publisher, gateway


async def run_action(action: Any, params: Any) -> dict[str, Any]:
    targets = await action.resolve(params, ctx())
    result = await action.execute(targets, params, ctx())
    return result.data


def ledger_row(*, warning_id: str = WRN, result: dict[str, Any] | None = None) -> dict[str, Any]:
    """动作台账那一行的最小形状：补偿只从这里读参数与执行前状态。"""
    return {"action_id": "act_0123456789abcdef", "resolved_args": {"warning_id": warning_id}, "result": result or {}}


# ---------- 派生状态：唯一真源 ----------


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({}, "draft"),
        ({"released_at": MOMENT}, "released"),
        ({"released_at": MOMENT, "deliveries": delivered()}, "delivered"),
        (
            {
                "released_at": MOMENT,
                "deliveries": [
                    DeliveryAttempt(channel="sms", audience_count=1, status="delivered", attempted_at=MOMENT),
                    DeliveryAttempt(channel="wechat", audience_count=1, status="failed", attempted_at=MOMENT),
                ],
            },
            "partial",
        ),
        (
            {
                "released_at": MOMENT,
                "deliveries": [DeliveryAttempt(channel="sms", audience_count=1, status="failed", attempted_at=MOMENT)],
            },
            "failed",
        ),
        ({"revoked_at": MOMENT}, "revoked"),
    ],
)
def test_派生状态只由WarningRecord决定且落库映射同源(kwargs: dict[str, Any], expected: str) -> None:
    record = warning(**kwargs)
    assert record.status == expected
    assert warning_status(record) == expected


def test_撤销优先于送达回执且不抹掉发布事实() -> None:
    record = warning(released_at=MOMENT, deliveries=delivered())
    revoked = record.model_copy(update={"revoked_at": MOMENT})

    assert record.status == "delivered"
    assert revoked.status == "revoked"
    assert revoked.released_at == record.released_at
    assert revoked.deliveries == record.deliveries, "撤销是后续控制动作，不能改写触达指标的依据"


def test_落库列与参数同序同长且含撤销时刻() -> None:
    record = warning(revoked_at=MOMENT)
    assert "revoked_at" in WARNING_COLUMNS
    assert len(warning_params(record)) == len(WARNING_COLUMNS)


@pytest.mark.asyncio
async def test_读视图按派生状态过滤且同ID覆盖不追加() -> None:
    store = WarningStore()
    await store.put(warning(warning_id="wrn_aaaaaaaaaaaa", released_at=MOMENT))
    await store.put(warning(warning_id="wrn_bbbbbbbbbbbb", released_at=MOMENT, revoked_at=MOMENT))
    await store.put(warning(warning_id="wrn_bbbbbbbbbbbb", released_at=MOMENT, revoked_at=MOMENT))

    assert store.size == 2, "同 ID 覆盖而不是追加，否则撤销后列表里两条并存"
    assert [row.warning_id for row in store.list(status="revoked")] == ["wrn_bbbbbbbbbbbb"]
    assert len(store.list(status="released")) == 1
    assert len(store.list(status="draft")) == 0


# ---------- 发布链：只有一处下发 + 广播 ----------


@pytest.mark.asyncio
async def test_无交付腿时只标发布不造回执且广播一次() -> None:
    _store, publisher, gateway = setup()
    result = await publisher.release(warning())

    assert result.released_at is not None
    assert result.deliveries == [], "没有交付腿就不该有送达记录（≤20min 指标的分母）"
    assert len(gateway.sent) == 1
    subject, event = gateway.sent[0]
    assert subject.startswith("platform.alert.")
    assert event.payload["warning_id"] == WRN


@pytest.mark.asyncio
async def test_有交付腿时dispatch与广播各一次且回执写回同一记录() -> None:
    dispatcher = FakeDispatcher()
    gateway = FakeGateway()
    publisher = WarningPublisher(dispatcher=dispatcher, gateway=gateway)  # type: ignore[arg-type]

    result = await publisher.release(warning(), drill=True)

    assert dispatcher.calls == 1
    assert len(gateway.sent) == 1
    assert result.status == "delivered"
    assert gateway.sent[0][1].payload["drill"] is True
    assert publisher.dispatcher_available is True


def test_发布资格判据互斥() -> None:
    _store, publisher, _gateway = setup()
    draft, released, revoked = warning(), warning(released_at=MOMENT), warning(released_at=MOMENT, revoked_at=MOMENT)

    assert publisher.publishable(draft) is None
    assert publisher.publishable(released) == "E_WARNING_ALREADY_RELEASED"
    assert publisher.publishable(revoked) == "E_WARNING_REVOKED"
    assert publisher.resendable(draft) == "E_WARNING_NOT_RELEASED"
    assert publisher.resendable(released) is None
    assert publisher.resendable(revoked) == "E_WARNING_REVOKED"


# ---------- 五个控制动作 ----------


@pytest.mark.asyncio
async def test_首次发布走同一发布链并覆盖读视图() -> None:
    store, publisher, gateway = setup()
    await store.put(warning())

    data = await run_action(WarningPublish(store, publisher), WarningPublishParams(warning_id=WRN))

    assert data["warning"]["status"] == "released"
    assert len(gateway.sent) == 1
    assert store.size == 1
    assert store.get(WRN) is not None and store.get(WRN).released_at is not None  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_已发布不能首次发布_草稿不能重发() -> None:
    store, publisher, _gateway = setup()
    await store.put(warning(released_at=MOMENT))
    with pytest.raises(ActionError) as caught:
        await run_action(WarningPublish(store, publisher), WarningPublishParams(warning_id=WRN))
    assert caught.value.code == "E_ACTION_CONFLICT"

    draft_store, draft_publisher, _ = setup()
    await draft_store.put(warning())
    with pytest.raises(ActionError) as caught:
        await run_action(WarningResend(draft_store, draft_publisher), WarningResendParams(warning_id=WRN))
    assert caught.value.code == "E_ACTION_CONFLICT"


@pytest.mark.asyncio
async def test_撤销与改级都不改写发布事实且只留一条记录() -> None:
    store, publisher, _gateway = setup()
    original = warning(released_at=MOMENT, deliveries=delivered())
    await store.put(original)

    revoked = await run_action(WarningRevoke(store, publisher), WarningRevokeParams(warning_id=WRN, reason="误报"))
    assert revoked["warning"]["status"] == "revoked"
    assert revoked["reason"] == "误报"
    saved = store.get(WRN)
    assert saved is not None
    assert saved.released_at == MOMENT
    assert saved.deliveries == original.deliveries
    assert store.size == 1

    changed = await run_action(WarningChangeLevel(store, publisher), WarningChangeLevelParams(warning_id=WRN, level=3))
    assert changed["before_level"] == 1
    assert changed["after_level"] == 3
    updated = store.get(WRN)
    assert updated is not None and int(updated.risk_level) == 3
    assert updated.released_at == MOMENT


@pytest.mark.asyncio
async def test_重发走发布链且撤销后拒绝重发() -> None:
    store = WarningStore()
    gateway = FakeGateway()
    dispatcher = FakeDispatcher()
    publisher = WarningPublisher(dispatcher=dispatcher, gateway=gateway)  # type: ignore[arg-type]
    await store.put(warning(released_at=MOMENT))

    data = await run_action(WarningResend(store, publisher), WarningResendParams(warning_id=WRN))
    assert data["warning"]["status"] == "delivered"
    assert dispatcher.calls == 1, "重发必须真的再走一次交付腿，不是只改个时间"
    assert len(gateway.sent) == 1

    revoked_store, revoked_publisher, _ = setup()
    await revoked_store.put(warning(released_at=MOMENT, revoked_at=MOMENT))
    with pytest.raises(ActionError) as caught:
        await run_action(WarningResend(revoked_store, revoked_publisher), WarningResendParams(warning_id=WRN))
    assert caught.value.code == "E_ACTION_CONFLICT"


@pytest.mark.asyncio
async def test_静默只推迟告警且缺台账时如实报错() -> None:
    store, publisher, _gateway = setup()
    await store.put(warning(released_at=MOMENT))
    alerts = FakeAlerts()
    action = WarningSilence(store, publisher, alerts)  # type: ignore[arg-type]

    data = await run_action(action, WarningSilenceParams(warning_id=WRN, minutes=45))
    assert alerts.minutes == 45
    assert data["alert"]["status"] == "raised", "静默不改告警状态"
    assert store.get(WRN) is not None and store.get(WRN).revoked_at is None  # type: ignore[union-attr]

    missing = WarningSilence(store, publisher, None)
    with pytest.raises(ActionError) as caught:
        await run_action(missing, WarningSilenceParams(warning_id=WRN))
    assert caught.value.code == "E_ALERTS_UNAVAILABLE"


# ---------- 撤销后的业务补偿（可逆动作才做） ----------


@pytest.mark.asyncio
async def test_发布撤回的补偿只标撤销且保住发布事实() -> None:
    store, publisher, _gateway = setup()
    action = WarningPublish(store, publisher)
    await store.put(warning())
    await run_action(action, WarningPublishParams(warning_id=WRN))
    published = store.get(WRN)
    assert published is not None and published.released_at is not None

    result = await action.revoke(ledger_row(result={"warning": {"warning_id": WRN}}), ctx())

    assert result.data["warning"]["status"] == "revoked"
    saved = store.get(WRN)
    assert saved is not None
    assert saved.revoked_at is not None
    assert saved.released_at == published.released_at, "补偿不改写发布事实"
    assert saved.deliveries == published.deliveries
    assert store.size == 1, "补偿覆盖同一条读视图记录，不追加"

    again = await action.revoke(ledger_row(), ctx())
    assert "无需补偿" in again.summary, "重复补偿是幂等的，不该再改一次"


@pytest.mark.asyncio
async def test_改级撤回的补偿按台账里的执行前等级还原() -> None:
    store, publisher, _gateway = setup()
    action = WarningChangeLevel(store, publisher)
    await store.put(warning(released_at=MOMENT))
    data = await run_action(action, WarningChangeLevelParams(warning_id=WRN, level=3))
    assert data["before_level"] == 1

    result = await action.revoke(ledger_row(result={"before_level": 1}), ctx())

    assert result.data["restored_level"] == 1
    saved = store.get(WRN)
    assert saved is not None and int(saved.risk_level) == 1
    assert saved.released_at == MOMENT, "改级补偿同样不改写发布事实"

    again = await action.revoke(ledger_row(result={"before_level": 1}), ctx())
    assert "无需补偿" in again.summary


@pytest.mark.asyncio
async def test_改级撤回缺执行前等级时拒绝猜一个值() -> None:
    store, publisher, _gateway = setup()
    action = WarningChangeLevel(store, publisher)
    await store.put(warning(released_at=MOMENT, risk_level=RiskLevel.ORANGE))

    with pytest.raises(ActionError) as caught:
        await action.revoke(ledger_row(), ctx())

    assert caught.value.code == "E_ACTION_EXECUTION_FAILED"
    saved = store.get(WRN)
    assert saved is not None and int(saved.risk_level) == int(RiskLevel.ORANGE), "补偿失败不能顺手改点别的"


@pytest.mark.asyncio
async def test_补偿找不到目标编号时如实拒绝而不是空转() -> None:
    store, publisher, _gateway = setup()
    with pytest.raises(ActionError) as caught:
        await WarningPublish(store, publisher).revoke(ledger_row(warning_id=""), ctx())
    assert caught.value.code == "E_ACTION_CONFLICT"


def test_发布带编号的指令按写入意图路由而不是一段解释() -> None:
    """「发布 wrn_…」没有"把/请"前缀：以前会掉进 warning.explain，说半天历史定级。"""
    store, publisher, _gateway = setup()
    classifier = IntentClassifier(ActionRegistry([WarningPublish(store, publisher)], domains={ActionDomain.WARNING}))

    decided = classifier.classify(f"发布 {WRN}")
    assert decided.action == "warning.publish"
    assert decided.args["warning_id"] == WRN

    asked = classifier.classify(f"{WRN} 发布了吗")
    assert asked.action is None, "问句被当成了发布指令"


@pytest.mark.parametrize("bad", ["", "warning-1", "wrn_ABCDEF123456", "wrn_abc"])
def test_非编号目标在参数层就被拒(bad: str) -> None:
    with pytest.raises(ValidationError):
        WarningPublishParams(warning_id=bad)


@pytest.mark.asyncio
async def test_目标不在读视图时明确报找不到而不是造一条() -> None:
    store, publisher, _gateway = setup()
    with pytest.raises(ActionError) as caught:
        await run_action(WarningPublish(store, publisher), WarningPublishParams(warning_id="wrn_deadbeef9999"))
    assert caught.value.code == "E_TARGET_NOT_FOUND"
    assert store.size == 0
