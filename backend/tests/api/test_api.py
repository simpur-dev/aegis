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
    ctn = create_container(settings, with_simulator=True)  # 带模拟场站，供 /drill 端点使用（夹具的配置里模拟器是关的）
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

    async def test_model_cards_endpoint_reports_real_config_without_fabricated_scores(
        self, client: httpx.AsyncClient, container: PlatformContainer
    ) -> None:
        """§5 模型卡片出口：真实路由/prompt 版本 + 未训练未评测如实 None，不冒充达标。"""
        response = await client.get("/api/v1/agents/model-cards")
        assert response.status_code == 200
        body = response.json()
        assert body["measured_columns"] == []
        cards = {c["agent"]: c for c in body["cards"]}
        assert cards["assess"]["model"] == "qwen-plus"
        assert cards["assess"]["prompt_version"] == "1.1.0"  # 灾种枚举进正文时换的号  # 已迁入版本化真源
        assert cards["execute"]["prompt_version"] == "1.0.0"  # 三分支文案已迁入
        assert cards["perceive"]["prompt_version"] is None  # 未迁入的仍如实留白
        for card in body["cards"]:
            assert card["trained_dataset"] is None and card["eval_score"] is None and card["measured"] is False

    async def test_simulation_run_is_deterministic_and_shape_correct(self, client: httpx.AsyncClient) -> None:
        body = {"rain_base_mm": 60, "rain_spread_mm": 20, "scenarios": 6, "rounds": 4, "playbook": "转移", "seed": 42}
        first = await client.post("/api/v1/simulation/run", json=body)
        second = await client.post("/api/v1/simulation/run", json=body)
        assert first.status_code == 200 and second.status_code == 200
        a, b = first.json(), second.json()
        assert a == b, "同参同种子必须逐字节可复现（§3.6 数字冻结自遥测、确定性）"
        assert a["mode"] == "demo_sandbox"
        assert {"probability_matrix", "timeline", "playbook_comparison"} <= set(a)
        assert {row["playbook"] for row in a["playbook_comparison"]} == {"转移", "封路", "不行动"}

    async def test_simulation_cost_cap_downscales_scenarios(self, client: httpx.AsyncClient) -> None:
        body = {"scenarios": 64, "rounds": 24, "token_budget": 100, "playbook": "转移", "seed": 7}
        data = (await client.post("/api/v1/simulation/run", json=body)).json()
        assert data["cost"]["downscaled"] is True
        assert 1 <= data["cost"]["effective_scenarios"] < 64

    async def test_simulation_rejects_unknown_playbook(self, client: httpx.AsyncClient) -> None:
        assert (await client.post("/api/v1/simulation/run", json={"playbook": "躺平"})).status_code == 422

    async def test_data_api_open_when_service_token_unset(self, client: httpx.AsyncClient) -> None:
        # 默认 service_token="" → 数据 API 不校验（向后兼容内部装配/演练/前端调用）。
        assert (await client.get("/api/v1/telemetry")).status_code == 200

    async def test_data_api_requires_service_token_when_configured(self) -> None:
        cfg = Settings(
            env="test", bus_backend="memory", store_backend="memory", simulator_enabled=False, delivery_mode="mock", service_token="s3cr3t"
        )
        ctn = create_container(cfg, with_simulator=False)
        await ctn.start()
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(cfg, container=ctn)), base_url=BASE) as c:
                assert (await c.get("/api/v1/telemetry")).status_code == 401
                assert (await c.get("/api/v1/telemetry", headers={"X-Aegis-Token": "wrong"})).status_code == 401
                assert (await c.get("/api/v1/telemetry", headers={"X-Aegis-Token": "s3cr3t"})).status_code == 200
        finally:
            await ctn.shutdown()

    @pytest.mark.parametrize(
        ("path", "expect"),
        [
            ("/api/v1/cases/recall?q=泥石流", 401),
            ("/api/v1/retrieval/search?q=泥石流", 401),
            ("/api/v1/memory/shared", 401),
        ],
    )
    async def test_智能体真在用的读端点也在这个鉴权面里(self, path: str, expect: int) -> None:
        """§4.8.1：`RestAssessContext` 逐条请求的就是这几个路径。

        漏一个的表现是"配了令牌"与"真的在拦"脱钩——客户端认真带 Bearer、服务端认真不校验，
        而同一份数据经 MCP 工具面取时反倒被拦（`app.py` 的 MCP 闸共用 `_presented_token`）。
        两个面同一口径，才是这一节原本要的东西。
        """
        cfg = Settings(
            env="test", bus_backend="memory", store_backend="memory", simulator_enabled=False, delivery_mode="mock", service_token="s3cr3t"
        )
        ctn = create_container(cfg, with_simulator=False)
        await ctn.start()
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(cfg, container=ctn)), base_url=BASE) as c:
                assert (await c.get(path)).status_code == expect, "没带凭据必须被拒"
                assert (await c.get(path, headers={"X-Aegis-Token": "wrong"})).status_code == expect
                for header in ({"X-Aegis-Token": "s3cr3t"}, {"authorization": "Bearer s3cr3t"}):
                    # 带对凭据后不要求 200：这台没开检索，放行后是 503（腿没装配），那是一行事实不是鉴权失败
                    assert (await c.get(path, headers=header)).status_code != expect, f"{header} 应当通过鉴权"
        finally:
            await ctn.shutdown()

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
        """过滤要有正反两判：旧写法只有一句 `all(...)`，端点返回空列表时照样绿。"""
        container.simulator.scenario = "surge"
        await client.post("/api/v1/drill/run", json={"scenario": "surge", "ticks": 1})
        listing = (await client.get("/api/v1/warnings", params={"region_code": "540121"})).json()
        assert listing["items"], "surge 演练之后 540121 一条预警都没有：这条过滤没在过滤任何东西"
        assert all("540121" in item["region_codes"] for item in listing["items"])
        other = (await client.get("/api/v1/warnings", params={"region_code": "000000"})).json()
        assert other["items"] == [], f"换了区号还返回同一批：region_code 根本没参与筛选 {[i['region_codes'] for i in other['items']]}"

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
            # 旧写法 `success_rate in (1.0, None) or >= 0.9` 让"一笔事务都没有"（None）直接达标。
            assert body["transactions"], "跑了 surge 却没留下任何协同事务：成功率那句没有底子"
            assert body["success_rate"] is not None and body["success_rate"] >= 0.9, body["success_rate"]
            first = body["transactions"][0]
            assert {"trace_id", "outcome", "latency_ms"} <= set(first), f"事务形状不对，报表侧读不到东西：{sorted(first)}"
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


