"""HTTP API 测试：端点契约、边界输入、错误码、并发请求与 SSE 事件流。

使用 ASGITransport 直连应用（不起真实端口、不依赖外部服务），
并显式注入已启动的容器，避免 lifespan 重复启动导致的重复订阅。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import httpx
import pytest

from aegis.api.app import create_app
from aegis.config import Settings
from aegis.connectors.simulator import HazardScenarioSimulator
from aegis.container import PlatformContainer, create_container
from aegis.domain.messages import utc_now

BASE = "http://testserver"


@pytest.fixture
async def container(settings: Settings) -> AsyncIterator[PlatformContainer]:
    ctn = create_container(settings)  # 带模拟场站，供 /drill 端点使用
    await ctn.start()
    yield ctn
    await ctn.shutdown()


@pytest.fixture
async def client(container: PlatformContainer) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(container.settings, container=container)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=BASE) as http_client:
        yield http_client


def surge_readings():
    return HazardScenarioSimulator(scenario="surge", seed=21).collect_at(utc_now())


class _InlineSource:
    """最小 DataSource：把固定读数当作一轮采集结果。"""

    name = "api_smoke"

    def __init__(self, readings: list) -> None:
        self._readings = readings

    async def collect(self) -> list:
        return self._readings


class TestOpsEndpoints:
    async def test_healthz_public(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/healthz")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert body["contract"] == "agent_message.v1"

    async def test_readyz_reports_state(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/readyz")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ready"
        assert body["bus"] == "memory"
        assert "store" in body

    async def test_readyz_503_when_bus_down(self, settings: Settings) -> None:
        ctn = create_container(settings, with_simulator=False)
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(settings, container=ctn)), base_url=BASE)
        async with client:
            response = await client.get("/readyz")
        assert response.status_code == 503

    async def test_prometheus_metrics(self, client: httpx.AsyncClient, container: PlatformContainer) -> None:
        container.ingest.add_source(_InlineSource(surge_readings()))
        await container.ingest.ingest_once()
        response = await client.get("/metrics")
        assert response.status_code == 200
        assert "aegis_gateway_events_total" in response.text
        assert "aegis_latency_ms" in response.text


class TestTelemetryEndpoints:
    async def test_empty_query(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/api/v1/telemetry")
        assert response.status_code == 200
        assert response.json() == {"count": 0, "items": []}

    async def test_query_after_ingest(self, client: httpx.AsyncClient, container: PlatformContainer) -> None:
        readings = surge_readings()
        await container.store.telemetry.add(readings)
        response = await client.get("/api/v1/telemetry", params={"region_code": "540121", "limit": 50})
        body = response.json()
        assert response.status_code == 200
        assert body["count"] == len([r for r in readings if r.region_code == "540121"])
        assert {item["region_code"] for item in body["items"]} == {"540121"}

    async def test_limit_bounds_rejected(self, client: httpx.AsyncClient) -> None:
        assert (await client.get("/api/v1/telemetry", params={"limit": 0})).status_code == 422
        assert (await client.get("/api/v1/telemetry", params={"limit": 6000})).status_code == 422


class TestDrillEndpoint:
    async def test_surge_drill_produces_warning(self, client: httpx.AsyncClient, container: PlatformContainer) -> None:
        container.simulator.scenario = "surge"
        response = await client.post("/api/v1/drill/run", json={"scenario": "surge", "ticks": 1})
        assert response.status_code == 200
        body = response.json()
        assert body["ingest"]["readings"] > 0
        assert body["regions"] >= 1
        assert any(chain["acted"] for chain in body["chains"]), body["chains"]

    async def test_normal_drill_does_not_alert(self, client: httpx.AsyncClient, container: PlatformContainer) -> None:
        container.simulator.scenario = "normal"
        await client.post("/api/v1/drill/run", json={"scenario": "normal", "ticks": 1})
        response = await client.get("/api/v1/warnings")
        assert response.json()["count"] == 0

    async def test_invalid_scenario_rejected(self, client: httpx.AsyncClient) -> None:
        response = await client.post("/api/v1/drill/run", json={"scenario": "typhoon"})
        assert response.status_code == 422

    async def test_invalid_region_code_rejected(self, client: httpx.AsyncClient) -> None:
        response = await client.post("/api/v1/drill/run", json={"scenario": "surge", "region_code": "abc"})
        assert response.status_code == 422

    async def test_ticks_bounds(self, client: httpx.AsyncClient) -> None:
        assert (await client.post("/api/v1/drill/run", json={"scenario": "surge", "ticks": 0})).status_code == 422
        assert (await client.post("/api/v1/drill/run", json={"scenario": "surge", "ticks": 51})).status_code == 422


class TestWarningAndTaskEndpoints:
    async def test_warning_listing_and_detail(self, client: httpx.AsyncClient, container: PlatformContainer) -> None:
        container.simulator.scenario = "surge"
        await client.post("/api/v1/drill/run", json={"scenario": "surge", "ticks": 1})

        listing = (await client.get("/api/v1/warnings", params={"limit": 20})).json()
        assert listing["count"] >= 1
        warning_id = listing["items"][0]["warning_id"]

        detail = await client.get(f"/api/v1/warnings/{warning_id}")
        assert detail.status_code == 200
        assert detail.json()["warning_id"] == warning_id
        assert detail.json()["channels"]

    async def test_warning_filter_by_region(self, client: httpx.AsyncClient, container: PlatformContainer) -> None:
        container.simulator.scenario = "surge"
        await client.post("/api/v1/drill/run", json={"scenario": "surge", "ticks": 1})
        listing = (await client.get("/api/v1/warnings", params={"region_code": "540121"})).json()
        assert all("540121" in item["region_codes"] for item in listing["items"])

    async def test_task_detail_and_404(self, client: httpx.AsyncClient, container: PlatformContainer) -> None:
        container.simulator.scenario = "surge"
        await client.post("/api/v1/drill/run", json={"scenario": "surge", "ticks": 1})
        chains = (await client.get("/api/v1/events")).json()["items"]
        unit_ids = [uid for chain in chains for uid in chain["task_units"]]
        assert unit_ids
        assert (await client.get(f"/api/v1/tasks/{unit_ids[0]}")).status_code == 200
        assert (await client.get("/api/v1/tasks/stu_" + "0" * 16)).status_code == 404

    async def test_warning_404(self, client: httpx.AsyncClient) -> None:
        assert (await client.get("/api/v1/warnings/wrn_doesnotexist")).status_code == 404


class TestAgentEndpoints:
    async def test_agents_empty_by_default(self, client: httpx.AsyncClient) -> None:
        body = (await client.get("/api/v1/agents")).json()
        assert body["online"] == 0
        assert body["items"] == []

    async def test_agents_visible_after_register(self, client: httpx.AsyncClient, container: PlatformContainer) -> None:
        from aegis.agents.mock import start_mock_agents

        agents = await start_mock_agents(container.transport, heartbeat_interval=0.05)
        await asyncio.sleep(0.1)
        await container.transport.idle()
        try:
            body = (await client.get("/api/v1/agents")).json()
            assert body["online"] == 5
            assert {"perceive.mock01", "assess.mock01", "plan.mock01"} <= {a["agent_id"] for a in body["items"]}
        finally:
            for agent in agents:
                await agent.stop()

    async def test_collaboration_rate(self, client: httpx.AsyncClient, container: PlatformContainer) -> None:
        from aegis.agents.mock import start_mock_agents

        agents = await start_mock_agents(container.transport, heartbeat_interval=0.05)
        await asyncio.sleep(0.1)
        try:
            container.simulator.scenario = "surge"
            await client.post("/api/v1/drill/run", json={"scenario": "surge", "ticks": 1})
            body = (await client.get("/api/v1/collaboration")).json()
            assert body["success_rate"] in (1.0, None) or body["success_rate"] >= 0.9
            assert isinstance(body["transactions"], list)
        finally:
            for agent in agents:
                await agent.stop()

    async def test_latency_report_structure(self, client: httpx.AsyncClient, container: PlatformContainer) -> None:
        container.simulator.scenario = "surge"
        await client.post("/api/v1/drill/run", json={"scenario": "surge", "ticks": 1})
        body = (await client.get("/api/v1/metrics/latency")).json()
        assert "sla_thresholds" in body and "metrics" in body and "collaboration" in body
        assert body["violations"] == {}


class TestStreamEndpointContract:
    async def test_stream_route_is_registered(self, client: httpx.AsyncClient) -> None:
        """流式行为由 tests/api/test_sse_live.py 用真实服务器验证；
        此处只固定路由存在性与响应类型，避免 ASGITransport 缓冲导致的假失败。"""
        response = await client.get("/openapi.json")
        assert response.status_code == 200
        assert "/api/v1/events/stream" in response.json()["paths"]


class TestConcurrentRequests:
    async def test_parallel_reads_are_isolated(self, client: httpx.AsyncClient, container: PlatformContainer) -> None:
        readings = surge_readings()
        await container.store.telemetry.add(readings)
        responses = await asyncio.gather(*(client.get("/api/v1/telemetry", params={"limit": 10}) for _ in range(30)))
        assert all(r.status_code == 200 for r in responses)
        counts = {len(r.json()["items"]) for r in responses}
        assert len(counts) == 1, "并发读取返回了不一致的结果"

    async def test_parallel_drills_serialise_safely(self, client: httpx.AsyncClient, container: PlatformContainer) -> None:
        container.simulator.scenario = "surge"
        responses = await asyncio.gather(*(client.post("/api/v1/drill/run", json={"scenario": "surge", "ticks": 1}) for _ in range(6)))
        assert all(r.status_code == 200 for r in responses)
        listing = (await client.get("/api/v1/warnings", params={"limit": 500})).json()
        assert len({item["warning_id"] for item in listing["items"]}) == listing["count"]
