"""P3 第一批：七个 L4 控制动作的完整闭环。

钉住四条容易回潮的口径：
1. 阈值/版本/设备地址由操作者用 `key=value` 显式给出，意图层不猜数字、不替人批准；
2. `decided_by` 取当前席位；实例/建议不存在是 404，状态冲突是 409，未装腿是 503；
3. 不可逆动作的预览必须写出"不可撤销"，且 `assert_consistent` 要求它可预览；
4. `rule.update` 走「写 pending 建议 → 立即确认」同一事务，不开直改路径。
"""

from __future__ import annotations

from typing import Any

import pytest

from aegis.domain.operator import Operator, OperatorRole
from aegis.services.assistant.actions.device_control import DeviceControl, DeviceControlParams
from aegis.services.assistant.actions.rule_control import (
    RuleConfirmParams,
    RuleConfirmSuggestion,
    RuleRejectParams,
    RuleRejectSuggestion,
    RuleUpdate,
    RuleUpdateParams,
    _translate,
)
from aegis.services.assistant.actions.workflow_control import (
    WorkflowAbort,
    WorkflowAbortParams,
    WorkflowDecide,
    WorkflowDecideParams,
    WorkflowStart,
    WorkflowStartParams,
)
from aegis.services.assistant.base import Action, ActionContext, ActionDomain, ActionError
from aegis.services.assistant.executor import ERROR_STATUS
from aegis.services.assistant.intent import IntentClassifier, bulk_scope_without_target
from aegis.services.assistant.narrate import narrate_result
from aegis.services.assistant.registry import ActionRegistry
from aegis.services.rules.threshold_suggestions import LedgerUnavailableError, ThresholdSuggestionService
from aegis.workflow.engine import WorkflowValidationError

WFI = "wfi_abcdef123456"
SUG = "sug_abcdef123456"
URL = "https://device.internal/control"


def ctx(role: OperatorRole = OperatorRole.ADMIN) -> ActionContext:
    return ActionContext(
        session_id="s-l4",
        utterance="",
        operator=Operator(operator_id="admin-01", display_name="管理员", role=role),
    )


async def run(action: Action, params: Any, context: ActionContext | None = None) -> Any:
    active = context or ctx()
    targets = await action.resolve(params, active)
    return await action.execute(targets, params, active)


