"""语义交互服务的单测（P1 拆包后的验收口径）。

三条最要紧的：
1. 注册表之外无动作——越权指令要"被拒 + 留痕"，不能悄悄换成一个读动作糊过去；
2. 执行类动作必须人工确认，且确认一次性有效（重复确认、过期确认都不许再执行）；
3. 认知镜像不编数——LLM 润色时若冒出事实里没有的数字，整段弃用。
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any, ClassVar

import pytest
from pydantic import BaseModel, ConfigDict

from aegis.config import Settings
from aegis.domain.operator import Operator, OperatorRole
from aegis.services.assistant import ActionDomain, ActionRegistry, AssistantService
from aegis.services.assistant.actions.control import DrillStart, ReportSubmit
from aegis.services.assistant.actions.query import (
    AgentList,
    ChainExplain,
    ChainList,
    MetricReport,
    PlanAsk,
    StationList,
    TaskGet,
    TaskList,
    WarningExplain,
    WarningList,
)
from aegis.services.assistant.base import Action, ActionContext, ActionResult, ActionSpec
from aegis.services.assistant.intent import IntentClassifier, out_of_scope_action
from aegis.storage.store import PlatformStore

WARNING_ROW: dict[str, Any] = {
    "warning_id": "wrn_0123456789abcdef0123",
    "event_id": "evt_0123456789ab",
    "trace_id": "trc_0123456789abcdef",
    "hazard_type": "debris_flow",
    "region_codes": ["540121"],
    "risk_level": 1,
    "title_zh": "西藏泥石流预警（红色）",
    "body_zh": "24小时累计降雨95毫米，沟道泥位抬升1.2米",
    "channels": ["sms", "broadcast"],
    "deliveries": [
        {"channel": "sms", "status": "delivered", "provider_msg_id": "gw-1", "receipt_at": "2026-10-03T06:00:00.000Z"},
        {"channel": "broadcast", "status": "failed", "provider_msg_id": None, "receipt_at": None},
    ],
    "released_at": "2026-10-03T05:59:00.000Z",
    "translation_pending": True,
}

CHAIN_ROW: dict[str, Any] = {
    "trace_id": "trc_0123456789abcdef",
    "event_id": "evt_0123456789ab",
    "stages": [{"name": "assess", "mode": "local", "ok": True, "note": "等级 1"}],
    "risk": {"rationale": "[R-DEBRIS-RAIN-2] debris_level=1.2", "assessed_by": "platform.risk_engine"},
    "degradations": ["研判降级: 无在线智能体"],
}


class _Row:
    """读视图里的行：动作只调 `model_dump` / `as_dict`，不必构造完整领域对象。"""

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = dict(data)

    def model_dump(self, mode: str = "json") -> dict[str, Any]:
        return dict(self._data)

    def as_dict(self) -> dict[str, Any]:
        return dict(self._data)


class Recorder:
    def __init__(self) -> None:
        self.calls: list[str] = []


class FakeWarnings:
    def __init__(self, rec: Recorder) -> None:
        self._rec = rec

    def list(
        self, *, limit: int = 50, region_code: str | None = None, released: bool | None = None, status: str | None = None
    ) -> list[_Row]:
        self._rec.calls.append(f"warning.list:{limit}:{region_code}:{released}")
        return [_Row(WARNING_ROW)]

    def get(self, warning_id: str) -> _Row | None:
        self._rec.calls.append(f"get_warning:{warning_id}")
        return _Row(WARNING_ROW) if warning_id == WARNING_ROW["warning_id"] else None


class FakeChains:
    def __init__(self, rec: Recorder) -> None:
        self._rec = rec

    def latest(self, count: int = 10) -> list[_Row]:
        self._rec.calls.append(f"chain.list:{count}")
        return [_Row(CHAIN_ROW)]

    def snapshot(self) -> list[_Row]:
        return [_Row(CHAIN_ROW)]


class FakeTasks:
    def __init__(self, rec: Recorder) -> None:
        self._rec = rec

    def list(self, *, limit: int = 50, region_code: str | None = None) -> list[_Row]:
        self._rec.calls.append(f"task.list:{limit}:{region_code}")
        return [_Row({"task_unit_id": "stu_0123456789abcdef", "objective": "生成并发布分级预警", "status": "ready"})]

    def get(self, task_unit_id: str) -> _Row | None:
        self._rec.calls.append(f"get_task:{task_unit_id}")
        return _Row({"task_unit_id": task_unit_id, "objective": "生成并发布分级预警", "status": "ready"})


class FakeStations:
    def __init__(self, rec: Recorder) -> None:
        self._rec = rec

    async def list_stations(self, *, region_code: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
        self._rec.calls.append(f"list_stations:{region_code}:{limit}")
        return [{"station_id": "ST-1", "region_code": "540121"}]


class FakeAgents:
    def __init__(self, rec: Recorder) -> None:
        self._rec = rec

    @property
    def online_count(self) -> int:
        self._rec.calls.append("list_agents")
        return 0

    def snapshot(self) -> list[dict[str, Any]]:
        return []


class FakeGateway:
    counters: ClassVar[dict[str, int]] = {"published": 0}


def build_registry(rec: Recorder) -> ActionRegistry:
    async def run_drill(*, scenario: str = "surge", ticks: int = 2, region_code: str | None = None) -> dict[str, Any]:
        rec.calls.append(f"run_drill:{scenario}:{ticks}:{region_code}")
        return {"chains": [], "regions": 1}

    async def submit_report(*, note: str, region_code: str, reporter: str = "web", hazard_hint: str | None = None) -> dict[str, Any]:
        rec.calls.append(f"submit_report:{region_code}:{reporter}")
        return {"chain": {"warning_id": "wrn_after_report"}, "human_review_required": False}

    def latency_report() -> dict[str, Any]:
        rec.calls.append("latency_report")
        return {
            "metrics": {"stage_assess_ms": {"count": 3, "p50": 1.2, "p95": 4.0, "breaches": 0}},
            "collaboration": {"success_rate": None},
        }

    warnings = FakeWarnings(rec)
    chains = FakeChains(rec)
    tasks = FakeTasks(rec)
    actions = [
        WarningList(warnings),
        WarningExplain(warnings, chains),
        ChainList(chains),
        ChainExplain(chains),
        TaskList(tasks),
        TaskGet(tasks),
        StationList(FakeStations(rec)),
        AgentList(FakeAgents(rec), FakeGateway()),
        MetricReport(latency_report),
        PlanAsk(None),
        DrillStart(run_drill, available=True),
        ReportSubmit(_FakeIntake(rec)),
    ]
    registry = ActionRegistry(
        actions,
        domains={ActionDomain.QUERY, ActionDomain.WARNING, ActionDomain.TASK, ActionDomain.DRILL},
    )
    registry.assert_consistent()
    return registry


class _FakeIntake:
    def __init__(self, rec: Recorder) -> None:
        self._rec = rec

    async def submit(self, *, note: str, region_code: str, reporter: str = "web", hazard_hint: str | None = None) -> dict[str, Any]:
        self._rec.calls.append(f"submit_report:{region_code}:{reporter}")
        return {"chain": {"warning_id": "wrn_after_report"}, "human_review_required": False}


class FakeLlm:
    def __init__(self, *, action: str | None = None, polish: str | None = None, error: Exception | None = None) -> None:
        self.available = True
        self.action = action
        self.polish = polish
        self.error = error
        self.json_calls = 0
        self.chat_calls = 0

    async def chat_json(self, messages: list[dict[str, str]], *, temperature: float = 0.1) -> dict[str, Any]:
        self.json_calls += 1
        if self.error is not None:
            raise self.error
        return {"action": self.action}

    async def chat(self, messages: list[dict[str, str]], *, temperature: float = 0.3, max_tokens: int | None = None) -> str:
        self.chat_calls += 1
        if self.error is not None:
            raise self.error
        return self.polish or "复述事实"


async def collect(service: AssistantService, text: str, **kwargs: Any) -> list[Any]:
    return [event async for event in service.respond(text, **kwargs)]


def settings(**overrides: Any) -> Settings:
    return Settings(env="test", bus_backend="memory", store_backend="memory", simulator_enabled=False, llm_api_key="", **overrides)


#: 写动作（演练/上报）必须有有效席位：匿名只能读。
DUTY = Operator(operator_id="duty-01", display_name="值班员", role=OperatorRole.DUTY)
LEAD = Operator(operator_id="lead-01", display_name="值班长", role=OperatorRole.LEAD)
LEAD2 = Operator(operator_id="lead-02", display_name="值班长乙", role=OperatorRole.LEAD)


def build_service(
    rec: Recorder | None = None,
    *,
    llm: Any | None = None,
    registry: ActionRegistry | None = None,
    store: PlatformStore | None = None,
    **overrides: Any,
) -> AssistantService:
    return AssistantService(
        registry=registry or build_registry(rec or Recorder()),
        llm=llm,
        settings=settings(**overrides),
        # 写动作要落动作台账；内存实现与 SQL 版同一套状态机，只是重启即失
        control_actions=store if store is not None else PlatformStore(),
        control_actions_persistent=False,
    )


class _DemoParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: str = "demo"


class _DemoWrite(Action):
    """测试专用写动作：要确认、有预览，用来复现"确认失败把动作卡在 executing"。

    真实动作里找不到一个"确认人不合格"还能稳定复现的（角色门槛都在 duty 以下），
    这条只存在于测试里，不注册进容器。
    """

    spec = ActionSpec(
        name="demo.write",
        title="演示写动作",
        domain=ActionDomain.DRILL,
        required_role=OperatorRole.LEAD,
        params=_DemoParams,
        needs_confirm=True,
        writes=True,
        previewable=True,
        example="演示一次写动作",
    )

    def __init__(self, rec: Recorder) -> None:
        self._rec = rec

    async def execute(self, targets: Any, params: _DemoParams, ctx: ActionContext) -> ActionResult:
        self._rec.calls.append("demo.write")
        return ActionResult(data={"note": params.note}, summary="已执行演示写动作")


class _DemoReviewedWrite(_DemoWrite):
    spec = ActionSpec(
        name="demo.reviewed",
        title="演示高危写动作",
        domain=ActionDomain.DRILL,
        required_role=OperatorRole.LEAD,
        params=_DemoParams,
        needs_confirm=True,
        writes=True,
        previewable=True,
        dual_review=True,
        example="演示一次高危写动作",
    )


def demo_registry(rec: Recorder) -> ActionRegistry:
    registry = ActionRegistry([_DemoWrite(rec), _DemoReviewedWrite(rec)], domains={ActionDomain.DRILL})
    registry.assert_consistent()
    return registry


# ---------- 越权闸 ----------


@pytest.mark.asyncio
async def test_查不到号时那句回答必须说没找到() -> None:
    """真机量的：问"解释 wrn_000… 为什么定这个等级"，答案帧给出的是
    `warning_id=wrn_00000000000000000000`——`found: False` 被 `_brief` 当假值滤掉了，
    整句读起来像查到了。读不到就说读不到，别让人对着一句像成功的话猜。"""
    service = build_service()
    events = await collect(service, "解释 wrn_00000000000000000000 为什么定这个等级", reporter="值班员")
    answer = next(event for event in events if event.type == "answer")
    assert "没找到" in answer.data["text"], answer.data["text"]
    assert "wrn_00000000000000000000" in answer.data["text"], answer.data["text"]


@pytest.mark.asyncio
async def test_查到号时答案仍是那段解释() -> None:
    """上面的对照：别把"没找到"当成万能前缀——查到了还得给出定级依据与触达回执。"""
    service = build_service()
    events = await collect(service, "解释 wrn_0123456789abcdef0123 为什么定这个等级", reporter="值班员")
    answer = next(event for event in events if event.type == "answer")
    assert "没找到" not in answer.data["text"], answer.data["text"]
    assert "sms=delivered" in answer.data["text"], answer.data["text"]


@pytest.mark.asyncio
async def test_越权指令被拒且留痕而不是降级成一个查询() -> None:
    rec = Recorder()
    service = build_service(rec)
    events = await collect(service, "把全网预警都删掉", reporter="值班员")
    assert [event.type for event in events][:2] == ["meta", "rejected"]
    assert rec.calls == [], "越权指令居然执行了动作"
    assert service.rejections and service.rejections[-1]["action"] == "warning.revoke"


@pytest.mark.asyncio
async def test_问句里的撤回发布字样不算越权() -> None:
    rec = Recorder()
    service = build_service(rec)
    events = await collect(service, "这条预警可以撤回吗 wrn_0123456789abcdef0123", reporter="值班员")
    assert not any(event.type == "rejected" for event in events)
    assert "get_warning:wrn_0123456789abcdef0123" in rec.calls


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("请立即发布全部区域", ("warning.publish", "立即发布")),
        ("帮我改阈值到 5", ("rule.update", "改阈值")),
        ("为什么发布红色预警", None),
        ("查询所有站点", None),
    ],
)
def test_越权判定只看祈使式与整体作用域(text: str, expected: tuple[str, str] | None) -> None:
    assert out_of_scope_action(text) == expected


def test_每个动作的示例句都被自己的词表判回该动作() -> None:
    """能力面把 `example` 交给页面当"点这里试一条"的预填文本。

    词表改了而示例没跟上，用户点开的就是一个答非所问的动作，而页面本身看不出任何异常——
    这条用例是那个错位唯一的探测器。示例句同时必须是可接受的（不被越权闸拦下），
    否则建议条一点就红。
    """
    registry = build_registry(Recorder())
    classifier = IntentClassifier(registry)
    for action in registry.actions:
        example = action.spec.example
        assert example, f"{action.spec.name} 没有示例句：页面上这条建议会预填出空输入框"
        decided = classifier.classify(example)
        assert decided.action == action.spec.name, f"{action.spec.name} 的示例句「{example}」被判成了 {decided.action}"
        assert out_of_scope_action(example) is None, f"{action.spec.name} 的示例句被判成越权"


def test_示例句非空且动作集合无重复() -> None:
    """12 个动作各自一条示例：重复的示例句会让两条建议指向同一个动作，页面看不出来。"""
    examples = [action.spec.example for action in build_registry(Recorder()).actions]
    assert len(examples) == len(set(examples)), "示例句重复：有两条建议会指向同一件事"
    assert all(example.strip() == example and example for example in examples)


@pytest.mark.asyncio
async def test_llm建议注册表外动作时拒绝并留痕() -> None:
    llm = FakeLlm(action="delete.all")
    service = build_service(llm=llm)
    await collect(service, "随便说点什么吧")
    assert llm.json_calls == 1
    assert any(event["action"] == "delete.all" for event in service.rejections), service.rejections


# ---------- 四类任务的通路 ----------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "action", "expected_calls"),
    [
        ("最近发布了哪些预警", "warning.list", {"warning.list"}),
        ("最近的链路事件", "chain.list", {"chain.list"}),
        ("查一下任务 stu_0123456789abcdef", "task.get", {"get_task"}),
        ("540121 有哪些站点", "station.list", {"list_stations"}),
        ("在线智能体有几个", "agent.list", {"list_agents"}),
        ("P95 时延是多少", "metric.report", {"latency_report"}),
        ("wrn_0123456789abcdef0123 为什么是红色", "warning.explain", {"get_warning"}),
        ("trc_0123456789abcdef 是谁判的", "chain.explain", set()),
    ],
)
async def test_只读任务直达且不产生待确认动作(text: str, action: str, expected_calls: set[str]) -> None:
    rec = Recorder()
    service = build_service(rec)
    events = await collect(service, text, reporter="值班员")
    intents = [event for event in events if event.type == "intent"]
    assert intents[0].data["action"] == action
    assert not any(event.type == "proposal" for event in events)
    # 意图对了还不算通路对：真正打出去的动作方法集合必须就是表里给定的那一个。
    assert {call.split(":", 1)[0] for call in rec.calls} == expected_calls, rec.calls


@pytest.mark.asyncio
async def test_预案问答给出内置剧本步骤并如实说明佐证为空() -> None:
    service = build_service()
    events = await collect(service, "泥石流该怎么办", reporter="值班员")
    result = next(event for event in events if event.type == "result")
    assert result.data["steps"] and result.data["steps"][0]["task_type"]
    assert "无命中" in result.data["text"]


# ---------- 人工确认 ----------


@pytest.mark.asyncio
async def test_演练与上报先给待确认动作执行后才落地() -> None:
    for text, action, call in (
        ("演练一次 surge 3 轮", "drill.start", "run_drill"),
        ("帮我上报：540121 沟道出现泥石流迹象", "report.submit", "submit_report"),
    ):
        rec = Recorder()
        service = build_service(rec)
        events = await collect(service, text, reporter="值班员", region_code="540121", operator=DUTY)
        proposals = [event for event in events if event.type == "proposal"]
        assert len(proposals) == 1, text
        assert proposals[0].data["action"] == action
        assert rec.calls == [], "未确认就执行了"

        first = await service.confirm(action_id=proposals[0].data["action_id"], operator=DUTY)
        assert first["status"] == "executed", first
        assert any(call in entry for entry in rec.calls)

        again = await service.confirm(action_id=proposals[0].data["action_id"], operator=DUTY)
        assert again["status"] == "rejected" and "已被处置" in again["reason"]


@pytest.mark.asyncio
async def test_未知动作号确认被拒() -> None:
    """动作号不再绑定会话：拿一个不存在的号去确认，必须是"不存在"而不是"跨会话"。"""
    rec = Recorder()
    service = build_service(rec)
    result = await service.confirm(action_id="act_deadbeef0000", operator=DUTY)
    assert result["status"] == "rejected"
    assert "不存在" in result["reason"]
    assert rec.calls == []


@pytest.mark.asyncio
async def test_过期动作确认后不执行() -> None:
    rec = Recorder()
    service = build_service(rec, assistant_confirm_ttl_seconds=1)
    events = await collect(service, "演练一次", reporter="值班员", operator=DUTY)
    proposal = next(event for event in events if event.type == "proposal")
    await asyncio.sleep(1.1)
    result = await service.confirm(action_id=proposal.data["action_id"], operator=DUTY)
    assert result["status"] in ("expired", "rejected")
    assert rec.calls == []


# ---------- 确认失败不卡死（P0-3） ----------


@pytest.mark.asyncio
async def test_确认人角色不够时提案留在proposed且换人可以再确认() -> None:
    """真机复现：lead 门槛的提案被 duty 点确认后，台账停在 executing——
    之后连 lead 也确认不了（"已被处置"），动作永远不执行、也永远不失败。"""
    rec = Recorder()
    store = PlatformStore()
    service = build_service(rec, registry=demo_registry(rec), store=store)
    created = await service.create_action("demo.write", {"note": "x"}, operator=LEAD)
    action_id = created["action_id"]

    denied = await service.confirm(action_id=action_id, operator=DUTY)
    assert denied["status"] == "rejected", denied
    assert "lead" in denied["reason"], denied

    current = await store.get_action(action_id)
    assert current is not None and current["status"] == "proposed", current

    retry = await service.confirm(action_id=action_id, operator=LEAD)
    assert retry["status"] == "executed", retry
    assert rec.calls == ["demo.write"]


@pytest.mark.asyncio
async def test_提案参数已不合法时如实拒绝且不卡成executing() -> None:
    """部署升级后 params 模型收紧了：旧提案的 resolved_args 不再能解析。
    确认必须回"参数不合法"并把提案留在 proposed（到期由维护收尾），
    而不是先迁到 executing 再抛 422——那会一直卡到 10 分钟卡死回收。"""
    rec = Recorder()
    store = PlatformStore()
    await store.create_action(
        action_id="act_0000000000000001",
        operator_id=LEAD.operator_id,
        action="demo.write",
        space_id=None,
        utterance="演示",
        resolved_args={"bogus": 1},
        impact_scope={},
        expires_in_seconds=300,
        idempotency_key=None,
    )
    service = build_service(rec, registry=demo_registry(rec), store=store)
    result = await service.confirm(action_id="act_0000000000000001", operator=LEAD)
    assert result["status"] == "rejected", result
    assert "参数不合法" in result["reason"], result
    assert rec.calls == []

    current = await store.get_action("act_0000000000000001")
    assert current is not None and current["status"] == "proposed", current


@pytest.mark.asyncio
async def test_第二票之后执行前失败要标记failed不能卡在executing() -> None:
    """双人复核的第二票已经把台账迁到 executing；此后的解析失败如果只抛错不改状态，
    这条动作就永远停在 executing（复核票也白花了）。执行前失败必须落 failed——
    对已经过了迁移点的动作而言，这是它唯一的终态。"""
    store = PlatformStore()
    await store.create_action(
        action_id="act_0000000000000002",
        operator_id=LEAD.operator_id,
        action="demo.reviewed",
        space_id=None,
        utterance="演示",
        resolved_args={"bogus": 1},
        impact_scope={},
        expires_in_seconds=300,
        idempotency_key=None,
        review_required=2,
    )
    await store.review_action(
        "act_0000000000000002", {"operator_id": "lead-01", "role": "lead", "at": "2026-11-09T00:00:00Z"}
    )
    rec = Recorder()
    service = build_service(rec, registry=demo_registry(rec), store=store, assistant_dual_review_enabled=True)

    result = await service.confirm(action_id="act_0000000000000002", operator=LEAD2)
    assert result["status"] == "rejected", result
    assert "参数不合法" in result["reason"], result
    assert rec.calls == []

    current = await store.get_action("act_0000000000000002")
    assert current is not None and current["status"] == "failed", current
    assert current["error"] is not None and current["error"]["code"] == "E_ACTION_PARAM_INVALID", current


@pytest.mark.asyncio
async def test_周期维护把过期提案与卡死执行中收尾() -> None:
    """起点回收只在启动跑一次；长跑进程里过期提案与卡死的 executing 谁也等不到重启。
    维护必须与启动收尾同一套判据：proposed 过期置 expired、executing 超 10 分钟置 failed。"""
    store = PlatformStore()
    await store.create_action(
        action_id="act_0000000000000003",
        operator_id=LEAD.operator_id,
        action="demo.write",
        space_id=None,
        utterance="演示",
        resolved_args={"note": "x"},
        impact_scope={},
        expires_in_seconds=-1,
        idempotency_key=None,
    )
    await store.create_action(
        action_id="act_0000000000000004",
        operator_id=LEAD.operator_id,
        action="demo.write",
        space_id=None,
        utterance="演示",
        resolved_args={"note": "x"},
        impact_scope={},
        expires_in_seconds=300,
        idempotency_key=None,
    )
    await store.confirm_action("act_0000000000000004")
    # 卡死判据是"超过 10 分钟没更新"：不能真等，把这一行的时钟拨回去
    store.control_actions._rows["act_0000000000000004"]["updated_at"] = (
        store.control_actions._now() - timedelta(seconds=660)
    )

    rec = Recorder()
    service = build_service(rec, registry=demo_registry(rec), store=store)
    report = await service.maintenance()

    assert report == {"expired": 1, "failed": 1}, report
    expired = await store.get_action("act_0000000000000003")
    assert expired is not None and expired["status"] == "expired", expired
    failed = await store.get_action("act_0000000000000004")
    assert failed is not None and failed["status"] == "failed", failed


# ---------- 认知镜像与不编数 ----------


@pytest.mark.asyncio
async def test_认知镜像叙述包含通道回执与降级事实() -> None:
    service = build_service()
    events = await collect(service, "wrn_0123456789abcdef0123 为什么是红色", reporter="值班员")
    result = next(event for event in events if event.type == "result")
    facts = result.data["facts"]
    assert "sms=delivered" in facts and "broadcast=failed" in facts
    assert "R-DEBRIS-RAIN-2" in facts
    assert "研判降级" in facts


@pytest.mark.asyncio
async def test_llm润色冒出没有的数字时整段弃用() -> None:
    service = build_service(llm=FakeLlm(polish="影响 8888 人，等级 2"))
    events = await collect(service, "wrn_0123456789abcdef0123 依据是什么", reporter="值班员")
    answer = next(event for event in events if event.type == "answer")
    assert "8888" not in answer.data["text"]
    assert "sms=delivered" in answer.data["text"]
    assert any("新数字" in row["reason"] for row in service.rejections)


@pytest.mark.asyncio
async def test_llm润色失败时回到确定性叙述() -> None:
    service = build_service(llm=FakeLlm(error=RuntimeError("网关超时")))
    events = await collect(service, "wrn_0123456789abcdef0123 依据是什么", reporter="值班员")
    answer = next(event for event in events if event.type == "answer")
    assert "sms=delivered" in answer.data["text"]


# ---------- 装配事实 ----------


@pytest.mark.asyncio
async def test_未注册动作给出能力提示而不是执行() -> None:
    service = build_service()
    events = await collect(service, "今天天气怎么样", reporter="值班员")
    answer = next(event for event in events if event.type == "answer")
    assert "可以做的事" in answer.data["text"]


def test_能力面按角色过滤且列出全部注册动作() -> None:
    registry = build_registry(Recorder())
    service = AssistantService(registry=registry, settings=settings())
    anonymous = service.capabilities(None)
    assert anonymous["llm_configured"] is False
    assert {row["action"] for row in anonymous["actions"]} == set(registry.names)
    assert all(not row["allowed"] for row in anonymous["actions"]), "匿名竟能看到可执行动作"

    duty = service.capabilities(OperatorRole.DUTY)
    allowed = {row["action"] for row in duty["actions"] if row["allowed"]}
    assert "warning.list" in allowed
    assert all(row["denied_reason"] is None for row in duty["actions"] if row["allowed"])


@pytest.mark.asyncio
async def test_会话数量有上界且空消息不出答案() -> None:
    service = build_service()
    assert (await collect(service, "   "))[1].type == "error"
    for index in range(260):
        await collect(service, "站点台账里有哪些站", session_id=f"as_session{index:04d}")
    assert len(service._sessions) <= 200


@pytest.mark.asyncio
async def test_缺参数的示例句要说清这个ID在哪个页面读得到() -> None:
    """能力面把每条动作的 example 交给前端当"点这里试一条"的预填文本。
    带标识符的示例句若原样发出去，回答必须指出这个 ID 在哪个页面能读到，而不是只回一句 422。
    """
    service = build_service()
    cases = [
        ("解释 wrn_… 为什么定这个等级", ("wrn_", "预警发布")),
        ("链路追踪 trc_… 每段耗时", ("trc_", "链路执行记录")),
    ]
    for text, needles in cases:
        events = await collect(service, text, reporter="值班员")
        answer = next(event for event in events if event.type == "answer")
        sentence = str(answer.data["text"])
        for needle in needles:
            assert needle in sentence, f"{text} 那句回答里没指路：{sentence}"


@pytest.mark.asyncio
async def test_缺参数不等于出错路径闭嘴但也不假称查到了() -> None:
    """上面那条的对照：指路归指路，仍要明说这次没给 ID、所以什么都没查。"""
    service = build_service()
    events = await collect(service, "解释 wrn_… 为什么定这个等级", reporter="值班员")
    answer = next(event for event in events if event.type == "answer")
    assert "缺少" in str(answer.data["text"]), answer.data["text"]