class TestChainAuditEndpoint:
    """`GET /api/v1/audit/chain/{trace_id}`：台账直读面的三档回答（200/404-空/503）与鉴权。"""

    async def test_内存形态回503并说清为什么(self, client: httpx.AsyncClient) -> None:
        """内存后端那份台账随进程走，回一份空清单会被读成"这条 trace 没发生过"。"""
        response = await client.get(f"/api/v1/audit/chain/trc_{'0' * 16}")
        assert response.status_code == 503
        assert response.json()["detail"]["code"] == "E_AUDIT_UNAVAILABLE"

    async def test_trace形状不对当场422(self, client: httpx.AsyncClient) -> None:
        assert (await client.get("/api/v1/audit/chain/trc_短")).status_code == 422

    async def test_审计面也在数据API同一个鉴权面上(self) -> None:
        cfg = Settings(
            env="test", bus_backend="memory", store_backend="memory", simulator_enabled=False, delivery_mode="mock", service_token="s3cr3t"
        )
        ctn = create_container(cfg, with_simulator=False)
        await ctn.start()
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(cfg, container=ctn)), base_url=BASE) as c:
                url = f"/api/v1/audit/chain/trc_{'0' * 16}"
                assert (await c.get(url)).status_code == 401, "审计行里有模型/Prompt 版本与结论，裸放着就是把 D10 的凭证泄露面开大"
                assert (await c.get(url, headers={"X-Aegis-Token": "s3cr3t"})).status_code == 503
        finally:
            await ctn.shutdown()
