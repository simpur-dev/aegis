"""节点类型分派矩阵：装配好的引擎必须真的驱动过画布上的**每一种**节点类型。

审计口径（2026-10-02）：`WorkflowServices` 的字段与 `require_service` 的调用点都有单测，
但"某一类节点在真实装配下能不能跑完"此前只有 `data_fetch/threshold/branch/join/delay/notify`
几类在引擎测试里出现过——`hazard_identify`、`risk_assess`、`situation_simulate`、
`warning_generate/publish`、`feedback_collect`、`degrade_to_rule`、`api_call`、
`device_control` 只在 handler 级或 `test_task_parser` 里被提到。节点注册表长到 16 类之后，
"≥10 类防控任务节点可视化编排"这句话要能被逐类指着说"引擎跑过它"。

这里刻意用**装配容器里的引擎**（`create_container` → `build_workflow_services`），
不是手搓的 `WorkflowServices(...)`：只有走生产装配，才能证明每类节点要的依赖服务真的从
装配桥给了出去——这正是上一轮抓到 `http_call` 缺失的那条路。

预警链那一格不是随便摆的：`warning_generate` 单独跑缺的是事件号，`warning_publish` 要的
是上一步真的产出的预警对象，`feedback_collect` 单独跑会拿到一个空 warning_id 而"成功"。
这三类必须由一条链跑通来作证据——单节点用例证不到它们。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from aegis.config import Settings
from aegis.container import PlatformContainer, create_container
from aegis.workflow.engine import WorkflowValidationError
from aegis.workflow.nodes import default_registry

ALLOWED_HOST = "api.example.com"
TRACE = "trc_" + "a" * 16

# 一个场景 = 一张最小的真实流程图；expectations 说"哪个节点必须产出哪个键"。
SCENARIOS: dict[str, dict[str, Any]] = {
    "预警链_从读数到发布与回执": {
        "nodes": [
            {"node_id": "fetch", "type": "data_fetch", "config": {"region_code": "540121", "limit": 5}},
            {
                "node_id": "judge",
                "type": "threshold",
                "config": {"upstream": "fetch", "conditions": [{"metric": "rain_10min", "op": ">=", "threshold": 30.0}]},
            },
            {"node_id": "risk", "type": "risk_assess", "config": {"upstream": "judge"}},
            {"node_id": "gen", "type": "warning_generate", "config": {}},
            {"node_id": "pub", "type": "warning_publish", "config": {"channels": ["sms"]}},
            {"node_id": "fb", "type": "feedback_collect", "config": {"wait_seconds": 0}},
        ],
        "edges": [
            {"source": "fetch", "target": "judge", "condition": ""},
            {"source": "judge", "target": "risk", "condition": ""},
            {"source": "risk", "target": "gen", "condition": ""},
            {"source": "gen", "target": "pub", "condition": ""},
            {"source": "pub", "target": "fb", "condition": ""},
        ],
        "payload": {
            "event_id": "evt_matrix_01",
            "region_code": "540121",
            "hazard_type": "debris_flow",
            "rows": [{"metric": "rain_10min", "value": 42.0, "quality_flag": "ok"}],
        },
        "expectations": {
            "fetch": "count",
            "judge": "triggered",
            "risk": "risk_level",
            "gen": "warning",
            "pub": "delivery",
            "fb": "feedback",
        },
    },
    "识别与推演": {
        "nodes": [
            {"node_id": "hz", "type": "hazard_identify", "config": {}},
            {"node_id": "sim", "type": "situation_simulate", "config": {}},
        ],
        "edges": [{"source": "hz", "target": "sim", "condition": ""}],
        "payload": {"region_code": "540121", "rows": [{"metric": "rain_10min", "value": 42.0, "quality_flag": "ok"}]},
        "expectations": {"hz": "hazard", "sim": "scenarios"},
    },
    "分支与汇聚与延时": {
        "nodes": [
            {"node_id": "src", "type": "delay", "config": {"seconds": 0}},
            {
                "node_id": "judge",
                "type": "branch",
                "config": {"upstream": "src", "rules": [{"when": "go", "op": "==", "value": True, "then": "advance"}], "default": "hold"},
            },
            {"node_id": "j", "type": "join", "config": {"upstream": ["src", "judge"]}},
        ],
        "edges": [
            {"source": "src", "target": "judge", "condition": ""},
            {"source": "judge", "target": "j", "condition": ""},
            {"source": "src", "target": "j", "condition": ""},
        ],
        "payload": {"go": True},
        "expectations": {"src": "delayed_seconds", "judge": "matched", "j": "merged"},
    },
    "通知与降级定级": {
        "nodes": [
            {"node_id": "notify", "type": "notify", "config": {"text": "矩阵演练：一条广播", "level": "warning"}},
            {"node_id": "degrade", "type": "degrade_to_rule", "config": {"risk_level": 2}},
        ],
        "edges": [{"source": "notify", "target": "degrade", "condition": ""}],
        "expectations": {"notify": "notified", "degrade": "risk_level"},
    },
    "外呼_查询接口": {
        "nodes": [
            {"node_id": "api", "type": "api_call", "config": {"method": "POST", "url": f"http://{ALLOWED_HOST}/ping", "body": {"probe": 1}}}
        ],
        "expectations": {"api": "response"},
    },
    "外呼_设备联动": {
        "nodes": [
            {
                "node_id": "dev",
                "type": "device_control",
                "config": {"device": "gate-3", "action": "close", "command_url": f"http://{ALLOWED_HOST}/cmd"},
            }
        ],
        "expectations": {"dev": "device"},
    },
}

HUMAN_CASE: dict[str, Any] = {
    "nodes": [{"node_id": "hr", "type": "human_review", "config": {"prompt": "是否发布红色预警", "options": ["approve", "reject"]}}],
    "expectations": {},
}


def _scenario_types() -> set[str]:
    return {str(node["type"]) for case in SCENARIOS.values() for node in case["nodes"]} | {str(HUMAN_CASE["nodes"][0]["type"])}


def _transport() -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "path": request.url.path})

    return httpx.MockTransport(handle)


def _settings() -> Settings:
    return Settings(
        env="test",
        bus_backend="memory",
        store_backend="memory",
        delivery_mode="mock",
        simulator_enabled=False,
        analytics_backend="off",
        workflow_http_allowed_hosts=ALLOWED_HOST,
    )


@pytest.fixture
async def assembled() -> AsyncIterator[PlatformContainer]:
    ctn = create_container(_settings(), with_simulator=False, outbound_client=httpx.AsyncClient(transport=_transport()))
    await ctn.start()
    try:
        yield ctn
    finally:
        await ctn.shutdown()


async def _run(ctn: PlatformContainer, case: dict[str, Any], label: str) -> dict[str, Any]:
    workflow = await ctn.workflow.create_definition(name=f"矩阵-{label}", description="", nodes=case["nodes"], edges=case.get("edges", []))
    return await ctn.workflow.start(workflow.workflow_id, trace_id=TRACE, payload=dict(case.get("payload", {})))


def _node(detail: dict[str, Any], node_id: str) -> dict[str, Any]:
    return next((node for node in detail["nodes"] if node["node_id"] == node_id), {})


class TestCoverage:
    def test_场景集合覆盖了注册表里的每一种节点类型(self) -> None:
        """加一类节点就必须在这里登记证据，否则"每类都被驱动过"这句话立刻失真。"""
        registered = set(default_registry().names())
        covered = _scenario_types()

        assert covered <= registered, f"矩阵里写了不存在的类型：{sorted(covered - registered)}"
        assert registered - covered == set(), f"这些节点类型还没有引擎级证据：{sorted(registered - covered)}"


class TestScenarios:
    @pytest.mark.parametrize("label", sorted(SCENARIOS))
    async def test_场景在装配好的引擎里跑完并且每类节点都有产出(self, assembled: PlatformContainer, label: str) -> None:
        case = SCENARIOS[label]
        detail = await _run(assembled, case, label)

        assert detail["status"] == "succeeded", f"{label} 没跑完：{detail}"
        for node_id, expect_key in case["expectations"].items():
            node = _node(detail, node_id)
            assert node.get("state") == "succeeded", f"{label}/{node_id} 状态不是成功：{node}"
            assert expect_key in node["output"], f"{label}/{node_id} 输出里没有 {expect_key}：{node['output']}"

    async def test_预警链真的产出了可发布的预警并拿到回执(self, assembled: PlatformContainer) -> None:
        """链的价值在于"下游吃的是上游真产出的东西"，所以把关键字段一路对下去。"""
        detail = await _run(assembled, SCENARIOS["预警链_从读数到发布与回执"], "chain")
        warning = _node(detail, "gen")["output"]["warning"]
        delivery = _node(detail, "pub")["output"]["delivery"]

        assert warning["event_id"] == "evt_matrix_01"
        assert delivery["warning_id"] == warning["warning_id"]
        assert int(delivery["delivered"]) > 0, "mock 通道一条都没发出去，等于这条证据是空的"

    async def test_外呼节点确实发出了请求并累计进装配状态(self, assembled: PlatformContainer) -> None:
        outbound = assembled.outbound
        assert outbound is not None
        before = int(outbound.status()["calls"])

        detail = await _run(assembled, SCENARIOS["外呼_查询接口"], "api_call")

        assert _node(detail, "api")["output"]["response"] == {"ok": True, "path": "/ping"}
        assert int(outbound.status()["calls"]) == before + 1


class TestDefinitionCrossCheck:
    """配置里点名的 upstream 必须是入边：这类错误该在定义期就挡住，不该等实例跑到那步才炸。

    写这条用例的原因是矩阵自己踩到的：`join` 指了一个没连线的节点时，此前的行为是
    流程跑到汇聚那一步才失败（"汇聚节点缺少上游结果"），现场只看到"流程卡住了"。
    """

    async def test_join指向没连线的节点时创建定义就被拒(self, assembled: PlatformContainer) -> None:
        with pytest.raises(WorkflowValidationError, match="不是它的入边"):
            await assembled.workflow.create_definition(
                name="缺连线",
                description="",
                nodes=[
                    {"node_id": "src", "type": "delay", "config": {"seconds": 0}},
                    {"node_id": "judge", "type": "delay", "config": {"seconds": 0}},
                    {"node_id": "j", "type": "join", "config": {"upstream": ["src", "judge"]}},
                ],
                edges=[{"source": "src", "target": "j", "condition": ""}],
            )

    async def test_single_id与列表两种写法同一条口径(self, assembled: PlatformContainer) -> None:
        """`upstream` 写单个 id 也要按入边校验——画布上两种形态都出现过。"""
        with pytest.raises(WorkflowValidationError, match="不是它的入边"):
            await assembled.workflow.create_definition(
                name="单值写法",
                description="",
                nodes=[
                    {"node_id": "src", "type": "delay", "config": {"seconds": 0}},
                    {"node_id": "judge", "type": "threshold", "config": {"upstream": "nope", "conditions": []}},
                ],
                edges=[{"source": "src", "target": "judge", "condition": ""}],
            )

    async def test_连了线的正常图照常通过(self, assembled: PlatformContainer) -> None:
        detail = await _run(assembled, SCENARIOS["分支与汇聚与延时"], "diamond")
        assert detail["status"] == "succeeded"
        assert _node(detail, "j")["output"]["merged"].keys() >= {"src", "judge"}


class TestOutboundGate:
    async def test_白名单外的目标让实例失败且拒因留在节点上(self, assembled: PlatformContainer) -> None:
        """类型化错误必须带着自己的原因出来：只报"节点执行异常: 类名"等于没报。"""
        case = {
            "nodes": [
                {"node_id": "api", "type": "api_call", "config": {"method": "GET", "url": "http://169.254.169.254/latest/meta-data"}}
            ],
            "expectations": {},
        }
        detail = await _run(assembled, case, "元数据端点")
        node = _node(detail, "api")

        assert detail["status"] == "failed"
        assert node["state"] == "failed"
        assert "主机不在白名单" in str(node.get("error")), node
        assert assembled.outbound is not None and int(assembled.outbound.status()["rejected"]) >= 1


class TestHumanReview:
    """人工节点的一等事实是"挂起 → 决策 → 继续"，两半都得在装配面上跑通。"""

    async def test_首次执行挂起等待决策而不是替人决定(self, assembled: PlatformContainer) -> None:
        detail = await _run(assembled, HUMAN_CASE, "human_review")

        assert detail["status"] == "waiting"
        assert _node(detail, "hr")["state"] == "awaiting_human"

    async def test_按决策恢复后走到对应分支(self, assembled: PlatformContainer) -> None:
        detail = await _run(assembled, HUMAN_CASE, "human_review")
        resumed = await assembled.workflow.resume(detail["instance_id"], node_id="hr", decision={"choice": "approve"})

        assert resumed["status"] == "succeeded"
        done = _node(resumed, "hr")
        assert done["output"]["choice"] == "approve"
        assert done["output"]["branch"] == "approve"

    async def test_决策值不在选项内时在门口就被拒(self, assembled: PlatformContainer) -> None:
        """引擎的前置校验：非法选项是调用方错误（400 语义），不该进节点执行再被判失败。"""
        detail = await _run(assembled, HUMAN_CASE, "human_review")
        instance_id = str(detail["instance_id"])

        with pytest.raises(WorkflowValidationError, match="人工决策值非法"):
            await assembled.workflow.resume(instance_id, node_id="hr", decision={"choice": "maybe"})

        still_waiting = assembled.workflow.instance_detail(instance_id)
        assert still_waiting is not None
        assert _node(still_waiting, "hr")["state"] == "awaiting_human"
