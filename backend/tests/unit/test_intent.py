"""意图判定器单测：规则多命中的取舍、问句写动作、置信度、多轮指代消解。"""

from __future__ import annotations

from aegis.services.assistant.intent import IntentClassifier, resolve_coreference

_READ_ACTIONS = [
    "warning.list",
    "warning.explain",
    "chain.list",
    "chain.explain",
    "task.list",
    "task.get",
    "station.list",
    "agent.list",
    "metric.report",
    "plan.ask",
    "action.status",
]
_WRITE_ACTIONS = [
    "drill.start",
    "report.submit",
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
    "notify.lead",
    "notify.group",
    "workflow.start",
    "workflow.decide",
    "workflow.abort",
    "rule.confirm_suggestion",
    "rule.reject_suggestion",
    "rule.update",
    "device.control",
]
_ALL = sorted(_READ_ACTIONS + _WRITE_ACTIONS)


class _Spec:
    def __init__(self, writes: bool) -> None:
        self.writes = writes


class _Action:
    def __init__(self, writes: bool) -> None:
        self.spec = _Spec(writes)


class FakeRegistry:
    def get(self, name: str) -> _Action | None:
        if name not in _ALL:
            return None
        return _Action(name in _WRITE_ACTIONS)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(_ALL)


def _clf() -> IntentClassifier:
    return IntentClassifier(FakeRegistry())


class Test规则多命中取舍:
    def test_句末上报动词优先于演练(self) -> None:
        # 旧实现按固定顺序+子串，"演练"排在前会把"请把演练情况上报"误判成 drill.start。
        assert _clf().classify("请把演练情况上报").action == "report.submit"

    def test_正常演练不被影响(self) -> None:
        assert _clf().classify("演练一次").action == "drill.start"

    def test_问句里的写动作转预案咨询(self) -> None:
        intent = _clf().classify("上报流程是什么")
        assert intent.action == "plan.ask", "问流程不是要执行，不该制造写提案"

    def test_问句里的读动作保持(self) -> None:
        assert _clf().classify("sla达标了吗").action == "metric.report"
        assert _clf().classify("请问预警有哪些").action == "warning.list"


class Test规则置信度:
    def test_规则命中不再恒为满分(self) -> None:
        confidence = _clf().classify("演练一次").confidence
        assert 0.0 < confidence < 1.0


class Test指代消解:
    def test_指代补上一轮编号(self) -> None:
        history = [{"role": "user", "text": "解释 wrn_0123456789abcdef0123"}]
        assert resolve_coreference("撤回它", history).endswith("wrn_0123456789abcdef0123")

    def test_无指代原样返回(self) -> None:
        assert resolve_coreference("发布预警", []) == "发布预警"

    def test_历史无编号原样返回(self) -> None:
        assert resolve_coreference("撤回它", [{"role": "user", "text": "你好"}]) == "撤回它"

    def test_多轮指代经分类识别为对应写动作(self) -> None:
        history = [{"role": "user", "text": "解释 wrn_0123456789abcdef0123"}]
        intent = _clf().classify(resolve_coreference("把它撤回", history))
        assert intent.action == "warning.revoke"
        assert intent.args["warning_id"] == "wrn_0123456789abcdef0123"
