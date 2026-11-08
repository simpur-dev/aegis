"""执行器第 8 步（audit.write）接线验证：audit_ops 从"有表无写入方"变成每条写动作都留痕。

§9 验收逐条钉住：
- **成功**：intent（执行前）+ result（执行后，`after` 带结果），两条共用 action_id；
- **失败**：result 行的 `after.rejected` 带错误码（业务拒绝与执行失败都算）；
- **越权**：只有一行 result，匿名尝试记 `operator_id='unknown'`；
- **只追加**：幂等重放不补行；确认/撤销/复核拒绝各自补齐对应行。
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict

from aegis.config import Settings
from aegis.domain.operator import Operator, OperatorRole
from aegis.services.assistant import ActionDomain
from aegis.services.assistant.base import Action, ActionError, ActionResult, ActionSpec, ResolvedTargets
from aegis.services.assistant.registry import ActionRegistry
from aegis.services.assistant.service import AssistantService
from aegis.storage.store import PlatformStore

LEAD = Operator(operator_id="lead-01", display_name="值班长", role=OperatorRole.LEAD)
LEAD2 = Operator(operator_id="lead-02", display_name="值班长乙", role=OperatorRole.LEAD)
DUTY = Operator(operator_id="duty-01", display_name="值班员", role=OperatorRole.DUTY)


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target: str


class _Gated(Action):
    """带确认闸的写动作替身（发布预警同构）：intent 随提案落库，result 随确认执行落库。"""

    spec = ActionSpec(
        name="warning.publish",
        title="发布预警",
        domain=ActionDomain.WARNING,
        required_role=OperatorRole.LEAD,
        params=_Params,
        needs_confirm=True,
        writes=True,
        previewable=True,
    )

    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, targets: ResolvedTargets, params: BaseModel, ctx: Any) -> ActionResult:
        self.calls += 1
        return ActionResult(summary="已发布", data={"target": _Params.model_validate(params).target})


class _Direct(Action):
    """无确认闸的写动作替身（认领同构）：一次调用直接执行，必须也有 intent+result 两行。"""

    spec = ActionSpec(
        name="task.claim",
        title="认领处置任务",
        domain=ActionDomain.TASK,
        required_role=OperatorRole.DUTY,
        params=_Params,
        writes=True,
        previewable=True,
    )

    def __init__(self, fail: ActionError | None = None) -> None:
        self.fail = fail
        self.calls = 0

    async def execute(self, targets: ResolvedTargets, params: BaseModel, ctx: Any) -> ActionResult:
        self.calls += 1
        if self.fail is not None:
            raise self.fail
        return ActionResult(summary="已认领", data={"status": "claimed"})


def settings(**overrides: Any) -> Settings:
    return Settings(env="test", bus_backend="memory", store_backend="memory", simulator_enabled=False, llm_api_key="", **overrides)


def build_service(*actions: Action, **overrides: Any) -> tuple[AssistantService, PlatformStore]:
    store = PlatformStore()
    domains = {action.spec.domain for action in actions}
    service = AssistantService(
        registry=ActionRegistry(list(actions), domains=domains),
        settings=settings(**overrides),
        control_actions=store,
        control_actions_persistent=False,
    )
    return service, store


def rows_for(store: PlatformStore, *, phase: str | None = None, action: str | None = None) -> list[dict[str, Any]]:
    rows = store.audit_snapshot()
    if phase is not None:
        rows = [row for row in rows if row["phase"] == phase]
    if action is not None:
        rows = [row for row in rows if row["action"] == action]
    return rows


# ---------- 成功：intent + result，两条共用 action_id ----------


async def test_带确认闸的写动作提案即留意图行() -> None:
    action = _Gated()
    service, store = build_service(action)
    created = await service.create_action("warning.publish", {"target": "wrn_x"}, operator=LEAD)
    intent_rows = rows_for(store, phase="intent", action="warning.publish")
    assert len(intent_rows) == 1
    row = intent_rows[0]
    assert row["action_id"] == created["action_id"]
    assert row["operator_id"] == "lead-01" and row["role"] == "lead"
    assert row["resolved_args"] == {"target": "wrn_x"}
    assert rows_for(store, phase="result") == [], "还没确认执行，不应有 result 行"


async def test_确认执行后补结果行且与意图行同号() -> None:
    action = _Gated()
    service, store = build_service(action)
    created = await service.create_action("warning.publish", {"target": "wrn_x"}, operator=LEAD)
    result = await service.confirm(action_id=str(created["action_id"]), operator=LEAD)
    assert result["status"] == "executed"
    intent_row = rows_for(store, phase="intent")[0]
    result_rows = rows_for(store, phase="result", action="warning.publish")
    assert len(result_rows) == 1
    assert result_rows[0]["action_id"] == intent_row["action_id"], "两条审计必须共用 action_id"
    assert result_rows[0]["after"]["summary"] == "已发布"


async def test_直接执行的写动作一次调用留两行() -> None:
    action = _Direct()
    service, store = build_service(action)
    outcome = await service.create_action("task.claim", {"target": "stu_x"}, operator=DUTY)
    assert outcome["status"] == "executed"
    rows = rows_for(store, action="task.claim")
    assert [row["phase"] for row in rows] == ["intent", "result"]
    assert rows[0]["action_id"] == rows[1]["action_id"]
    assert rows[1]["after"]["summary"] == "已认领"


async def test_幂等重放不补审计行() -> None:
    """幂等键只对走台账的写动作生效：首次记 intent，重放不产生第二行。"""
    action = _Gated()
    service, store = build_service(action)
    first = await service.create_action("warning.publish", {"target": "wrn_x"}, operator=LEAD, idempotency_key="replay-0001")
    assert first["replayed"] is not True
    again = await service.create_action("warning.publish", {"target": "wrn_x"}, operator=LEAD, idempotency_key="replay-0001")
    assert again["replayed"] is True
    assert len(rows_for(store, phase="intent", action="warning.publish")) == 1, "重复提交不是新意图，不追加审计行"


# ---------- 失败：result 行带 rejected ----------


async def test_业务拒绝也留结果行() -> None:
    action = _Direct(fail=ActionError("E_ACTION_CONFLICT", "该任务已被他人认领"))
    service, store = build_service(action)
    with pytest.raises(ActionError):
        await service.create_action("task.claim", {"target": "stu_x"}, operator=DUTY)
    rows = rows_for(store, action="task.claim")
    assert [row["phase"] for row in rows] == ["intent", "result"]
    assert rows[1]["after"]["rejected"] == "E_ACTION_CONFLICT"


async def test_确认后执行失败补失败结果行() -> None:
    action = _Gated()

    async def broken(targets: ResolvedTargets, params: BaseModel, ctx: Any) -> ActionResult:
        raise ActionError("E_ACTION_EXECUTION_FAILED", "通道全挂")

    action.execute = broken  # type: ignore[method-assign]
    service, store = build_service(action)
    created = await service.create_action("warning.publish", {"target": "wrn_x"}, operator=LEAD)
    # confirm() 的既有语义：执行失败被折成 rejected 回执并带原因，不向外抛
    result = await service.confirm(action_id=str(created["action_id"]), operator=LEAD)
    assert result["status"] == "rejected" and "通道全挂" in str(result.get("reason"))
    result_rows = rows_for(store, phase="result")
    assert len(result_rows) == 1 and result_rows[0]["after"]["rejected"] == "E_ACTION_EXECUTION_FAILED"


# ---------- 越权：只有一行 result，匿名记 unknown ----------


async def test_角色不足的写指令留越权行且不执行() -> None:
    action = _Gated()
    service, store = build_service(action)
    with pytest.raises(ActionError):
        await service.create_action("warning.publish", {"target": "wrn_x"}, operator=DUTY)
    rows = rows_for(store)
    assert len(rows) == 1 and rows[0]["phase"] == "result"
    assert rows[0]["after"]["rejected"] == "E_ACTION_NOT_ALLOWED"
    assert rows[0]["operator_id"] == "duty-01" and rows[0]["role"] == "duty"
    assert action.calls == 0


async def test_匿名的写指令记unknown() -> None:
    action = _Direct()
    service, store = build_service(action)
    with pytest.raises(ActionError):
        await service.create_action("task.claim", {"target": "stu_x"}, operator=None)
    rows = rows_for(store)
    assert len(rows) == 1 and rows[0]["operator_id"] == "unknown" and rows[0]["role"] == "unknown"


async def test_复核资格被拒留越权行() -> None:
    spec_with_review = ActionSpec(
        name="warning.publish",
        title="发布预警",
        domain=ActionDomain.WARNING,
        required_role=OperatorRole.LEAD,
        params=_Params,
        needs_confirm=True,
        writes=True,
        previewable=True,
        dual_review=True,
    )

    class _Reviewable(_Gated):
        spec = spec_with_review

    reviewable = _Reviewable()
    service, store = build_service(reviewable, assistant_dual_review_enabled=True)
    created = await service.create_action("warning.publish", {"target": "wrn_x"}, operator=LEAD)
    action_id = str(created["action_id"])
    before = len(store.audit_snapshot())
    with pytest.raises(ActionError):
        await service.confirm(action_id=action_id, operator=DUTY)
    rows = store.audit_snapshot()[before:]
    assert [row["phase"] for row in rows] == ["result"]
    assert rows[0]["after"]["rejected"] == "E_ACTION_REVIEW_FORBIDDEN"
    assert rows[0]["operator_id"] == "duty-01"
    assert reviewable.calls == 0


# ---------- 撤销 ----------


async def test_撤销成功补一行结果() -> None:
    action = _Gated()
    service, store = build_service(action)
    created = await service.create_action("warning.publish", {"target": "wrn_x"}, operator=LEAD)
    revoked = await service.revoke_action(action_id=str(created["action_id"]), operator=LEAD)
    assert revoked["status"] == "revoked"
    result_rows = rows_for(store, phase="result")
    assert len(result_rows) == 1 and result_rows[0]["after"] == {"revoked": True}
    assert result_rows[0]["action_id"] == created["action_id"]


# ---------- 整链冒烟：两票复核 + 审计恰好 intent/result 各一行 ----------


async def test_两票复核通过后审计恰好两行且只执行一次() -> None:
    spec_with_review = ActionSpec(
        name="warning.publish",
        title="发布预警",
        domain=ActionDomain.WARNING,
        required_role=OperatorRole.LEAD,
        params=_Params,
        needs_confirm=True,
        writes=True,
        previewable=True,
        dual_review=True,
    )

    class _Reviewable(_Gated):
        spec = spec_with_review

    reviewable = _Reviewable()
    service, store = build_service(reviewable, assistant_dual_review_enabled=True)
    created = await service.create_action("warning.publish", {"target": "wrn_x"}, operator=LEAD)
    action_id = str(created["action_id"])
    await service.confirm(action_id=action_id, operator=LEAD)
    await service.confirm(action_id=action_id, operator=LEAD2)
    phases = [row["phase"] for row in store.audit_snapshot()]
    assert phases == ["intent", "result"], "两票只对应一次执行：intent 随提案、result 随第二票后的执行"
    assert reviewable.calls == 1
