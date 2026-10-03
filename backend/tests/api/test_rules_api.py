"""/api/v1/rules 与版本面的门禁（批次 C2 的对外证据）。

指标 1 说的"识别 ≥5 类触发条件"要能在 HTTP 面上被第三方量出来：
条数、灾种覆盖、触发条件种类数、版本出处、以及"哪些还没标定"。
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


@pytest.fixture()
def settings() -> Settings:
    return Settings(env="test", bus_backend="memory", store_backend="memory", simulator_enabled=False, delivery_mode="mock", llm_api_key="")


@asynccontextmanager
async def _client(container: PlatformContainer) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(container.settings, container=container)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        yield client


@pytest.mark.asyncio
async def test_生效规则集外显条数灾种与出处(settings: Settings) -> None:
    container = create_container(settings, with_simulator=False)
    async with _client(container) as client:
        body = (await client.get("/api/v1/rules")).json()
        assert body["provenance"]["source"] == "builtin"
        assert body["provenance"]["rules"] == len(body["items"]) == 9
        assert len(body["hazards_covered"]) >= 5
        assert body["trigger_condition_kinds"] >= 5, body["trigger_condition_kinds"]
        assert body["load_error"] is None
        assert all(row["conditions"] for row in body["items"])
        assert all("未经现场标定" in row["calibration_basis"] for row in body["items"]), "未标定状态必须随规则一起外显"


@pytest.mark.asyncio
async def test_指标出口带着同一份规则出处(settings: Settings) -> None:
    """`/api/v1/metrics/latency` 与 `/api/v1/rules` 说不了两版阈值：两处读的是同一个引擎快照。"""
    container = create_container(settings, with_simulator=False)
    async with _client(container) as client:
        rules = (await client.get("/api/v1/rules")).json()["provenance"]
        latency = (await client.get("/api/v1/metrics/latency")).json()["rulebook"]
        assert latency["versions"] == rules["versions"]
        assert latency["error"] is None


@pytest.mark.asyncio
async def test_换版立即反映到对外面(settings: Settings) -> None:
    from dataclasses import replace

    container = create_container(settings, with_simulator=False)
    engine = container.rule_engine
    engine.reload([replace(rule, version=7) for rule in engine.rules], source="postgres")
    async with _client(container) as client:
        body = (await client.get("/api/v1/rules")).json()
        assert body["provenance"]["source"] == "postgres"
        assert all(row["version"] == 7 for row in body["items"])


@pytest.mark.asyncio
async def test_版本面只在有规则库的形态存在(settings: Settings) -> None:
    """内存读视图没有版本化阈值这件事：如实 503 并把补齐条件写清楚。"""
    container = create_container(settings, with_simulator=False)
    async with _client(container) as client:
        response = await client.get("/api/v1/rules/versions")
        assert response.status_code == 503
        detail = response.json()["detail"]
        assert detail["code"] == "E_RULEBOOK_UNAVAILABLE"
        assert "postgres" in detail["requires"]

        assert (await client.get("/api/v1/rules/versions", params={"status": "nope"})).status_code == 422


@pytest.mark.asyncio
async def test_规则库读取失败时沿用种子并留下事实(settings: Settings) -> None:
    class BrokenStore:
        def __init__(self, inner: Any) -> None:
            self._inner = inner

        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)

        async def trigger_rules(self, **_kwargs: Any) -> list[dict[str, Any]]:
            raise RuntimeError("库不可达")

    container = create_container(settings, with_simulator=False)
    container.store = BrokenStore(container.store)  # type: ignore[assignment]
    await container._load_rulebook()
    assert container.rulebook_error and "库不可达" in container.rulebook_error
    assert container.rule_engine.source == "builtin", "读失败必须留在上一版，而不是空集"


@pytest.mark.asyncio
async def test_标定报表把命中实测与精度未测得分开说(settings: Settings) -> None:
    """/api/v1/rules/calibration 的两半：命中次数是台账实测，误报精度在缺真值时必须留空。"""
    container = create_container(settings, with_simulator=False)
    await container.start()
    try:
        await container.submit_report(
            note="24小时累计降雨95毫米，沟道泥位抬升1.2米",
            region_code="540121",
            reporter="巡护员",
        )
        async with _client(container) as client:
            body = (await client.get("/api/v1/rules/calibration")).json()
            assert body["status"] == "not_measured"
            assert body["auto_applied"] is False
            assert body["ledger_rows"] >= 1
            fired = {row["rule_id"]: row for row in body["advice"]}
            assert fired["R-DEBRIS-RAIN-2"]["hits"] == 1
            assert all(row["precision"] is None for row in body["advice"])
            assert "E1" in body["note"]
            assert body["report_rulebook"]["source"] == "builtin"
            assert body["off_bookrule_hits"] == {}, "阈值命中的上报不该被算成书外规则"
    finally:
        await container.shutdown()
