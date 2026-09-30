"""真实 Jaeger 取证：跨度经 OTLP/HTTP 上报后必须能从 Jaeger 自己的查询 API 原样取回。

默认跳过（不依赖任何服务也能跑完单元测试）。本地起 Jaeger（Apache-2.0 全在一体镜像）：
    docker run -d --rm --name aegis-jaeger -p 16686:16686 -p 4318:4318 jaegertracing/all-in-one:latest
再执行：
    AEGIS_TEST_OTEL_ENDPOINT=http://127.0.0.1:4318 \
        .venv/Scripts/python.exe -m pytest -q -m slow tests/integration/test_telemetry_otel.py

取证要点（验收报告可直接引用）：契约 trace_id 去掉 `trc_` 前缀即 Jaeger 的 traceID，
`/api/traces/<traceID>` 与按 tag 检索两条路径都必须命中同一条链路。
"""

from __future__ import annotations

import asyncio
import os
import secrets
from typing import Any

import httpx
import pytest

from aegis.errors import DeadlineExceededError
from aegis.observability import telemetry
from aegis.observability.instrumentation import span_stage
from aegis.observability.tracer import Tracer

OTEL_ENDPOINT = os.getenv("AEGIS_TEST_OTEL_ENDPOINT", "")
JAEGER_API = os.getenv("AEGIS_TEST_JAEGER_API", "http://127.0.0.1:16686")
SERVICE = "aegis-backend"
INDEX_TIMEOUT_S = 20.0

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not OTEL_ENDPOINT, reason="未设置 AEGIS_TEST_OTEL_ENDPOINT，跳过真实 Jaeger 取证"),
]


def _tag_value(span: dict[str, Any], key: str) -> Any:
    return next((tag["value"] for tag in span["tags"] if tag["key"] == key), None)


async def _emit_event_trace(trace: str) -> str:
    """跑一次"事件级"埋点：根跨度 + 三个子跨度，其中一次协同事务既超预算又失败。"""
    tr = Tracer()
    telemetry.init_telemetry(service_name=SERVICE, deployment_environment="test", otlp_endpoint=OTEL_ENDPOINT)

    async def child(name: str, budget_ms: float, sleep_s: float, **attrs: Any) -> None:
        async with span_stage(name, budget_ms, tracer=tr, trace_id=trace, **attrs):
            await asyncio.sleep(sleep_s)

    async with span_stage("workflow_instance_ms", 10_000, tracer=tr, trace_id=trace, event_id="evt_" + secrets.token_hex(6)) as root:
        await child("workflow_schedule_ms", 2_000, 0.02, node_id="assess")
        await child("sync_agent_to_gateway_ms", 3_000, 0.02, agent_id="assess.mock01")
        with pytest.raises(DeadlineExceededError):
            async with span_stage("collab_txn", 60, tracer=tr, trace_id=trace, agent_id="plan.mock01"):
                await asyncio.sleep(0.12)
                raise DeadlineExceededError("研判智能体超期未回")
        await child("warning_reach_ms", 1_200_000, 0.01, channel="sms")
    root_span_id = root.get_span_context().span_id
    telemetry.shutdown_telemetry()
    assert tr.ledger.stats("collab_txn").breaches == 1, "本地账本与 Jaeger 必须给出同一个违约结论"
    return f"{root_span_id:016x}"


async def _query_json(client: httpx.AsyncClient, url: str, params: dict[str, str]) -> dict[str, Any]:
    response = await client.get(url, params=params, timeout=10.0)
    response.raise_for_status()
    return response.json()


async def _await_trace_by_id(client: httpx.AsyncClient, trace_hex: str) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + INDEX_TIMEOUT_S
    while asyncio.get_running_loop().time() < deadline:
        payload = await _query_json(client, f"{JAEGER_API}/api/traces/{trace_hex}", {})
        if payload.get("data"):
            return payload["data"][0]
        await asyncio.sleep(0.5)
    raise AssertionError(f"Jaeger 未在 {INDEX_TIMEOUT_S}s 内返回 trace {trace_hex}")


