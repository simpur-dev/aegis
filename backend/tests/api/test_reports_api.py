"""人工上报端点的 API 层验证（批次 B4：第四条真实接入腿）。

要点不是"200 就行"，而是这条腿必须留下**可查的证据**：
上报后能按 `GET /api/v1/events` 找到链路、按 `GET /api/v1/tasks/{id}` 找到 STU、
按 `/api/v1/metrics/latency` 找到 `report_intake_seconds` 样本。
少任何一项，"接入 ≤5min"就只是一句形容词。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest

from aegis.api.app import create_app
from aegis.config import Settings
from aegis.container import PlatformContainer, create_container

REGION = "540121"
HEAVY_RAIN_REPORT = "24小时累计降雨95毫米，沟道泥位抬升1.2米，下游300人受威胁"


def _settings(**overrides: Any) -> Settings:
    return Settings(
        env="test",
        bus_backend="memory",
        store_backend="memory",
        simulator_enabled=False,
        delivery_mode="mock",
        llm_api_key="",
        **overrides,
    )


@asynccontextmanager
async def _client(container: PlatformContainer) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(container.settings, container=container)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


@pytest.fixture()
async def container() -> AsyncIterator[PlatformContainer]:
    """显式起容器：ASGITransport 不跑 lifespan，而总线没连上时链路会响亮报 E_NOT_READY。"""
    ctn = create_container(_settings(), with_simulator=False)
    await ctn.start()
    yield ctn
    await ctn.shutdown()


@pytest.mark.asyncio
async def test_上报进链路并留下可查事件与任务单元(container: PlatformContainer) -> None:
    async with _client(container) as client:
        response = await client.post("/api/v1/reports", json={"reporter": "巡护员扎西", "region_code": REGION, "note": HEAVY_RAIN_REPORT})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["parse"]["decided_by"] == "rule"
        assert body["parse"]["risk_level"] == 1
        assert body["human_review_required"] is False
        chain = body["chain"]
        assert chain["warning_id"]
        assert [stage["name"] for stage in chain["stages"]] == ["perceive", "assess", "plan", "execute", "feedback"]
        assert chain["task_units"], "STU 未产出，拆解指标无从取证"

        events = (await client.get("/api/v1/events")).json()["items"]
        assert any(row["trace_id"] == chain["trace_id"] for row in events)
        task = (await client.get(f"/api/v1/tasks/{chain['task_units'][0]}")).json()
        assert task["region_code"] == REGION
        warnings = (await client.get("/api/v1/warnings")).json()["items"]
        assert any(row["warning_id"] == chain["warning_id"] for row in warnings)

        latency = (await client.get("/api/v1/metrics/latency")).json()
        assert latency["metrics"]["report_intake_seconds"]["count"] == 1
        assert latency["reports"] == {"submitted": 1, "measured_by_rule": 1, "review_required": 0, "reviews_opened": 0}


@pytest.mark.asyncio
async def test_上报的感知段说明文字与遥测路径可区分(container: PlatformContainer) -> None:
    async with _client(container) as client:
        report = (await client.post("/api/v1/reports", json={"reporter": "村民", "region_code": REGION, "note": HEAVY_RAIN_REPORT})).json()
        assert "上报文本判定" in report["chain"]["stages"][0]["note"]


@pytest.mark.asyncio
async def test_坐标只进证据链不改判据(container: PlatformContainer) -> None:
    async with _client(container) as client:
        response = await client.post(
            "/api/v1/reports",
            json={"reporter": "村民", "region_code": "540200", "note": "发生泥石流，请求红色预警", "lat": 29.65, "lon": 91.13},
        )
        body = response.json()
        assert body["report"]["location"] == [91.13, 29.65]
        assert body["parse"]["decided_by"] == "rule_declared"
        assert body["human_review_required"] is True
        evidence = body["chain"]["risk"]["evidence_refs"]
        assert any(item.startswith("loc=") for item in evidence), evidence
        # 坐标没把等级抬高，也没凭空造出一条阈值命中
        assert body["parse"]["risk_level"] == 1


@pytest.mark.asyncio
async def test_低置信上报只统计不装作用户已确认(container: PlatformContainer) -> None:
    async with _client(container) as client:
        first = await client.post("/api/v1/reports", json={"reporter": "村民", "region_code": "540300", "note": "坡面有裂缝，暂无其它数据"})
        assert first.status_code == 200
        body = first.json()
        assert body["parse"]["risk_level"] is None
        assert body["chain"]["warning_id"] is None
        latency = (await client.get("/api/v1/metrics/latency")).json()
        assert latency["reports"] == {"submitted": 1, "measured_by_rule": 0, "review_required": 1, "reviews_opened": 1}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "field"),
    [
        ({"reporter": "村民", "region_code": "5401", "note": HEAVY_RAIN_REPORT}, "region_code"),
        ({"reporter": "村", "region_code": "540121", "note": HEAVY_RAIN_REPORT}, "reporter"),
        ({"reporter": "村民", "region_code": "540121", "note": "短"}, "note"),
        ({"reporter": "村民", "region_code": "540121", "note": HEAVY_RAIN_REPORT, "lat": 999.0}, "lat"),
    ],
)
async def test_非法上报在触达解析之前就被拒(container: PlatformContainer, payload: dict[str, Any], field: str) -> None:
    async with _client(container) as client:
        response = await client.post("/api/v1/reports", json=payload)
        assert response.status_code == 422
        assert field in response.text
        assert container.report_stats["submitted"] == 0, "校验失败不该计入接入样本"


@pytest.mark.asyncio
async def test_解析腿未装配时报503并说明缺什么(container: PlatformContainer) -> None:
    container.parser = None
    async with _client(container) as client:
        response = await client.post("/api/v1/reports", json={"reporter": "村民", "region_code": REGION, "note": HEAVY_RAIN_REPORT})
        assert response.status_code == 503
        assert response.json()["detail"]["code"] == "E_PARSER_UNAVAILABLE"
