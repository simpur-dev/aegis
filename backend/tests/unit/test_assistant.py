"""语义交互服务的单测（批次 B1 的验收口径）。

三条最要紧的：
1. 白名单之外无动作——越权指令要"被拒 + 留痕"，不能悄悄换成一个读动作糊过去；
2. 执行类动作必须人工确认，且确认一次性有效（重复确认、过期确认都不许再执行）；
3. 认知镜像不编数——LLM 润色时若冒出事实里没有的数字，整段弃用。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from aegis.config import Settings
from aegis.services.assistant import ACTION_SPECS, ACTION_WHITELIST, AssistantActions, AssistantService, out_of_scope

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


class Recorder:
    def __init__(self) -> None:
        self.calls: list[str] = []


def build_actions(rec: Recorder) -> AssistantActions:
    async def list_warnings(*, limit: int = 10, region_code: str | None = None) -> list[dict[str, Any]]:
        rec.calls.append(f"list_warnings:{limit}:{region_code}")
        return [WARNING_ROW]

    async def get_warning(warning_id: str) -> dict[str, Any] | None:
        rec.calls.append(f"get_warning:{warning_id}")
        return WARNING_ROW if warning_id == WARNING_ROW["warning_id"] else None

    async def list_chains(*, limit: int = 8) -> list[dict[str, Any]]:
        rec.calls.append(f"list_chains:{limit}")
        return [{"trace_id": "trc_0123456789abcdef", "event_id": "evt_0123456789ab", "stages": [], "ok": True}]

    async def get_chain(trace_id: str) -> dict[str, Any] | None:
        rec.calls.append(f"get_chain:{trace_id}")
        return {
            "trace_id": trace_id,
            "stages": [{"name": "assess", "mode": "local", "ok": True, "note": "等级 1"}],
            "risk": {"rationale": "[R-DEBRIS-RAIN-2] debris_level=1.2", "assessed_by": "platform.risk_engine"},
            "degradations": ["研判降级: 无在线智能体"],
        }

    async def get_task(task_unit_id: str) -> dict[str, Any] | None:
        rec.calls.append(f"get_task:{task_unit_id}")
        return {"task_unit_id": task_unit_id, "objective": "生成并发布分级预警", "status": "ready"}

    async def list_stations(*, region_code: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        rec.calls.append(f"list_stations:{region_code}:{limit}")
        return [{"station_id": "ST-1", "region_code": "540121", "lat": None, "lon": None}]

    async def list_agents() -> dict[str, Any]:
        rec.calls.append("list_agents")
        return {"online": 0, "items": []}

    def latency_report() -> dict[str, Any]:
        rec.calls.append("latency_report")
        return {
            "metrics": {"stage_assess_ms": {"count": 3, "p50": 1.2, "p95": 4.0, "breaches": 0}},
            "collaboration": {"success_rate": None},
        }

    async def run_drill(*, scenario: str = "surge", ticks: int = 2, region_code: str | None = None) -> dict[str, Any]:
        rec.calls.append(f"run_drill:{scenario}:{ticks}:{region_code}")
        return {"chains": [], "regions": 1}

    async def submit_report(*, note: str, region_code: str, reporter: str = "web", hazard_hint: str | None = None) -> dict[str, Any]:
        rec.calls.append(f"submit_report:{region_code}:{reporter}")
        return {"chain": {"warning_id": "wrn_after_report"}, "human_review_required": False}

    return AssistantActions(
        list_warnings=list_warnings,
        get_warning=get_warning,
        list_chains=list_chains,
        get_chain=get_chain,
        get_task=get_task,
        list_stations=list_stations,
        list_agents=list_agents,
        latency_report=latency_report,
        run_drill=run_drill,
        submit_report=submit_report,
    )


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


# ---------- 越权闸 ----------


@pytest.mark.asyncio
async def test_查不到号时那句回答必须说没找到() -> None:
    """真机量的：问"解释 wrn_000… 为什么定这个等级"，答案帧给出的是
    `warning_id=wrn_00000000000000000000`——`found: False` 被 `_brief` 当假值滤掉了，
    整句读起来像查到了。读不到就说读不到，别让人对着一句像成功的话猜。"""
    service = AssistantService(actions=build_actions(Recorder()), settings=settings())
    events = await collect(service, "解释 wrn_00000000000000000000 为什么定这个等级", reporter="值班员")
    answer = next(event for event in events if event.type == "answer")
    assert "没找到" in answer.data["text"], answer.data["text"]
    assert "wrn_00000000000000000000" in answer.data["text"], answer.data["text"]


@pytest.mark.asyncio
async def test_查到号时答案仍是那段解释() -> None:
    """上面的对照：别把"没找到"当成万能前缀——查到了还得给出定级依据与触达回执。"""
    service = AssistantService(actions=build_actions(Recorder()), settings=settings())
    events = await collect(service, "解释 wrn_0123456789abcdef0123 为什么定这个等级", reporter="值班员")
    answer = next(event for event in events if event.type == "answer")
    assert "没找到" not in answer.data["text"], answer.data["text"]
    assert "sms=delivered" in answer.data["text"], answer.data["text"]


@pytest.mark.asyncio
async def test_越权指令被拒且留痕而不是降级成一个查询() -> None:
    rec = Recorder()
    service = AssistantService(actions=build_actions(rec), settings=settings())
    events = await collect(service, "把全网预警都删掉", reporter="值班员")
    assert [event.type for event in events][:2] == ["meta", "rejected"]
    assert rec.calls == [], "越权指令居然执行了动作"
    assert service.rejections and service.rejections[-1]["action"] == "删掉"


@pytest.mark.asyncio
async def test_问句里的撤回发布字样不算越权() -> None:
    rec = Recorder()
    service = AssistantService(actions=build_actions(rec), settings=settings())
    events = await collect(service, "这条预警可以撤回吗 wrn_0123456789abcdef0123", reporter="值班员")
    assert not any(event.type == "rejected" for event in events)
    assert "get_warning:wrn_0123456789abcdef0123" in rec.calls


@pytest.mark.parametrize(
    ("text", "expected"),
    [("请立即发布全部区域", "立即发布"), ("帮我改阈值到 5", "改阈值"), ("为什么发布红色预警", None), ("查询所有站点", None)],
)
def test_越权判定只看祈使式与整体作用域(text: str, expected: str | None) -> None:
    assert out_of_scope(text) == expected


def test_每个动作的示例句都被自己的词表判回该动作() -> None:
    """能力面把 `example` 交给页面当"点这里试一条"的预填文本。

    词表改了而示例没跟上，用户点开的就是一个答非所问的动作，而页面本身看不出任何异常——
    这条用例是那个错位唯一的探测器。示例句同时必须是可接受的（不被越权闸拦下），
    否则建议条一点就红。
    """
    for spec in ACTION_SPECS:
        assert spec.example, f"{spec.name} 没有示例句：页面上这条建议会预填出空输入框"
        assert AssistantService._match_intent(spec.example) == spec.name, (
            f"{spec.name} 的示例句「{spec.example}」被判成了 {AssistantService._match_intent(spec.example)}"
        )
        assert out_of_scope(spec.example) is None, f"{spec.name} 的示例句被判成越权：{out_of_scope(spec.example)}"


def test_示例句非空且动作集合无重复() -> None:
    """12 个动作各自一条示例：重复的示例句会让两条建议指向同一个动作，页面看不出来。"""
    examples = [spec.example for spec in ACTION_SPECS]
    assert len(examples) == len(set(examples)), "示例句重复：有两条建议会指向同一件事"
    assert all(example.strip() == example and example for example in examples)


@pytest.mark.asyncio
async def test_llm建议白名单外动作时拒绝并计数() -> None:
    llm = FakeLlm(action="delete.all")
    service = AssistantService(actions=build_actions(Recorder()), llm=llm, settings=settings())
    events = await collect(service, "随便说点什么吧")
    assert llm.json_calls == 1
    assert any(event["action"] == "delete.all" for event in service.rejections)
    assert any("越权" in event.data.get("note", "") or event.type == "answer" for event in events)


# ---------- 四类任务的通路 ----------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "action", "call_prefix"),
    [
        ("最近发布了哪些预警", "query.warnings", "list_warnings"),
        ("最近的链路事件", "query.chains", "list_chains"),
        ("查一下任务 stu_0123456789abcdef", "query.tasks", "get_task"),
        ("540121 有哪些站点", "query.stations", "list_stations"),
        ("在线智能体有几个", "query.agents", "list_agents"),
        ("P95 时延是多少", "query.metrics", "latency_report"),
        ("wrn_0123456789abcdef0123 为什么是红色", "explain.warning", "get_warning"),
        ("trc_0123456789abcdef 是谁判的", "explain.chain", "get_chain"),
    ],
)
async def test_只读任务直达且不产生待确认动作(text: str, action: str, call_prefix: str) -> None:
    rec = Recorder()
    service = AssistantService(actions=build_actions(rec), settings=settings())
    events = await collect(service, text, reporter="值班员")
    intents = [event for event in events if event.type == "intent"]
    assert intents[0].data["action"] == action
    assert not any(event.type == "proposal" for event in events)
    assert any(call in call for call in rec.calls), rec.calls


@pytest.mark.asyncio
async def test_预案问答给出内置剧本步骤并如实说明佐证为空() -> None:
    service = AssistantService(actions=build_actions(Recorder()), settings=settings())
    events = await collect(service, "泥石流该怎么办", reporter="值班员")
    result = next(event for event in events if event.type == "result")
    assert result.data["steps"] and result.data["steps"][0]["task_type"]
    assert "无命中" in result.data["text"]


# ---------- 人工确认 ----------


@pytest.mark.asyncio
async def test_演练与上报先给待确认动作执行后才落地() -> None:
    for text, action, call in (
        ("演练一次 surge 3 轮", "run.drill", "run_drill"),
        ("帮我上报：540121 沟道出现泥石流迹象", "create.report", "submit_report"),
    ):
        rec = Recorder()
        service = AssistantService(actions=build_actions(rec), settings=settings())
        events = await collect(service, text, reporter="值班员", region_code="540121")
        proposals = [event for event in events if event.type == "proposal"]
        assert len(proposals) == 1, text
        assert proposals[0].data["action"] == action
        assert rec.calls == [], "未确认就执行了"

        first = await service.confirm(session_id=proposals[0].data["session_id"], action_id=proposals[0].data["action_id"])
        assert first["status"] == "executed", first
        assert any(call in entry for entry in rec.calls)

        again = await service.confirm(session_id=proposals[0].data["session_id"], action_id=proposals[0].data["action_id"])
        assert again["status"] == "rejected" and "不重复执行" in again["reason"]


@pytest.mark.asyncio
async def test_确认跨会话不通用() -> None:
    rec = Recorder()
    service = AssistantService(actions=build_actions(rec), settings=settings())
    events = await collect(service, "演练一次", reporter="值班员")
    proposal = next(event for event in events if event.type == "proposal")
    result = await service.confirm(session_id="as_other_session", action_id=proposal.data["action_id"])
    assert result["status"] == "rejected"
    assert rec.calls == []


@pytest.mark.asyncio
async def test_过期动作确认后不执行() -> None:
    rec = Recorder()
    service = AssistantService(actions=build_actions(rec), settings=settings(assistant_session_ttl_seconds=0.001))
    events = await collect(service, "演练一次", reporter="值班员")
    proposal = next(event for event in events if event.type == "proposal")
    await asyncio.sleep(0.05)
    result = await service.confirm(session_id=proposal.data["session_id"], action_id=proposal.data["action_id"])
    assert result["status"] in ("expired", "rejected")
    assert rec.calls == []


# ---------- 认知镜像与不编数 ----------


@pytest.mark.asyncio
async def test_认知镜像叙述包含通道回执与降级事实() -> None:
    service = AssistantService(actions=build_actions(Recorder()), settings=settings())
    events = await collect(service, "wrn_0123456789abcdef0123 为什么是红色", reporter="值班员")
    result = next(event for event in events if event.type == "result")
    facts = result.data["facts"]
    assert "sms=delivered" in facts and "broadcast=failed" in facts
    assert "R-DEBRIS-RAIN-2" in facts
    assert "研判降级" in facts


@pytest.mark.asyncio
async def test_llm润色冒出没有的数字时整段弃用() -> None:
    service = AssistantService(actions=build_actions(Recorder()), llm=FakeLlm(polish="影响 8888 人，等级 2"), settings=settings())
    events = await collect(service, "wrn_0123456789abcdef0123 依据是什么", reporter="值班员")
    answer = next(event for event in events if event.type == "answer")
    assert "8888" not in answer.data["text"]
    assert "sms=delivered" in answer.data["text"]
    assert any("新数字" in row["reason"] for row in service.rejections)


@pytest.mark.asyncio
async def test_llm润色失败时回到确定性叙述() -> None:
    service = AssistantService(actions=build_actions(Recorder()), llm=FakeLlm(error=RuntimeError("网关超时")), settings=settings())
    events = await collect(service, "wrn_0123456789abcdef0123 依据是什么", reporter="值班员")
    answer = next(event for event in events if event.type == "answer")
    assert "sms=delivered" in answer.data["text"]


# ---------- 装配事实 ----------


@pytest.mark.asyncio
async def test_缺依赖的动作如实说明缺哪个能力() -> None:
    service = AssistantService(actions=AssistantActions(), settings=settings())
    events = await collect(service, "在线智能体有几个", reporter="值班员")
    answer = next(event for event in events if event.type == "answer")
    assert "缺少依赖" in answer.data["text"] and "list_agents" in answer.data["text"]


def test_装配面区分未配置与已配置且动作集合与白名单一致() -> None:
    caps = AssistantService(actions=build_actions(Recorder()), settings=settings()).capabilities()
    assert caps["llm_configured"] is False
    assert {row["action"] for row in caps["actions"]} == set(ACTION_WHITELIST)
    assert all(row["available"] for row in caps["actions"]), "动作依赖名与 AssistantActions 字段对不上"

    empty = AssistantService(actions=AssistantActions(), llm=FakeLlm(), settings=settings()).capabilities()
    assert empty["llm_configured"] is True
    unavailable = {row["action"]: row["missing"] for row in empty["actions"] if not row["available"]}
    assert unavailable, "空装配面上居然所有动作都可执行"
    assert all(missing for missing in unavailable.values()), "缺依赖必须点名缺哪个"


@pytest.mark.asyncio
async def test_会话数量有上界且空消息不出答案() -> None:
    service = AssistantService(actions=build_actions(Recorder()), settings=settings())
    assert (await collect(service, "   "))[1].type == "error"
    for index in range(260):
        await collect(service, "查询所有站点", session_id=f"as_session{index:04d}")
    assert len(service._sessions) <= 200


@pytest.mark.asyncio
async def test_缺参数的那三条示例句要说清这个ID在哪个页面读得到() -> None:
    """能力面把每条动作的 example 交给前端当"点这里试一条"的预填文本（`assistant.py` 里
    `ActionSpec.example` 的注释就是这么写的）。可三条带标识符的示例句原样发出去都必然缺参数：
    真机在全新后端上点「查询任务单元」，答案帧就停在"缺少任务单元 ID（stu_…）"——
    芯片成了死胡同，页面上哪儿能读到这个 ID 一个字没说。
    """
    service = AssistantService(actions=build_actions(Recorder()), settings=settings())
    cases = [
        ("任务单元清单", ("stu_", "预警发布")),
        ("解释 wrn_… 为什么定这个等级", ("wrn_", "预警标识")),
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
    service = AssistantService(actions=build_actions(Recorder()), settings=settings())
    events = await collect(service, "任务单元清单", reporter="值班员")
    answer = next(event for event in events if event.type == "answer")
    assert "缺少" in str(answer.data["text"]), answer.data["text"]
