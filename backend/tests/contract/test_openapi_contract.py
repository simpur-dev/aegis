"""OpenAPI 契约模糊测试（schemathesis）：按 schema 随机生成参数，验证实现不偏离自己声明的接口。

这一层专门抓"手写用例想不到的输入"：越界 limit、畸形 id、任意字符串 region_code、超长路径段。
契约探测的目的是发现偏差，不是把 CI 变成压测，因此：

- 只打只读端点（写侧会在平台内制造真实链路状态；SSE 长连接会挂住探测）；
- 显式限制 max_examples；
- `/api/v1/tasks/{id}` 单独放行 404：随机生成的 id 必然查不到，"查不到"是声明过的行为。

容器只装配不启动：只读接口只读内存视图，起总线只会让探测变慢、变不确定。
"""

from __future__ import annotations

import re

import pytest
import schemathesis
from hypothesis import settings

from aegis.api.app import create_app
from aegis.config import Settings
from aegis.container import PlatformContainer, create_container

# /readyz 不在模糊集合里：503 是它的语义本身（未就绪就该 503），
# 由下面的显式用例分别验证"未就绪 503 / 已就绪 200"，比随机撞更准确。
READ_ONLY = r"^/healthz$|^/api/v1/(telemetry|warnings|agents|collaboration|metrics/latency|integrations)$"
TASK_LOOKUP = r"^/api/v1/tasks/"
BASE_URL = "http://testserver"

_settings = Settings(env="test", bus_backend="memory", delivery_mode="mock", simulator_enabled=False)
_container: PlatformContainer = create_container(_settings)
APP = create_app(_settings, container=_container)
SCHEMA = schemathesis.openapi.from_asgi("/openapi.json", app=APP)


def test_the_openapi_document_covers_the_sla_bearing_endpoints() -> None:
    """契约测试的前提是"契约存在"：考核指标涉及的端点必须出现在文档里。"""
    paths = set(APP.openapi().get("paths", {}))
    required = {
        "/healthz",
        "/readyz",
        "/api/v1/telemetry",
        "/api/v1/warnings",
        "/api/v1/collaboration",
        "/api/v1/metrics/latency",
        "/api/v1/integrations",
        "/api/v1/drill/run",
    }
    missing = required - paths
    assert not missing, f"考核相关端点未进 OpenAPI: {sorted(missing)}"


def test_filter_patterns_are_regex_and_select_real_operations() -> None:
    """过滤器静默失配会让整层契约测试空转，所以先把"确实选到了操作"钉成断言。"""
    assert re.compile(READ_ONLY).match("/api/v1/telemetry")
    assert not re.compile(READ_ONLY).match("/api/v1/drill/run")
    assert not re.compile(READ_ONLY).match("/api/v1/events/stream")
    assert len(list(SCHEMA.include(path_regex=READ_ONLY).get_all_operations())) >= 5
    assert len(list(SCHEMA.include(path_regex=TASK_LOOKUP).get_all_operations())) >= 1


@pytest.mark.slow
@SCHEMA.include(path_regex=READ_ONLY).parametrize()
@settings(max_examples=25, deadline=None)
def test_read_operations_satisfy_the_declared_contract(case: schemathesis.Case) -> None:
    """状态码与响应结构必须与声明一致，且不得出现 5xx。"""
    case.call_and_validate(base_url=BASE_URL)


def test_readyz_reports_503_before_the_bus_connects_and_200_after() -> None:
    """就绪探针的两态：未连接必须 503（这正是它存在的意义），连接后必须 200。

    刻意不进 TestClient 的上下文管理器：那会触发 lifespan 并自动 start 容器，
    "未就绪"这一态就永远测不到了。
    """
    import asyncio

    from fastapi.testclient import TestClient

    cold = create_container(_settings)
    assert TestClient(create_app(_settings, container=cold)).get("/readyz").status_code == 503

    warm = create_container(_settings)
    asyncio.run(warm.start())
    try:
        ready = TestClient(create_app(_settings, container=warm)).get("/readyz")
        assert ready.status_code == 200, ready.text
        assert ready.json()["bus"] == "memory"
    finally:
        asyncio.run(warm.shutdown())


@pytest.mark.slow
@SCHEMA.include(path_regex=TASK_LOOKUP).parametrize()
@settings(max_examples=12, deadline=None)
def test_task_lookup_accepts_only_declared_shapes(case: schemathesis.Case) -> None:
    """随机 id 查不到是声明过的 404；除此之外的响应必须仍然符合 schema。"""
    response = case.call(base_url=BASE_URL)
    if response.status_code == 404:
        return
    case.validate_response(response)
