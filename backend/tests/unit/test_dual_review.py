"""双人复核（P3 第二批）：配置默认关、票的资格与两票迁移的状态机。

钉住四条不变量：
- 配置默认关闭时逐字回到 D5「指令即执行」：`review_required=0`，本人一次确认即执行；
- 打开后只对注册表里 `dual_review=True` 的动作生效，其余动作不加票；
- 一人一票、两票必须不同席位且达到角色门槛；同一席位投第二票被明确拒绝；
- 第二票与 `proposed → executing` 在台账同一条原子迁移里完成，确认闸不能被单票绕过。
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
from aegis.services.assistant.review import DualReview, ReviewPolicy
from aegis.services.assistant.service import AssistantService
from aegis.storage.store import PlatformStore

LEAD1 = Operator(operator_id="lead-01", display_name="值班长甲", role=OperatorRole.LEAD)
LEAD2 = Operator(operator_id="lead-02", display_name="值班长乙", role=OperatorRole.LEAD)
DUTY = Operator(operator_id="duty-01", display_name="值班员", role=OperatorRole.DUTY)


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target: str


class _Publish(Action):
    """最小高危动作替身：只统计执行次数，不做任何业务。"""

    spec = ActionSpec(
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

    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, targets: ResolvedTargets, params: BaseModel, ctx: Any) -> ActionResult:
        self.calls += 1
        return ActionResult(summary="已发布", data={"target": _Params.model_validate(params).target})


def settings(**overrides: Any) -> Settings:
    return Settings(env="test", bus_backend="memory", store_backend="memory", simulator_enabled=False, llm_api_key="", **overrides)


def build_service(action: _Publish, **overrides: Any) -> AssistantService:
    registry = ActionRegistry([action], domains={ActionDomain.WARNING})
    return AssistantService(
        registry=registry,
        settings=settings(**overrides),
        control_actions=PlatformStore(),
        control_actions_persistent=False,
    )


# ---------- ReviewPolicy：配置与资格的单一判据 ----------


def test_默认关闭时任何动作都不要复核票() -> None:
    policy = ReviewPolicy()  # 默认 False
    assert policy.votes_required(_Publish.spec) == 0
    assert ReviewPolicy(enabled=False).votes_required(_Publish.spec) == 0


def test_打开后只对标记了双人复核的动作生效() -> None:
    spec_no_review = ActionSpec(
        name="task.claim",
        title="认领处置任务",
        domain=ActionDomain.TASK,
        required_role=OperatorRole.DUTY,
        params=_Params,
        needs_confirm=True,
        writes=True,
        previewable=True,
    )
    policy = ReviewPolicy(enabled=True)
    assert policy.votes_required(_Publish.spec) == 2
    assert policy.votes_required(spec_no_review) == 0


def test_复核角色取动作门槛与值班长的更严者() -> None:
    policy = ReviewPolicy(enabled=True)
    assert policy.minimum_role(_Publish.spec) is OperatorRole.LEAD
    admin_spec = ActionSpec(
        name="rule.update",
        title="按显式阈值调整规则",
        domain=ActionDomain.RULE,
        required_role=OperatorRole.ADMIN,
        params=_Params,
        needs_confirm=True,
        writes=True,
        previewable=True,
        dual_review=True,
    )
    assert policy.minimum_role(admin_spec) is OperatorRole.ADMIN


# ---------- DualReview：一人一票 + 两票才迁移 ----------


async def test_两票不同席位才把提案迁到执行态() -> None:
    store = PlatformStore()
    record, _ = await store.create_action(
        action_id="act_dual000001",
        operator_id=LEAD1.operator_id,
        action="warning.publish",
        space_id=None,
        utterance="",
        resolved_args={"target": "wrn_x"},
        impact_scope={},
        expires_in_seconds=60,
        idempotency_key=None,
        review_required=2,
    )
    assert record["status"] == "proposed"
    dual = DualReview(store, ReviewPolicy(enabled=True))

    first = await dual.vote("act_dual000001", LEAD1, _Publish.spec)
    assert not first.ready and first.record["status"] == "proposed" and len(first.record["reviewers"]) == 1
    # 同一席位不能投第二票：明确报"重复"，而不是被当成"没抢到"
    with pytest.raises(ActionError) as dup:
        await dual.vote("act_dual000001", LEAD1, _Publish.spec)
    assert dup.value.code == "E_ACTION_DUPLICATE_REVIEW"
    # 值班员投票被拒并留痕（FR-4.4）
    with pytest.raises(ActionError) as low:
        await dual.vote("act_dual000001", DUTY, _Publish.spec)
    assert low.value.code == "E_ACTION_REVIEW_FORBIDDEN"

    second = await dual.vote("act_dual000001", LEAD2, _Publish.spec)
    assert second.ready and second.record["status"] == "executing"
    # 已执行后不再收票：这一票来自已投过票的席位，报"重复"而不是"没抢到"
    with pytest.raises(ActionError) as done:
        await dual.vote("act_dual000001", LEAD2, _Publish.spec)
    assert done.value.code == "E_ACTION_DUPLICATE_REVIEW"


async def test_复核票不够时确认闸不能被绕过() -> None:
    store = PlatformStore()
    await store.create_action(
        action_id="act_dual000002",
        operator_id=LEAD1.operator_id,
        action="warning.publish",
        space_id=None,
        utterance="",
        resolved_args={},
        impact_scope={},
        expires_in_seconds=60,
        idempotency_key=None,
        review_required=2,
    )
    await DualReview(store, ReviewPolicy(enabled=True)).vote("act_dual000002", LEAD1, _Publish.spec)
    assert await store.confirm_action("act_dual000002") is None


# ---------- 全链路：create_action → 两票 confirm → 执行恰好一次 ----------


async def test_配置关闭时本人一次确认即执行() -> None:
    action = _Publish()
    service = build_service(action)
    created = await service.create_action("warning.publish", {"target": "wrn_x"}, operator=LEAD1)
    assert created["status"] == "proposed" and created["review_required"] == 0
    result = await service.confirm(action_id=str(created["action_id"]), operator=LEAD1)
    assert result["status"] == "executed" and action.calls == 1


async def test_打开后两票各一人才执行且只执行一次() -> None:
    action = _Publish()
    service = build_service(action, assistant_dual_review_enabled=True)
    created = await service.create_action("warning.publish", {"target": "wrn_x"}, operator=LEAD1)
    action_id = str(created["action_id"])
    assert created["review_required"] == 2

    first = await service.confirm(action_id=action_id, operator=LEAD1)
    assert first["status"] == "awaiting_review" and len(first["reviews"]) == 1
    assert action.calls == 0

    with pytest.raises(ActionError) as dup:
        await service.confirm(action_id=action_id, operator=LEAD1)
    assert dup.value.code == "E_ACTION_DUPLICATE_REVIEW"

    second = await service.confirm(action_id=action_id, operator=LEAD2)
    assert second["status"] == "executed" and action.calls == 1


async def test_匿名或角色不足的复核被拒并留痕() -> None:
    action = _Publish()
    service = build_service(action, assistant_dual_review_enabled=True)
    created = await service.create_action("warning.publish", {"target": "wrn_x"}, operator=LEAD1)
    action_id = str(created["action_id"])
    before = len(service.rejections)
    with pytest.raises(ActionError):
        await service.confirm(action_id=action_id, operator=None)
    assert len(service.rejections) == before + 1


def test_注册表拒绝没有确认闸的复核标记() -> None:
    bad = ActionSpec(
        name="task.list",
        title="查询任务单元",
        domain=ActionDomain.TASK,
        required_role=OperatorRole.DUTY,
        params=_Params,
        dual_review=True,
    )

    class _Read(Action):
        spec = bad

        async def execute(self, targets: ResolvedTargets, params: BaseModel, ctx: Any) -> ActionResult:
            return ActionResult(summary="ok")

    registry = ActionRegistry([_Read()], domains={ActionDomain.TASK})
    with pytest.raises(AssertionError, match="双人复核"):
        registry.assert_consistent()
