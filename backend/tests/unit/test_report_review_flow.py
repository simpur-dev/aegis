"""低置信度上报的人工核签工单（架构文档 §6.2 场景二的后半段）。

要钉住的是"开单不拦发布"这件事的两头：
- 等级不是阈值命中所得 → 必须真的开出一个等人在画布上签的实例（不是一句标志位）；
- 阈值命中的上报 → 不该多开一张单，否则核签队列会被确定性的告警淹没。
另外工单不得重复发布预警：那会把触达数字凭空翻一倍。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from aegis.config import Settings
from aegis.container import PlatformContainer, create_container
from aegis.workflow.templates import REPORT_REVIEW_TEMPLATE, register_report_review_template

HEAVY_RAIN = "24小时累计降雨95毫米，沟道泥位抬升1.2米"
DECLARED_ONLY = "发生泥石流，请求红色预警"


def _settings() -> Settings:
    return Settings(
        env="test",
        bus_backend="memory",
        store_backend="memory",
        simulator_enabled=False,
        delivery_mode="mock",
        llm_api_key="",
    )


@pytest.fixture()
async def platform() -> AsyncIterator[PlatformContainer]:
    container = create_container(_settings(), with_simulator=False)
    await container.start()
    try:
        yield container
    finally:
        await container.shutdown()


def test_核签流程的三态与出口节点都对得上() -> None:
    options = next(node["config"]["options"] for node in REPORT_REVIEW_TEMPLATE["nodes"] if node["type"] == "human_review")
    branches = {edge["condition"] for edge in REPORT_REVIEW_TEMPLATE["edges"]}
    assert set(options) == branches, "选项没有对应出口就等于有一签无处落"
    types = {str(node["type"]) for node in REPORT_REVIEW_TEMPLATE["nodes"]}
    assert not types & {"warning_publish", "warning_generate"}, "核签流程再发一次预警会把触达数字翻倍"


@pytest.mark.asyncio
async def test_注册是幂等的(platform: PlatformContainer) -> None:
    name = REPORT_REVIEW_TEMPLATE["name"]
    assert platform.workflow.latest_definition(name) is not None, "启动期就该把核签流程注册上"
    assert await register_report_review_template(platform.workflow) is None
    assert (await register_report_review_template(platform.workflow, force=True)) == name
    assert len(platform.workflow.repository.versions(name)) >= 2, "force 应产生新版本而不是覆盖历史"


@pytest.mark.asyncio
async def test_核签流程被归档后_重新注册会把开单能力带回来(platform: PlatformContainer) -> None:
    """现场动线：值班员在 /workflow 把「人工上报核签流程」归档（一按就生效，没有确认），
    此后每一条低置信度上报都只留 `human_review_required` 标志、开不出工单；
    注册只在启动期跑一次，运行中不会补，所以这个班次的核签腿就断在这里。
    这里两头都钉：归档确实断单，重新注册确实恢复。"""
    name = REPORT_REVIEW_TEMPLATE["name"]
    active = platform.workflow.latest_definition(name)
    assert active is not None and active.status == "active"
    await platform.workflow.archive(active.workflow_id)

    blocked = await platform.submit_report(note=DECLARED_ONLY, region_code="540200", reporter="村民")
    assert blocked["human_review_required"] is True
    assert blocked["review"] is None, "归档态不该被当成可用定义——这条就是修复前的现场"

    assert await register_report_review_template(platform.workflow) == name
    revived = platform.workflow.latest_definition(name)
    assert revived is not None and revived.status == "active"
    assert revived.version == active.version + 1

    opened = await platform.submit_report(note=DECLARED_ONLY, region_code="540200", reporter="村民")
    review = opened["review"]
    assert review is not None and review["status"] == "waiting", "恢复的是开单能力，不是多一行定义"
    assert review["workflow_id"] == revived.workflow_id


@pytest.mark.asyncio
async def test_阈值命中的上报不开核签单(platform: PlatformContainer) -> None:
    result = await platform.submit_report(note=HEAVY_RAIN, region_code="540121", reporter="巡护员")
    assert result["parse"]["decided_by"] == "rule"
    assert result["human_review_required"] is False
    assert result["review"] is None
    assert platform.report_stats["reviews_opened"] == 0


@pytest.mark.asyncio
async def test_申报等级的上报开出可签的实例(platform: PlatformContainer) -> None:
    warnings_before = platform.store.warnings.size
    result = await platform.submit_report(note=DECLARED_ONLY, region_code="540200", reporter="村民", location=(91.1, 29.6))
    review = result["review"]
    assert result["human_review_required"] is True
    assert review and review["instance_id"] and review["pending_node"] == "review"
    assert review["status"] == "waiting", review
    assert review["options"] == ["approve", "adjust", "reject"]
    assert platform.report_stats["reviews_opened"] == 1
    assert platform.store.warnings.size == warnings_before + 1, "核签不拦发布：预警照发"

    detail = platform.workflow.instance_detail(str(review["instance_id"])) or {}
    payload: dict[str, Any] = detail.get("payload") or {}
    assert payload["decided_by"] == "rule_declared"
    assert payload["location"] == [91.1, 29.6]
    assert any("申报值" in str(item) for item in payload["why_review"]), "为什么被转核签必须写在工单上"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("choice", "branch"),
    [("approve", "ok"), ("adjust", "adjust"), ("reject", "drop")],
    ids=["approve", "adjust", "reject"],
)
async def test_核签三选一都能把工单签完(platform: PlatformContainer, choice: str, branch: str) -> None:
    """三个选项都要真签到底：只测一个的话，另两条边接错节点也不会红。"""
    result = await platform.submit_report(note="坡面裂缝在变宽", region_code="540300", reporter="村民")
    review = result["review"]
    assert review is not None and review["instance_id"] and review["pending_node"]

    detail = await platform.workflow.resume(str(review["instance_id"]), node_id=str(review["pending_node"]), decision={"choice": choice})
    assert detail["status"] == "succeeded", detail
    assert all(node["state"] != "awaiting_human" for node in detail["nodes"])
    signed = next(node for node in detail["nodes"] if node["node_id"] == "review")
    assert signed["output"]["choice"] == choice

    fired = {node["node_id"] for node in detail["nodes"] if node["state"] == "succeeded"}
    assert branch in fired, f"{choice} 没走到自己的出口分支：{fired}"
    assert (fired - {"review"}) == {branch}, f"{choice} 跑出了多余分支：{fired}"


@pytest.mark.asyncio
async def test_流程没注册时只留标志不开单(platform: PlatformContainer) -> None:
    """降级要可见而不是硬失败：核签流程缺失不该让上报入口整个报错。"""
    definition = platform.workflow.latest_definition(REPORT_REVIEW_TEMPLATE["name"])
    assert definition is not None
    await platform.workflow.archive(definition.workflow_id)

    result = await platform.submit_report(note=DECLARED_ONLY, region_code="540400", reporter="村民")
    assert result["human_review_required"] is True
    assert result["review"] is None, "开不了单要回一个明确的 None，而不是带着假 instance_id 的空壳"
    assert platform.report_stats["reviews_opened"] == 0, "开不了单就不能假装开了"
