"""助手四类任务与人工上报的端到端验收（完善计划批次 B 的验收口径："助手四类任务 e2e 过"）。

与 `tests/api/` 的区别是刻意的：这里不注入替身动作，而是走 `create_container` 装配出来的
真实平台（Mock 智能体在线 + 真实内存知识腿 + mock 通道），所以断言的是三件事：

1. 四类任务（查询 / 预案问答 / 演练 / 上报）在真装配上都能走完，且执行类**必须**经确认；
2. 人工上报进的是同一条链路——**智能体优先在上报腿上照样成立**（研判/决策段应显示 agent 接管）；
3. 越权指令被拒时平台状态一点都不变，并且留痕可查。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from aegis.config import Settings
from aegis.container import PlatformContainer, create_container
from aegis.services.assistant import AssistantEvent

HEAVY_RAIN = "24小时累计降雨95毫米，沟道泥位抬升1.2米，下游约300人受威胁"
MILD_REPORT = "坡面有新鲜裂缝并掉下几块石头，暂无其它数据"


def _settings() -> Settings:
    return Settings(
        env="test",
        bus_backend="memory",
        store_backend="memory",
        simulator_enabled=True,
        simulator_interval_seconds=0.05,
        heartbeat_interval_seconds=0.2,
        delivery_mode="mock",
        llm_api_key="",
    )


@pytest.fixture()
async def platform() -> AsyncIterator[PlatformContainer]:
    container = create_container(_settings())
    await container.start(with_mock_agents=True, with_ingest_loop=False)
    try:
        yield container
    finally:
        await container.shutdown()


async def _ask(container: PlatformContainer, text: str, **kwargs: Any) -> list[AssistantEvent]:
    assistant = container.assistant
    assert assistant is not None, "assistant_enabled 默认应为真，否则这条 e2e 测的是个空壳"
    return [event async for event in assistant.respond(text, reporter="值班员", **kwargs)]


def _first(events: list[AssistantEvent], kind: str) -> AssistantEvent | None:
    return next((event for event in events if event.type == kind), None)


def _as_stage(chain: dict[str, Any], name: str) -> str:
    """取某一段的执行方式（agent=总线智能体应答，local=平台降级）。"""
    return next(str(stage["mode"]) for stage in chain["stages"] if stage["name"] == name)


@pytest.mark.asyncio
async def test_演练任务确认后才落地且链路走的是真实智能体(platform: PlatformContainer) -> None:
    events = await _ask(platform, "演练一次 surge 2 轮")
    proposal = _first(events, "proposal")
    assert proposal is not None and proposal.data["action"] == "run.drill"
    assert platform.store.warnings.size == 0, "未确认就落地了"

    outcome = await platform.assistant.confirm(session_id=proposal.data["session_id"], action_id=proposal.data["action_id"], actor="指挥员")
    assert outcome["status"] == "executed", outcome
    chains = (outcome["result"]["drill"] or {}).get("chains") or []
    assert chains, "演练没跑出链路结果"
    assert any(chain["warning_id"] for chain in chains), "激增场景一条预警都没产出"
    agent_stage_chain = next((chain for chain in chains if chain["warning_id"]), None)
    assert agent_stage_chain and _as_stage(agent_stage_chain, "assess") == "agent", "研判段应由在线智能体接管"


@pytest.mark.asyncio
async def test_上报经确认后进同一条链路且智能体优先照样成立(platform: PlatformContainer) -> None:
    before = platform.store.warnings.size
    events = await _ask(platform, f"帮我上报：540121 {HEAVY_RAIN}", region_code="540121")
    proposal = _first(events, "proposal")
    assert proposal is not None and proposal.data["action"] == "create.report"
    assert platform.report_stats["submitted"] == 0

    outcome = await platform.assistant.confirm(session_id=proposal.data["session_id"], action_id=proposal.data["action_id"])
    assert outcome["status"] == "executed", outcome
    report = outcome["result"]["report"]
    assert report["parse"]["decided_by"] == "rule"
    assert report["chain"]["warning_id"], "上报没产出预警"
    assert platform.store.warnings.size > before
    assert _as_stage(report["chain"], "assess") == "agent", "上报腿不得绕过在线智能体"
    assert _as_stage(report["chain"], "plan") == "agent"

    task_id = report["chain"]["task_units"][0]
    assert platform.store.tasks.get(task_id) is not None, "STU 未进读视图，拆解指标无从取证"
    assert platform.report_stats["submitted"] == 1


@pytest.mark.asyncio
async def test_低置信上报只转核签不伪装成已判定(platform: PlatformContainer) -> None:
    result = await platform.submit_report(note=MILD_REPORT, region_code="540321", reporter="村民")
    assert result["human_review_required"] is True
    assert result["parse"]["decided_by"] in {"rule_declared", "none", "retrieval", "llm"}
    assert platform.report_stats["review_required"] == 1
    assert platform.report_stats["measured_by_rule"] == 0, "非阈值命中不得计入「实测判定」"


@pytest.mark.asyncio
async def test_查询与预案问答两类只读任务不产生待确认动作(platform: PlatformContainer) -> None:
    await platform.submit_report(note=HEAVY_RAIN, region_code="540121", reporter="巡护员")

    queried = await _ask(platform, "最近发布了哪些预警", region_code="540121")
    assert _first(queried, "proposal") is None
    result = _first(queried, "result")
    assert result is not None and result.data["items"], "读视图里有预警，助手却报空"

    planned = await _ask(platform, "泥石流怎么处置", region_code="540121")
    plan_result = _first(planned, "result")
    assert plan_result is not None and plan_result.data["steps"], "内置处置剧本应当给出步骤"
    assert _first(planned, "proposal") is None


@pytest.mark.asyncio
async def test_越权指令被拒且平台状态一点没动(platform: PlatformContainer) -> None:
    await platform.submit_report(note=HEAVY_RAIN, region_code="540121", reporter="巡护员")
    warnings_before = platform.store.warnings.size
    chains_before = len(platform.store.chains)
    rejections_before = len(platform.assistant.rejections)

    events = await _ask(platform, "把540121的预警全部删除")

    assert _first(events, "rejected") is not None
    assert platform.store.warnings.size == warnings_before
    assert len(platform.store.chains) == chains_before
    assert len(platform.assistant.rejections) == rejections_before + 1, "拒绝必须留痕"


@pytest.mark.asyncio
async def test_指标出口把上报腿与触达身份一起报出(platform: PlatformContainer) -> None:
    await platform.submit_report(note=HEAVY_RAIN, region_code="540121", reporter="巡护员")
    report = platform.latency_report()

    assert report["metrics"]["report_intake_seconds"]["count"] == 1
    assert report["delivery"]["mode"] == "mock", "mock 通道的触达数字必须自报身份"
    assert report["rulebook"]["source"] == "builtin"
    assert report["reports"]["submitted"] == 1