class TestLiveJaegerEvidence:
    async def test_trace_is_retrievable_with_sla_and_error_tags(self) -> None:
        contract_trace = "trc_" + secrets.token_hex(8)
        trace_hex = telemetry.w3c_trace_id(contract_trace)
        assert trace_hex is not None

        root_span_hex = await _emit_event_trace(contract_trace)

        async with httpx.AsyncClient() as client:
            by_id = await _await_trace_by_id(client, trace_hex)
            assert int(by_id["traceID"], 16) == int(trace_hex, 16), "Jaeger traceID 必须等于契约 trace_id 的十六进制部分"
            spans = {span["operationName"]: span for span in by_id["spans"]}
            assert {"workflow_instance_ms", "workflow_schedule_ms", "sync_agent_to_gateway_ms", "collab_txn", "warning_reach_ms"} <= set(
                spans
            )

            collab = spans["collab_txn"]
            assert _tag_value(collab, "aegis.trace_id") == contract_trace
            assert _tag_value(collab, "aegis.sla.budget_ms") == 60.0
            assert _tag_value(collab, "aegis.sla.breach") is True
            assert _tag_value(collab, "aegis.sla.over_ms") > 0
            assert _tag_value(collab, "aegis.sla.outcome") == "error"
            assert _tag_value(collab, "otel.status_code") == "ERROR"
            assert "研判智能体超期未回" in str(_tag_value(collab, "otel.status_description"))
            assert _tag_value(spans["sync_agent_to_gateway_ms"], "aegis.sla.breach") is False

            process = by_id["processes"][by_id["spans"][0]["processID"]]
            tags = {item["key"]: item["value"] for item in process["tags"]}
            assert process["serviceName"] == SERVICE, "Jaeger 把 service.name 映射为 process.serviceName"
            assert tags["deployment.environment.name"] == "test"
            assert tags["service.version"]

            # 树形：三个子跨度都挂在根跨度下（而不是并列根）
            for name in ("workflow_schedule_ms", "sync_agent_to_gateway_ms", "collab_txn", "warning_reach_ms"):
                refs = spans[name].get("references") or []
                assert refs and refs[0]["refType"] == "CHILD_OF"
                assert refs[0]["spanID"] == root_span_hex

            by_tag = await _query_json(
                client,
                f"{JAEGER_API}/api/traces",
                {"service": SERVICE, "tags": f'{{"aegis.trace_id":"{contract_trace}"}}', "limit": "20"},
            )
            assert [item["traceID"] for item in by_tag["data"]] and int(by_tag["data"][0]["traceID"], 16) == int(trace_hex, 16)

    async def test_each_disaster_event_gets_its_own_trace(self) -> None:
        first, second = "trc_" + secrets.token_hex(8), "trc_" + secrets.token_hex(8)
        await _emit_event_trace(first)
        await _emit_event_trace(second)

        async with httpx.AsyncClient() as client:
            one = await _await_trace_by_id(client, telemetry.w3c_trace_id(first) or "")
            two = await _await_trace_by_id(client, telemetry.w3c_trace_id(second) or "")
            assert one["traceID"] != two["traceID"]
            assert _tag_value(one["spans"][0], "aegis.trace_id") == first
            assert _tag_value(two["spans"][0], "aegis.trace_id") == second

    async def test_dead_endpoint_never_breaks_business_call(self) -> None:
        """弱网/端点消失：业务返回值与跨度记账都不受影响，只留下丢弃计数。"""
        contract_trace = "trc_" + secrets.token_hex(8)
        tr = Tracer()
        telemetry.init_telemetry(service_name=SERVICE, deployment_environment="test", otlp_endpoint="http://127.0.0.1:14318")

        async def business() -> str:
            async with span_stage("collab_txn", 1_000, tracer=tr, trace_id=contract_trace):
                return "平台降级结论"

        try:
            assert await business() == "平台降级结论"
        finally:
            telemetry.shutdown_telemetry()
        assert telemetry.dropped_span_count() > 0
        assert tr.ledger.stats("collab_txn").count == 1
