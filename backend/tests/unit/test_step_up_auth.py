"""高危动作的二次认证（P4，设计 §14.1.3）。

与双人复核正交：复核是"两个不同席位各投一票"（横向），二次认证是"同一席位再亮一次钥匙"（纵向）。
钉五条不变量：
- 开关默认关：高危动作也不额外要令牌（D5「指令即执行」逐字不变）；
- 开关打开后：高危动作确认必须带有效令牌，否则拒绝且**不执行**；
- 非高危动作不受影响（只对 `dual_review=True` 那 10 个生效）；
- 二次认证失败**不消耗复核票**（票还没执行，只是没通过认证）；
- 认证失败留审计（"想做但没通过"也要有账）。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

from aegis.config import Settings
from aegis.domain.operator import Operator, OperatorRole
from aegis.services.assistant import ActionDomain
from aegis.services.assistant.base import Action, ActionResult, ActionSpec, ResolvedTargets
from aegis.services.assistant.registry import ActionRegistry
from aegis.services.assistant.service import AssistantService
from aegis.services.operator_auth import OperatorAuthService
from aegis.storage.store import PlatformStore

LEAD = Operator(operator_id="lead-01", display_name="值班长", role=OperatorRole.LEAD)
LEAD2 = Operator(operator_id="lead-02", display_name="值班长乙", role=OperatorRole.LEAD)


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target: str


class _HighRisk(Action):
    """高危动作替身（`dual_review=True` 那一族）。"""

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


class _LowRisk(Action):
    """非高危写动作（`dual_review=False`）：二次认证不该碰它。"""

    spec = ActionSpec(
        name="task.claim",
        title="认领处置任务",
        domain=ActionDomain.TASK,
        required_role=OperatorRole.DUTY,
        params=_Params,
        needs_confirm=True,
        writes=True,
        previewable=True,
    )

    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, targets: ResolvedTargets, params: BaseModel, ctx: Any) -> ActionResult:
        self.calls += 1
        return ActionResult(summary="已认领", data={})


class FakeKeyLedger:
    def __init__(self) -> None:
        self.by_hash: dict[str, dict[str, Any]] = {}
        self.attempts: dict[str, dict[str, Any]] = {}

    async def create_operator_key(self, *, key_id: str, operator_id: str, key_hash: str) -> dict[str, Any]:
        row = {"key_id": key_id, "operator_id": operator_id, "active": True, "key_hash": key_hash}
        self.by_hash[key_hash] = row
        return {k: v for k, v in row.items() if k != "key_hash"}

    async def get_operator_key_by_hash(self, key_hash: str) -> dict[str, Any] | None:
        return self.by_hash.get(key_hash)

    async def list_operator_keys(self, operator_id: str) -> list[dict[str, Any]]:
        return []

    async def revoke_operator_key(self, key_id: str) -> bool:
        return False

    async def touch_operator_key(self, key_id: str) -> None:
        return None

    async def get_key_attempts(self, operator_id: str) -> dict[str, Any] | None:
        return self.attempts.get(operator_id)

    async def record_key_failure(self, *, operator_id: str, max_attempts: int, lock_minutes: int) -> dict[str, Any]:
        cur = self.attempts.setdefault(operator_id, {"failed_attempts": 0, "locked_until": None})
        cur["failed_attempts"] += 1
        return {"operator_id": operator_id, **cur}

    async def clear_key_failures(self, operator_id: str) -> None:
        self.attempts[operator_id] = {"failed_attempts": 0, "locked_until": None}


class FakeOperators:
    async def get(self, operator_id: str) -> Operator | None:
        return {LEAD.operator_id: LEAD, LEAD2.operator_id: LEAD2}.get(operator_id)


def _settings(**overrides: Any) -> Settings:
    return Settings(env="test", bus_backend="memory", store_backend="memory", simulator_enabled=False, llm_api_key="", **overrides)


async def _build(action: Action, *, step_up: bool) -> tuple[AssistantService, str]:
    store = PlatformStore()
    auth = OperatorAuthService(FakeKeyLedger(), FakeOperators(), enabled=True)
    issued = await auth.issue_key(LEAD.operator_id)
    service = AssistantService(
        registry=ActionRegistry([action], domains={ActionDomain.WARNING, ActionDomain.TASK}),
        settings=_settings(assistant_step_up_auth_enabled=step_up),
        control_actions=store,
        control_actions_persistent=False,
        operator_auth=auth,
    )
    return service, issued.plaintext


async def _propose(service: AssistantService, action: Action, *, review_required: int = 0) -> str:
    record, _ = await service._repo.create_action(  # type: ignore[union-attr]
        action_id=f"act_step{action.spec.name.replace('.', '')[:8]:0<8}"[:20],
        operator_id=LEAD.operator_id,
        action=action.spec.name,
        space_id=None,
        utterance="发布这条预警",
        resolved_args={"target": "wrn_x"},
        impact_scope={},
        expires_in_seconds=300,
        idempotency_key=None,
        review_required=review_required,
        audit=None,
    )
    return str(record["action_id"])


async def _build_reviewed(action: Action) -> tuple[AssistantService, str, str]:
    """同时打开双人复核与二次认证：钉住「认证先于记票」的顺序。"""
    store = PlatformStore()
    auth = OperatorAuthService(FakeKeyLedger(), FakeOperators(), enabled=True)
    first = await auth.issue_key(LEAD.operator_id)
    second = await auth.issue_key(LEAD2.operator_id)
    service = AssistantService(
        registry=ActionRegistry([action], domains={ActionDomain.WARNING}),
        settings=_settings(assistant_step_up_auth_enabled=True, assistant_dual_review_enabled=True),
        control_actions=store,
        control_actions_persistent=False,
        operator_auth=auth,
    )
    return service, first.plaintext, second.plaintext


class Test开关关时行为不变:
    async def test_高危动作不带令牌也能执行(self) -> None:
        action = _HighRisk()
        service, _ = await _build(action, step_up=False)
        action_id = await _propose(service, action)

        result = await service.confirm(action_id=action_id, operator=LEAD)
        assert result["status"] == "executed"
        assert action.calls == 1


class Test开关开时高危动作要二次认证:
    async def test_不带令牌被拒且不执行(self) -> None:
        action = _HighRisk()
        service, _ = await _build(action, step_up=True)
        action_id = await _propose(service, action)

        result = await service.confirm(action_id=action_id, operator=LEAD)
        assert result["status"] == "rejected"
        assert "二次认证" in result["reason"]
        assert action.calls == 0, "认证没过就不该执行"

    async def test_带有效令牌可执行(self) -> None:
        action = _HighRisk()
        service, plaintext = await _build(action, step_up=True)
        action_id = await _propose(service, action)

        result = await service.confirm(action_id=action_id, operator=LEAD, operator_key=plaintext)
        assert result["status"] == "executed"
        assert action.calls == 1

    async def test_令牌错被拒且不执行(self) -> None:
        action = _HighRisk()
        service, _ = await _build(action, step_up=True)
        action_id = await _propose(service, action)

        result = await service.confirm(action_id=action_id, operator=LEAD, operator_key="aegis_op_bogus")
        assert result["status"] == "rejected"
        assert action.calls == 0

    async def test_非高危动作不受二次认证影响(self) -> None:
        """只对 `dual_review=True` 那 10 个生效——认领这类写动作不该被加一道门。"""
        action = _LowRisk()
        service, _ = await _build(action, step_up=True)
        action_id = await _propose(service, action)

        result = await service.confirm(action_id=action_id, operator=LEAD)
        assert result["status"] == "executed"
        assert action.calls == 1


class Test认证先于复核票:
    async def test_认证失败不消耗复核票(self) -> None:
        """认证没过就落票的话，同一人第二次带对令牌会被判「已经投过」；
        第二票更糟：票一落状态就迁到 executing，认证失败后谁也收不了尾。"""
        action = _HighRisk()
        service, lead_key, other_key = await _build_reviewed(action)
        action_id = await _propose(service, action, review_required=2)

        first_try = await service.confirm(action_id=action_id, operator=LEAD, operator_key="aegis_op_bogus")
        assert first_try["status"] == "rejected"
        assert first_try["action_id"] == action_id, "认证失败的回执要带真实动作号"
        record = await service._repo.get_action(action_id)  # type: ignore[union-attr]
        assert record is not None and record["status"] == "proposed" and record["reviewers"] == []

        voted = await service.confirm(action_id=action_id, operator=LEAD, operator_key=lead_key)
        assert voted["status"] == "awaiting_review"
        assert len(voted["reviews"]) == 1

        second_bad = await service.confirm(action_id=action_id, operator=LEAD2, operator_key="aegis_op_bogus")
        assert second_bad["status"] == "rejected"
        record = await service._repo.get_action(action_id)  # type: ignore[union-attr]
        assert record is not None and record["status"] == "proposed" and len(record["reviewers"]) == 1

        done = await service.confirm(action_id=action_id, operator=LEAD2, operator_key=other_key)
        assert done["status"] == "executed"
        assert action.calls == 1


class Test认证失败留账:
    async def test_认证失败写一行审计(self) -> None:
        """「想做但没通过」也要有账（与 §4.9 的三段结局一致），并挂在被确认的动作号上。"""
        action = _HighRisk()
        service, _ = await _build(action, step_up=True)
        action_id = await _propose(service, action)

        result = await service.confirm(action_id=action_id, operator=LEAD)
        rows = [row for row in await service._repo.read_audit(limit=200) if row.get("action_id") == action_id]  # type: ignore[union-attr]

        assert result["status"] == "rejected"
        assert rows, "认证失败的审计行没有挂在被确认的动作上"
        assert rows[-1]["after"]["rejected"] == "E_OPERATOR_KEY_REQUIRED", rows[-1]
        assert rows[-1]["utterance"] == "发布这条预警"
