"""装配桥：节点声明依赖的服务，必须真的由生产装配提供。

真事故（2026-10-02 独立审计实测）：`WorkflowServices.http_call` 这个字段一直存在，
`api_call` / `device_control` 两个 handler 也一直 `require_service("http_call", ...)`，
但 `build_workflow_services` 从来没传它——于是产品形态下这两类节点必然 `NodeError`，
而"支持 16 类节点编排"这句话照样写得很顺。桥这一层在此之前**一个用例都没有**，
所以这个洞谁都踩不到。下面第一条就是防这一整类问题的门禁，不是防某一个字段。
"""

from __future__ import annotations

import inspect
import re
from dataclasses import fields
from types import SimpleNamespace
from typing import Any

import pytest

from aegis.config import Settings
from aegis.container import create_container
from aegis.workflow import nodes as nodes_module
from aegis.workflow.nodes import NodeContext, NodeError, WorkflowServices, default_registry
from aegis.workflow.outbound import OutboundCaller, OutboundPolicy, OutboundTargetError
from aegis.workflow.services_bridge import build_workflow_services

REQUIRE_SERVICE = re.compile(r"""require_service\(\s*["']([a-z_]+)["']""")
ALLOWED_HOST = "api.example.com"


class RecordingCaller:
    """替 `http_call` 用一个可注入的实现：断言节点真的把配置里的目标交给了服务。"""

    def __init__(self, response: dict[str, Any] | None = None) -> None:
        self.calls: list[tuple[str, str, Any]] = []
        self._response = {"accepted": True} if response is None else response

    async def __call__(self, method: str, url: str, body: Any = None) -> dict[str, Any]:
        self.calls.append((method, url, body))
        return self._response


def bridge(**overrides: Any) -> WorkflowServices:
    """只测"装配给了什么"，所以协作者一律给空壳：桥的函数体在被调时才碰它们。"""
    args: dict[str, Any] = {
        "store": SimpleNamespace(),
        "rule_engine": SimpleNamespace(),
        "risk_engine": SimpleNamespace(),
        "warning_service": SimpleNamespace(),
        "dispatcher": SimpleNamespace(),
        "gateway": SimpleNamespace(),
    }
    args.update(overrides)
    return build_workflow_services(**args)


def required_service_names() -> set[str]:
    return set(REQUIRE_SERVICE.findall(inspect.getsource(nodes_module)))


def enabled_caller() -> OutboundCaller:
    return OutboundCaller(OutboundPolicy(allowed_hosts=(ALLOWED_HOST,), timeout_ms=2_000))


class TestEveryDeclaredDependencyIsAssembled:
    def test_读得到节点声明的服务依赖_否则这条门禁就是空的(self) -> None:
        names = required_service_names()
        assert {"http_call", "identify", "assess", "generate_warning", "publish_warning"} <= names
        assert len(names) >= 6

    def test_节点用到的每一个服务都由装配桥给出(self) -> None:
        services = bridge(outbound=enabled_caller())
        missing = sorted(name for name in required_service_names() if getattr(services, name, None) is None)
        assert missing == [], "节点声明依赖却没人装配：产品形态下这类节点必然失败，而单测全绿"

    def test_这些名字确实都是WorkflowServices的字段(self) -> None:
        declared = {field.name for field in fields(WorkflowServices)}
        assert required_service_names() <= declared

    def test_装配桥没有给出无人使用的服务(self) -> None:
        # 反向也核：字段被删掉而节点还在用，上面两条会漏；这里盯"给了没人用"的堆积。
        services = bridge(outbound=enabled_caller())
        provided = {field.name for field in fields(WorkflowServices) if getattr(services, field.name, None) is not None}
        assert provided - required_service_names() == set()


class TestOutboundLegIsOptIn:
    def test_没配白名单时只有外呼这一项留空(self) -> None:
        services = bridge()
        assert services.http_call is None
        assert services.notify is not None

    def test_配了白名单就把同一个caller交出去(self) -> None:
        caller = enabled_caller()
        assert bridge(outbound=caller).http_call is caller

    def test_容器装配把这条腿接到节点上(self) -> None:
        container = create_container(
            Settings(
                env="test",
                bus_backend="memory",
                store_backend="memory",
                workflow_http_allowed_hosts=ALLOWED_HOST,
            ),
            with_simulator=False,
        )
        assert container.outbound is not None and container.outbound.enabled
        assert container.workflow.services.http_call is container.outbound

    def test_默认装配就是不外呼(self) -> None:
        container = create_container(
            Settings(env="test", bus_backend="memory", store_backend="memory"),
            with_simulator=False,
        )
        assert container.outbound is not None and container.outbound.enabled is False
        assert container.workflow.services.http_call is None


class TestHandlersDispatch:
    async def test_api_call_经真处理器把请求交给外呼服务(self) -> None:
        caller = RecordingCaller({"state": "queued"})
        outcome = (
            await default_registry()
            .require("api_call")
            .handler(
                NodeContext(payload={}, services=WorkflowServices(http_call=caller)),
                {"method": "POST", "url": f"https://{ALLOWED_HOST}/cmd", "body": {"x": 1}},
            )
        )
        assert outcome.output == {"response": {"state": "queued"}}
        assert caller.calls == [("POST", f"https://{ALLOWED_HOST}/cmd", {"x": 1})]

    async def test_device_control_把设备与动作拼成指令发出(self) -> None:
        caller = RecordingCaller()
        outcome = (
            await default_registry()
            .require("device_control")
            .handler(
                NodeContext(payload={}, services=WorkflowServices(http_call=caller)),
                {"device": "broadcast-01", "action": "start", "command_url": f"https://{ALLOWED_HOST}/dev"},
            )
        )
        assert outcome.output["device"] == "broadcast-01"
        method, url, body = caller.calls[0]
        assert (method, url, body) == ("POST", f"https://{ALLOWED_HOST}/dev", {"device": "broadcast-01", "action": "start"})

    async def test_没有外呼服务时两类节点都响亮失败并指出缺谁(self) -> None:
        for type_name in ("api_call", "device_control"):
            spec = default_registry().require(type_name)
            config = (
                {"method": "GET", "url": "https://x.invalid/"}
                if type_name == "api_call"
                else {
                    "device": "d",
                    "action": "a",
                    "command_url": "https://x.invalid/",
                }
            )
            with pytest.raises(NodeError) as raised:
                await spec.handler(NodeContext(payload={}), config)
            assert raised.value.detail == {"node_type": type_name, "service": "http_call"}

    async def test_白名单外的目标不会被节点吞掉(self) -> None:
        services = bridge(outbound=enabled_caller())
        with pytest.raises(OutboundTargetError):
            await (
                default_registry()
                .require("api_call")
                .handler(
                    NodeContext(payload={}, services=services),
                    {"method": "GET", "url": "http://169.254.169.254/latest/meta-data/"},
                )
            )