class FakeWorkflow:
    def __init__(self) -> None:
        self.instances: dict[str, dict[str, Any]] = {WFI: {"instance_id": WFI, "status": "waiting"}}
        self.started: list[tuple[str, str]] = []
        self.resumed: list[tuple[str, str, dict[str, Any]]] = []
        self.aborted: list[tuple[str, str]] = []
        self.unknown_template = False
        self.resume_conflict = False

    async def start_from_name(self, name: str, *, trace_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        self.started.append((name, trace_id))
        if self.unknown_template:
            raise WorkflowValidationError(f"未找到工作流模板: {name}")
        detail = {"instance_id": WFI, "status": "running", "trace_id": trace_id, "workflow_id": "wf_abcdef123456"}
        self.instances[WFI] = detail
        return detail

    async def resume(self, instance_id: str, *, node_id: str, decision: dict[str, Any]) -> dict[str, Any]:
        self.resumed.append((instance_id, node_id, decision))
        if self.resume_conflict:
            raise WorkflowValidationError(f"节点 {node_id} 未在等待人工决策（当前 succeeded）")
        return {"instance_id": instance_id, "status": "running"}

    async def abort(self, instance_id: str, *, reason: str = "") -> dict[str, Any]:
        self.aborted.append((instance_id, reason))
        return {"instance_id": instance_id, "status": "aborted", "error": reason}

    def instance_detail(self, instance_id: str) -> dict[str, Any] | None:
        return self.instances.get(instance_id)


class FakeRules:
    def __init__(self, *, available: bool = True, error: Exception | None = None) -> None:
        self._available = available
        self.error = error
        self.confirm_calls: list[tuple[str, dict[str, Any]]] = []
        self.reject_calls: list[tuple[str, dict[str, Any]]] = []
        self.apply_calls: list[dict[str, Any]] = []

    @property
    def available(self) -> bool:
        return self._available

    async def confirm(self, suggestion_id: str, **kwargs: Any) -> dict[str, Any]:
        self.confirm_calls.append((suggestion_id, kwargs))
        if self.error is not None:
            raise self.error
        return {
            "suggestion": {"suggestion_id": suggestion_id, "status": "confirmed", "new_version": 3},
            "rule_id": kwargs["rule_id"],
            "new_version": 3,
            "engine_loaded": True,
        }

    async def reject(self, suggestion_id: str, **kwargs: Any) -> dict[str, Any]:
        self.reject_calls.append((suggestion_id, kwargs))
        if self.error is not None:
            raise self.error
        return {"suggestion_id": suggestion_id, "status": "rejected", "decided_by": kwargs["decided_by"]}

    async def apply(self, **kwargs: Any) -> dict[str, Any]:
        self.apply_calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return {
            "suggestion": {"suggestion_id": SUG, "status": "confirmed", "new_version": 3},
            "rule_id": kwargs["rule_id"],
            "new_version": 3,
            "engine_loaded": True,
        }


class FakeDevice:
    def __init__(self, *, enabled: bool = True, refuse: str | None = None, fail: bool = False) -> None:
        self._enabled = enabled
        self.refuse = refuse
        self.fail = fail
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []

    @property
    def enabled(self) -> bool:
        return self._enabled

    def refusal_reason(self, method: str, url: str) -> str | None:
        return self.refuse

    async def __call__(self, method: str, url: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls.append((method, url, body))
        if self.fail:
            raise RuntimeError("timeout")
        return {"ok": True}


def build_registry(workflow: FakeWorkflow, rules: FakeRules, device: FakeDevice) -> ActionRegistry:
    actions: list[Action] = [
        WorkflowStart(workflow),
        WorkflowDecide(workflow),
        WorkflowAbort(workflow),
        RuleConfirmSuggestion(rules),
        RuleRejectSuggestion(rules),
        RuleUpdate(rules),
        DeviceControl(device),
    ]
    return ActionRegistry(actions, domains={ActionDomain.WORKFLOW, ActionDomain.RULE, ActionDomain.DEVICE})


class SuggestionConflictError(Exception):
    """与持久层异常同名的测试桩：动作层按类名映射，不 import 可选腿包。"""


class SuggestionArgumentError(ValueError):
    """同上。"""


# ---------- 注册表 ----------


def test_七动作注册表自洽且角色判定来自spec() -> None:
    registry = build_registry(FakeWorkflow(), FakeRules(), FakeDevice())
    registry.assert_consistent()
    assert registry.names == tuple(
        sorted(
            {
                "workflow.start",
                "workflow.decide",
                "workflow.abort",
                "rule.confirm_suggestion",
                "rule.reject_suggestion",
                "rule.update",
                "device.control",
            }
        )
    )
    caps = {row["action"]: row for row in registry.capabilities(OperatorRole.LEAD)}
    assert caps["rule.update"]["allowed"] is False, "rule.update 只允许 admin"
    assert caps["workflow.start"]["needs_confirm"] is True
    assert caps["device.control"]["allowed"] is True


def test_三个新错误码都有HTTP映射() -> None:
    assert ERROR_STATUS["E_RULE_LEDGER_UNAVAILABLE"] == 503
    assert ERROR_STATUS["E_DEVICE_UNAVAILABLE"] == 503
    assert ERROR_STATUS["E_ACTION_UNAVAILABLE"] == 503


# ---------- workflow.* ----------


@pytest.mark.asyncio
async def test_启动工作流按模板名并生成链路号() -> None:
    workflow = FakeWorkflow()
    result = await run(WorkflowStart(workflow), WorkflowStartParams(name="report_review"))
    assert workflow.started[0][0] == "report_review"
    assert workflow.started[0][1].startswith("trc_")
    assert result.data["instance"]["status"] == "running"


@pytest.mark.asyncio
async def test_模板不存在是404而不是执行失败() -> None:
    workflow = FakeWorkflow()
    workflow.unknown_template = True
    with pytest.raises(ActionError) as caught:
        await run(WorkflowStart(workflow), WorkflowStartParams(name="missing"))
    assert caught.value.code == "E_TARGET_NOT_FOUND"


@pytest.mark.asyncio
async def test_人工决策把席位写进decision且冲突转409() -> None:
    workflow = FakeWorkflow()
    params = WorkflowDecideParams(instance_id=WFI, node_id="human_review_1", choice="approve", comment="同意")
    result = await run(WorkflowDecide(workflow), params)
    assert workflow.resumed[0][2] == {"choice": "approve", "by": "admin-01", "comment": "同意"}
    assert result.data["choice"] == "approve"

    workflow.resume_conflict = True
    with pytest.raises(ActionError) as caught:
        await run(WorkflowDecide(workflow), params)
    assert caught.value.code == "E_ACTION_CONFLICT"


@pytest.mark.asyncio
async def test_实例不存在在resolve阶段就404() -> None:
    workflow = FakeWorkflow()
    action = WorkflowDecide(workflow)
    with pytest.raises(ActionError) as caught:
        await action.resolve(WorkflowDecideParams(instance_id="wfi_000000000000", node_id="n1", choice="approve"), ctx())
    assert caught.value.code == "E_TARGET_NOT_FOUND"
    assert workflow.resumed == []


@pytest.mark.asyncio
async def test_中止工作流带原因且预览声明不可撤销() -> None:
    workflow = FakeWorkflow()
    action = WorkflowAbort(workflow)
    params = WorkflowAbortParams(instance_id=WFI, reason="误报")
    preview = await action.preview(await action.resolve(params, ctx()), params, ctx())
    assert preview.irreversible and "不可撤销" in preview.irreversible[0]
    result = await run(action, params)
    assert workflow.aborted == [(WFI, "误报")]
    assert result.data["instance"]["status"] == "aborted"


# ---------- rule.* ----------


@pytest.mark.asyncio
async def test_确认建议的数字与席位都按参数传递() -> None:
    rules = FakeRules()
    params = RuleConfirmParams(
        suggestion_id=SUG, rule_id="rain-01", metric="rainfall_1h", threshold=75.0, expected_version=2, note="复核通过"
    )
    result = await run(RuleConfirmSuggestion(rules), params)
    assert rules.confirm_calls[0][1]["decided_by"] == "admin-01"
    assert rules.confirm_calls[0][1]["expected_version"] == 2
    assert result.data["new_version"] == 3


@pytest.mark.asyncio
async def test_驳回建议且台账缺席报503() -> None:
    rules = FakeRules()
    result = await run(RuleRejectSuggestion(rules), RuleRejectParams(suggestion_id=SUG, note="样本不足"))
    assert result.data["suggestion"]["status"] == "rejected"
    assert rules.reject_calls[0][1]["decided_by"] == "admin-01"

    with pytest.raises(ActionError) as caught:
        await run(RuleRejectSuggestion(FakeRules(available=False)), RuleRejectParams(suggestion_id=SUG))
    assert caught.value.code == "E_RULE_LEDGER_UNAVAILABLE"


@pytest.mark.asyncio
async def test_直改阈值走apply且冲突与台账异常分层() -> None:
    rules = FakeRules()
    params = RuleUpdateParams(rule_id="rain-01", metric="rainfall_1h", threshold=80.0, expected_version=2)
    result = await run(RuleUpdate(rules), params)
    assert rules.apply_calls[0]["threshold"] == 80.0
    assert rules.apply_calls[0]["decided_by"] == "admin-01"
    assert result.data["engine_loaded"] is True

    with pytest.raises(ActionError) as caught:
        await run(RuleUpdate(FakeRules(error=SuggestionConflictError("已被裁决"))), params)
    assert caught.value.code == "E_ACTION_CONFLICT"


def test_异常类名映射只此一处且不吞未知异常() -> None:
    assert _translate(SuggestionConflictError("x")).code == "E_ACTION_CONFLICT"
    assert _translate(SuggestionArgumentError("x")).code == "E_ACTION_PARAM_INVALID"
    assert _translate(LedgerUnavailableError("x")).code == "E_RULE_LEDGER_UNAVAILABLE"
    assert _translate(RuntimeError("x")).code == "E_ACTION_EXECUTION_FAILED"


# ---------- device.control ----------


@pytest.mark.asyncio
async def test_设备指令走白名单外呼腿() -> None:
    device = FakeDevice()
    params = DeviceControlParams(device="valve-01", action="open", command_url=URL)
    result = await run(DeviceControl(device), params)
    assert device.calls == [("POST", URL, {"device": "valve-01", "action": "open"})]
    assert result.data["response"] == {"ok": True}


@pytest.mark.asyncio
async def test_设备外呼三种拒绝语义不混在一起() -> None:
    params = DeviceControlParams(device="valve-01", action="open", command_url=URL)
    with pytest.raises(ActionError) as off:
        await run(DeviceControl(FakeDevice(enabled=False)), params)
    assert off.value.code == "E_DEVICE_UNAVAILABLE"

    with pytest.raises(ActionError) as refused:
        await run(DeviceControl(FakeDevice(refuse="主机不在白名单：evil.example")), params)
    assert refused.value.code == "E_ACTION_NOT_ALLOWED"

    with pytest.raises(ActionError) as failed:
        await run(DeviceControl(FakeDevice(fail=True)), params)
    assert failed.value.code == "E_ACTION_EXECUTION_FAILED"


# ---------- 意图层 ----------


@pytest.mark.parametrize(
    ("text", "action", "expected"),
    [
        ("启动工作流 name=report_review", "workflow.start", {"name": "report_review"}),
        (
            f"工作流决策 instance_id={WFI} node_id=human_review_1 choice=approve",
            "workflow.decide",
            {"instance_id": WFI, "node_id": "human_review_1", "choice": "approve"},
        ),
        (f"中止工作流 instance_id={WFI} reason=误报", "workflow.abort", {"instance_id": WFI, "reason": "误报"}),
        (
            f"确认阈值建议 suggestion_id={SUG} rule_id=rain-01 metric=rainfall_1h threshold=75 version=2",
            "rule.confirm_suggestion",
            {"suggestion_id": SUG, "rule_id": "rain-01", "threshold": 75.0, "expected_version": 2},
        ),
        (f"驳回阈值建议 suggestion_id={SUG}", "rule.reject_suggestion", {"suggestion_id": SUG}),
        (
            "改阈值 rule_id=rain-01 metric=rainfall_1h threshold=80 version=2",
            "rule.update",
            {"rule_id": "rain-01", "metric": "rainfall_1h", "threshold": 80.0, "expected_version": 2},
        ),
        (
            f"设备控制 device=valve-01 action=open command_url={URL}",
            "device.control",
            {"device": "valve-01", "action": "open", "command_url": URL},
        ),
    ],
)
def test_七个L4动作都能被规则识别并抽全参数(text: str, action: str, expected: dict[str, Any]) -> None:
    classifier = IntentClassifier(build_registry(FakeWorkflow(), FakeRules(), FakeDevice()))
    intent = classifier.classify(text)
    assert intent.action == action, intent.note
    for key, value in expected.items():
        assert intent.args[key] == value


def test_缺参数不产生动作而是指路() -> None:
    classifier = IntentClassifier(build_registry(FakeWorkflow(), FakeRules(), FakeDevice()))
    for text in ("改阈值", "启动工作流", "设备控制", "工作流决策"):
        intent = classifier.classify(text)
        assert intent.action is None
        assert "请写「" in intent.note, intent.note
    bad = classifier.classify("改阈值 rule_id=rain-01 metric=rainfall_1h threshold=abc version=2")
    assert bad.action is None, "阈值不是数字时必须指路，不能悄悄填 0"


def test_带实例或建议编号的指令不被整体批量闸误伤() -> None:
    assert bulk_scope_without_target(f"把 {WFI} 都中止") is False
    assert bulk_scope_without_target(f"把 {SUG} 都驳回") is False
    assert bulk_scope_without_target("把全网预警都删掉") is True


# ---------- 叙述层 ----------


def test_七个动作都有确定性叙述分支() -> None:
    assert "工作流实例" in narrate_result("workflow.start", {"instance": {"instance_id": WFI, "status": "running"}})
    assert "阈值建议" in narrate_result("rule.update", {"suggestion": {"suggestion_id": SUG, "status": "confirmed"}})
    assert "valve-01" in narrate_result("device.control", {"device": "valve-01", "action": "open", "response": {"ok": True}})


# ---------- ThresholdSuggestionService.apply ----------


class FakeLedger:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.confirmed: list[dict[str, Any]] = []

    async def put_threshold_suggestion(self, row: dict[str, Any]) -> dict[str, Any]:
        self.rows.append(row)
        return {"suggestion_id": row["suggestion_id"]}

    async def threshold_suggestions(self, *, status: str = "pending_human_review", limit: int = 100) -> list[dict[str, Any]]:
        return self.rows

    async def confirm_threshold_suggestion(self, **kwargs: Any) -> dict[str, Any]:
        self.confirmed.append(kwargs)
        return {"suggestion_id": kwargs["suggestion_id"], "status": "confirmed", "new_version": kwargs["expected_version"] + 1}

    async def reject_threshold_suggestion(self, **kwargs: Any) -> dict[str, Any]:
        return {"suggestion_id": kwargs["suggestion_id"], "status": "rejected"}


class FakeRuleEngine:
    def describe(self) -> dict[str, Any]:
        return {"source": "fake"}

    def reload(self, rules: Any, *, source: str) -> int:
        return len(rules)


@pytest.mark.asyncio
async def test_apply先入账再确认且数字来自显式参数() -> None:
    ledger = FakeLedger()
    service = ThresholdSuggestionService(store=ledger, rule_engine=FakeRuleEngine())
    outcome = await service.apply(
        rule_id="rain-01", metric="rainfall_1h", threshold=80.0, decided_by="admin-01", note="汛期上调", expected_version=2
    )
    assert len(ledger.rows) == 1
    assert ledger.rows[0]["status"] == "pending_human_review"
    assert "admin-01" in str(ledger.rows[0]["suggestion"])
    assert "80" in str(ledger.rows[0]["suggestion"])
    assert ledger.confirmed[0]["rule_id"] == "rain-01"
    assert ledger.confirmed[0]["threshold"] == 80.0
    assert ledger.confirmed[0]["expected_version"] == 2
    assert outcome["new_version"] == 3


@pytest.mark.asyncio
async def test_apply在台账缺席时明确失败() -> None:
    service = ThresholdSuggestionService(store=object(), rule_engine=FakeRuleEngine())
    with pytest.raises(LedgerUnavailableError):
        await service.apply(rule_id="rain-01", metric="rainfall_1h", threshold=80.0, decided_by="admin-01", expected_version=2)
