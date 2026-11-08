"""P1/P2 收尾：处置任务状态机、站内通知、`action.status` 与意图路由。

钉住四条不变量：
- `handling_tasks.version` 是乐观锁，状态已被别人改过时本次迁移不生效（409 语义）；
- 一条链路幂等映射成「一个空间 + 每个 STU 一条任务」，不在别处补建；
- 站内通知不伪造 `WarningRecord`，去重在写入前做，广播 payload 与前端分流同名；
- 带编号的祈使句必须进写动作，整体批量指令仍被越权闸拦下（助手不放大权限）。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from aegis.domain.chain import ChainResult
from aegis.domain.enums import Action
from aegis.domain.operator import Operator, OperatorRole
from aegis.services.alerting.notifier import Notifier
from aegis.services.assistant.actions.action_status import ActionStatus, ActionStatusParams
from aegis.services.assistant.actions.notify import NotifyGroup, NotifyGroupParams, NotifyLead, NotifyLeadParams
from aegis.services.assistant.actions.task_control import (
    TaskAck,
    TaskAckParams,
    TaskAssign,
    TaskAssignParams,
    TaskClaim,
    TaskClaimParams,
    TaskClose,
    TaskCloseParams,
    TaskTransfer,
    TaskTransferParams,
)
from aegis.services.assistant.base import ActionContext, ActionError
from aegis.services.assistant.intent import IntentClassifier, bulk_scope_without_target, out_of_scope_action
from aegis.services.collab.tasks import CollabService, TaskConflictError
from aegis.storage.store import HandlingTaskStore, NotificationStore

STU = "stu_0123456789abcdef"
STU2 = "stu_fedcba9876543210"
WRN = "wrn_abcdef123456"
ACT = "act_abc123"
SUBJECT = "ops.assistant.notify"


def ctx() -> ActionContext:
    return ActionContext(
        session_id="as_collab_test",
        operator=Operator(operator_id="lead-01", display_name="值班长", role=OperatorRole.LEAD),
        space_id=WRN,
    )


async def run_action(action: Any, params: Any) -> dict[str, Any]:
    targets = await action.resolve(params, ctx())
    result = await action.execute(targets, params, ctx())
    return result.data


def chain(*stu_ids: str) -> ChainResult:
    return ChainResult(
        trace_id="trc_0123456789abcdef",
        event_id="evt_0123456789ab",
        warning=SimpleNamespace(warning_id=WRN),
        task_units=[SimpleNamespace(task_unit_id=item) for item in stu_ids],
    )


class NotificationBus:
    def __init__(self) -> None:
        self.messages: list[tuple[str, Any]] = []

    async def __call__(self, subject: str, event: Any) -> None:
        self.messages.append((subject, event))


class FakeOperators:
    available = True

    def __init__(self, *roles: OperatorRole) -> None:
        self._roles = roles

    async def list_operators(self, *, active_only: bool = True, limit: int = 200) -> list[Operator]:
        return [Operator(operator_id=f"op-{index}", display_name=f"席位{index}", role=role) for index, role in enumerate(self._roles)]


class FakeActionLedger:
    def __init__(self, rows: dict[str, dict[str, Any]]) -> None:
        self._rows = rows

    async def get_action(self, action_id: str) -> dict[str, Any] | None:
        return self._rows.get(action_id)


class FakeRegistry:
    """意图分类只用 `get()` 判"这个动作在不在"，不需要真注册表。"""

    def __init__(self, names: frozenset[str]) -> None:
        self.names = names
        self.registered = {name: SimpleNamespace(name=name) for name in names}

    def get(self, name: str) -> Any | None:
        return self.registered.get(name)


def notifier_setup(*roles: OperatorRole) -> tuple[Notifier, NotificationStore, NotificationBus]:
    store = NotificationStore()
    bus = NotificationBus()
    service = Notifier(store, publish=bus, subject=SUBJECT, operators=FakeOperators(*roles))
    return service, store, bus


# ---------- 处置任务：幂等 + 乐观锁 ----------


@pytest.mark.asyncio
async def test_链路幂等映射成空间与任务() -> None:
    store = HandlingTaskStore()
    collab = CollabService(store)

    first = await collab.ensure_from_chain(chain(STU, STU2))
    second = await collab.ensure_from_chain(chain(STU, STU2))

    assert first == {"space_id": WRN, "tasks_total": 2, "tasks_created": 2}
    assert second is not None and second["tasks_created"] == 0
    assert len(await collab.list_tasks(space_id=WRN)) == 2
    assert (await collab.get(STU))["status"] == "pending"


@pytest.mark.asyncio
async def test_无预警的链路不建空间() -> None:
    store = HandlingTaskStore()
    collab = CollabService(store)
    result = ChainResult(trace_id="trc_0123456789abcdef", event_id="evt_0123456789ab", warning=None)
    assert await collab.ensure_from_chain(result) is None
    assert await collab.list_tasks() == []


@pytest.mark.asyncio
async def test_乐观锁_版本或状态不匹配时迁移不生效() -> None:
    store = HandlingTaskStore()
    await store.ensure_space(space_id=WRN, warning_id=WRN)
    await store.ensure_task(space_id=WRN, stu_id=STU)

    assert await store.claim_task(STU, "op-duty", expected_version=99) is None
    claimed = await store.claim_task(STU, "op-duty", expected_version=1)
    assert claimed is not None and claimed["status"] == "claimed" and claimed["version"] == 2
    assert await store.claim_task(STU, "op-other", expected_version=2) is None, "只有 pending 能被认领"

    acked = await store.ack_task(STU, "op-duty", expected_version=2)
    assert acked is not None and acked["status"] == "acked" and acked["version"] == 3
    assert await store.close_task(STU, expected_version=2) is None, "过期版本不能覆盖别人的结果"


@pytest.mark.asyncio
async def test_结单后一切迁移拒绝() -> None:
    store = HandlingTaskStore()
    await store.ensure_space(space_id=WRN, warning_id=WRN)
    await store.ensure_task(space_id=WRN, stu_id=STU)
    closed = await store.close_task(STU, expected_version=1)
    assert closed is not None and closed["status"] == "closed"

    assert await store.claim_task(STU, "op-duty", expected_version=2) is None
    assert await store.assign_task(STU, "lead-02", expected_version=2) is None
    assert await store.transfer_task(STU, "lead-02", expected_version=2) is None
    assert await store.close_task(STU, expected_version=2) is None


@pytest.mark.asyncio
async def test_服务层把并发冲突翻成领域异常() -> None:
    store = HandlingTaskStore()
    await store.ensure_space(space_id=WRN, warning_id=WRN)
    await store.ensure_task(space_id=WRN, stu_id=STU)
    collab = CollabService(store)

    first = await collab.claim(STU, "lead-01")
    assert first["status"] == "claimed" and first["assignee"] == "lead-01"
    with pytest.raises(TaskConflictError):
        await collab.claim(STU, "lead-02")
    assert collab.describe() == {"available": True, "persistent": False}


@pytest.mark.asyncio
async def test_五个任务动作共用一份状态机() -> None:
    store = HandlingTaskStore()
    await store.ensure_space(space_id=WRN, warning_id=WRN)
    await store.ensure_task(space_id=WRN, stu_id=STU)
    collab = CollabService(store)

    claimed = await run_action(TaskClaim(collab), TaskClaimParams(task_id=STU))
    assert claimed["task"]["status"] == "claimed"
    acked = await run_action(TaskAck(collab), TaskAckParams(task_id=STU))
    assert acked["task"]["status"] == "acked"
    assigned = await run_action(TaskAssign(collab), TaskAssignParams(task_id=STU, assignee="lead-02"))
    assert assigned["task"]["assignee"] == "lead-02" and assigned["task"]["status"] == "claimed"
    transferred = await run_action(TaskTransfer(collab), TaskTransferParams(task_id=STU, assignee="lead-03"))
    assert transferred["task"]["assignee"] == "lead-03" and transferred["task"]["status"] == "claimed"
    closed = await run_action(TaskClose(collab), TaskCloseParams(task_id=STU))
    assert closed["task"]["status"] == "closed"
    assert (await collab.get(STU))["version"] == 6


@pytest.mark.asyncio
async def test_任务动作把领域异常翻译成统一错误码() -> None:
    collab = CollabService(HandlingTaskStore())
    with pytest.raises(ActionError) as missing:
        await run_action(TaskClaim(collab), TaskClaimParams(task_id=STU))
    assert missing.value.code == "E_TARGET_NOT_FOUND"

    store = HandlingTaskStore()
    await store.ensure_space(space_id=WRN, warning_id=WRN)
    await store.ensure_task(space_id=WRN, stu_id=STU)
    await store.close_task(STU, expected_version=1)
    with pytest.raises(ActionError) as conflict:
        await run_action(TaskClaim(CollabService(store)), TaskClaimParams(task_id=STU))
    assert conflict.value.code == "E_ACTION_CONFLICT"


# ---------- 站内通知 ----------


@pytest.mark.asyncio
async def test_群发去重在写入前做且广播与前端分流同名() -> None:
    service, _store, bus = notifier_setup(OperatorRole.LEAD)

    assert Action.NOTIFY_SENT.value == "notify.sent"
    records = await service.notify_many(["lead-01", "lead-01", " lead-01 ", "lead-02", ""], "立即会商", sender="duty-01")

    assert len(records) == 2, "同一席位只写一条，不靠前端去重"
    assert len(bus.messages) == 2
    assert all(subject == SUBJECT for subject, _ in bus.messages)
    assert [event.payload["recipient"] for _, event in bus.messages] == ["lead-01", "lead-02"]
    assert all(event.action == "notify.sent" for _, event in bus.messages)
    assert len(await service.list_for("lead-01", unread_only=True)) == 1


@pytest.mark.asyncio
async def test_通知已读只能由收件人自己标且幂等() -> None:
    service, store, _bus = notifier_setup()
    await service.notify(recipient="lead-01", message="复核预警", kind="lead")
    rows = await service.list_for("lead-01")
    notification_id = rows[0]["id"]

    assert await service.mark_read(notification_id, "lead-02") is None, "不能替别人标已读"
    marked = await service.mark_read(notification_id, "lead-01")
    assert marked is not None and marked["read_at"] is not None
    assert await service.mark_read(notification_id, "lead-01") is None, "重复标记不改时间"
    assert await service.list_for("lead-01", unread_only=True) == []
    assert len(await store.list_notifications(recipient="lead-01")) == 1


@pytest.mark.asyncio
async def test_通知值班长动作从席位台账找lead而不臆造ID() -> None:
    service, _store, bus = notifier_setup(OperatorRole.DUTY, OperatorRole.LEAD, OperatorRole.ADMIN)
    assert await service.lead_ids() == ["op-1", "op-2"]

    data = await run_action(NotifyLead(service), NotifyLeadParams(message="通麦镇需要复核"))
    assert data["notification"]["recipient"] == "op-1"
    assert data["notification"]["sender"] == "lead-01"
    assert len(bus.messages) == 1

    empty = Notifier(NotificationStore(), publish=None, subject=SUBJECT, operators=FakeOperators(OperatorRole.DUTY))
    with pytest.raises(ActionError) as caught:
        await run_action(NotifyLead(empty), NotifyLeadParams(message="通麦镇需要复核"))
    assert caught.value.code == "E_TARGET_NOT_FOUND"


@pytest.mark.asyncio
async def test_群发动作按去重后的席位各写一条() -> None:
    service, _store, bus = notifier_setup()
    data = await run_action(NotifyGroup(service), NotifyGroupParams(operator_ids=["lead-01", "lead-02", "lead-01"], message="立即会商"))
    assert data["count"] == 2
    assert len(bus.messages) == 2


# ---------- action.status ----------


@pytest.mark.asyncio
async def test_动作状态复用台账且不新增查询路径() -> None:
    ledger = FakeActionLedger({ACT: {"action_id": ACT, "action": "warning.revoke", "status": "executed"}})
    data = await run_action(ActionStatus(ledger), ActionStatusParams(action_id=ACT))
    assert data["action"]["status"] == "executed"

    with pytest.raises(ActionError) as missing:
        await run_action(ActionStatus(ledger), ActionStatusParams(action_id="act_deadbeef"))
    assert missing.value.code == "E_TARGET_NOT_FOUND"

    with pytest.raises(ActionError) as unavailable:
        await run_action(ActionStatus(None), ActionStatusParams(action_id=ACT))
    assert unavailable.value.code == "E_ACTION_EXECUTION_FAILED"


# ---------- 意图路由与越权闸 ----------


ALL_ACTIONS = frozenset(
    {
        "warning.explain",
        "chain.explain",
        "task.get",
        "warning.list",
        "notify.lead",
        "notify.group",
        "action.status",
        "warning.publish",
        "warning.revoke",
        "warning.change_level",
        "warning.silence",
        "warning.resend",
        "task.claim",
        "task.ack",
        "task.assign",
        "task.transfer",
        "task.close",
    }
)


@pytest.mark.parametrize(
    ("text", "expected", "arg_key", "arg_value"),
    [
        (f"把 {WRN} 撤销", "warning.revoke", "warning_id", WRN),
        (f"把 {WRN} 调整为 2 级", "warning.change_level", "level", 2),
        (f"把 {WRN} 静默 30 分钟", "warning.silence", "minutes", 30),
        (f"把 {STU} 认领", "task.claim", "task_id", STU),
        (f"把 {STU} 派给 lead-02", "task.assign", "assignee", "lead-02"),
        (f"动作状态 {ACT}", "action.status", "action_id", ACT),
        ("群发通知 @lead-01 @lead-02 立即会商", "notify.group", "operator_ids", ["lead-01", "lead-02"]),
    ],
)
def test_带编号的祈使句进写动作(text: str, expected: str, arg_key: str, arg_value: Any) -> None:
    intent = IntentClassifier(FakeRegistry(ALL_ACTIONS)).classify(text)
    assert intent.action == expected
    assert intent.args.get(arg_key) == arg_value


def test_整体批量指令仍被越权闸拦且点名编号后放行() -> None:
    assert out_of_scope_action("把全网预警都删掉") == ("warning.revoke", "删掉")
    assert bulk_scope_without_target("把全网预警都删掉") is True
    assert bulk_scope_without_target("把540121的预警全部删除") is True
    assert bulk_scope_without_target(f"把 {WRN} 都撤销") is False
    assert bulk_scope_without_target(f"把全网预警撤销，只保留 {WRN}") is False
    assert bulk_scope_without_target("这条预警可以撤回吗") is False
