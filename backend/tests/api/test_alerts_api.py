"""告警 API：列表与三种处置的权限与状态语义。

内存容器的告警台账可用（`PlatformStore` 实现了 `SupportsAlerts`），但重启即失——
`persistent=false` 会出现在响应里，前端据此标注"内存形态"。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest

from aegis.api.app import create_app
from aegis.config import Settings
from aegis.container import PlatformContainer, create_container
from aegis.domain.operator import Operator
from aegis.services.operators import OperatorService


class _FakeLedger:
    def __init__(self) -> None:
        self.rows: dict[str, Operator] = {}

    async def upsert_operator(self, operator: Operator) -> Operator:
        self.rows[operator.operator_id] = operator
        return operator

    async def create_operator(self, operator: Operator) -> Operator | None:
        if operator.operator_id in self.rows:
            return None
        self.rows[operator.operator_id] = operator
        return operator

    async def get_operator(self, operator_id: str) -> Operator | None:
        return self.rows.get(operator_id)

    async def list_operators(self, *, active_only: bool = True, limit: int = 200) -> list[Operator]:
        rows = [op for op in self.rows.values() if not active_only or op.active]
        return sorted(rows, key=lambda op: op.operator_id)[:limit]


def _settings() -> Settings:
    return Settings(
        env="test",
        bus_backend="memory",
        store_backend="memory",
        simulator_enabled=False,
        delivery_mode="mock",
        llm_api_key="",
    )


@asynccontextmanager
async def _client(container: PlatformContainer) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(container.settings, container=container)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        yield client


def _headers(operator_id: str) -> dict[str, str]:
    return {"X-Aegis-Operator": operator_id}


async def _raise(ctn: PlatformContainer, *, warning_id: str, level: int, drill: bool) -> str:
    assert ctn.alerts is not None
    record, created = await ctn.alerts.raise_from_broadcast(
        {"warning_id": warning_id, "risk_level": level, "regions": ["540121"], "title": "演练注入", "drill": drill}
    )
    assert created and record is not None
    return record["alert_id"]


@pytest.fixture()
async def container() -> AsyncIterator[PlatformContainer]:
    ctn = create_container(_settings(), with_simulator=False)
    await ctn.start()
    ledger = _FakeLedger()
    for operator in (
        Operator(operator_id="op-lead", display_name="值班长", role="lead"),
        Operator(operator_id="op-duty", display_name="值班员", role="duty"),
    ):
        await ledger.upsert_operator(operator)
    ctn.operators = OperatorService(ledger)
    yield ctn
    await ctn.shutdown()


@pytest.mark.asyncio
async def test_列表默认只给在桌上的并标注形态(container: PlatformContainer) -> None:
    await _raise(container, warning_id="wrn_" + "1" * 20, level=2, drill=False)
    await _raise(container, warning_id="wrn_" + "2" * 20, level=1, drill=True)
    async with _client(container) as client:
        response = await client.get("/api/v1/alerts")
    body = response.json()
    assert response.status_code == 200
    assert body["count"] == 2 and body["persistent"] is False
    # level 升序：最严重的排前面（1 是 RED）
    assert [row["level"] for row in body["items"]] == [1, 2]
    assert body["items"][0]["drill"] is True


@pytest.mark.asyncio
async def test_接手需要席位_重复确认幂等(container: PlatformContainer) -> None:
    alert_id = await _raise(container, warning_id="wrn_" + "3" * 20, level=1, drill=False)
    async with _client(container) as client:
        no_header = await client.post(f"/api/v1/alerts/{alert_id}/ack")
        first = await client.post(f"/api/v1/alerts/{alert_id}/ack", headers=_headers("op-duty"))
        again = await client.post(f"/api/v1/alerts/{alert_id}/ack", headers=_headers("op-duty"))
        history = await client.get("/api/v1/alerts?status=acked")
    assert no_header.status_code == 401
    assert no_header.json()["detail"]["code"] == "E_OPERATOR_REQUIRED"
    assert first.status_code == 200 and first.json()["already"] is False
    assert again.status_code == 200 and again.json()["already"] is True
    assert history.json()["count"] == 1
    assert history.json()["items"][0]["ack_by"] == "op-duty"


@pytest.mark.asyncio
async def test_转派需要值班长_受理人必须真实存在(container: PlatformContainer) -> None:
    alert_id = await _raise(container, warning_id="wrn_" + "4" * 20, level=1, drill=False)
    async with _client(container) as client:
        as_duty = await client.post(f"/api/v1/alerts/{alert_id}/transfer", json={"assignee": "op-duty"}, headers=_headers("op-duty"))
        as_lead = await client.post(f"/api/v1/alerts/{alert_id}/transfer", json={"assignee": "op-duty"}, headers=_headers("op-lead"))
        ghost = await client.post(f"/api/v1/alerts/{alert_id}/transfer", json={"assignee": "op-ghost"}, headers=_headers("op-lead"))
        listed = await client.get("/api/v1/alerts")
    assert as_duty.status_code == 403
    assert as_duty.json()["detail"]["code"] == "E_OPERATOR_FORBIDDEN"
    assert as_lead.status_code == 200
    assert as_lead.json()["alert"]["assigned_to"] == "op-duty"
    assert ghost.status_code == 422
    assert ghost.json()["detail"]["code"] == "E_OPERATOR_INVALID"
    assert listed.json()["items"][0]["assigned_to"] == "op-duty"


@pytest.mark.asyncio
async def test_稍后保留状态_告警仍在桌上(container: PlatformContainer) -> None:
    alert_id = await _raise(container, warning_id="wrn_" + "5" * 20, level=3, drill=False)
    async with _client(container) as client:
        response = await client.post(f"/api/v1/alerts/{alert_id}/snooze", json={"minutes": 10}, headers=_headers("op-duty"))
        listed = await client.get("/api/v1/alerts")
    assert response.status_code == 200
    assert response.json()["alert"]["status"] == "raised"
    assert response.json()["alert"]["snooze_until"] is not None
    assert listed.json()["count"] == 1


@pytest.mark.asyncio
async def test_不存在的告警_404_终态_409(container: PlatformContainer) -> None:
    async with _client(container) as client:
        missing = await client.post("/api/v1/alerts/alt_00000000ffffffff/ack", headers=_headers("op-duty"))
        alert_id = await _raise(container, warning_id="wrn_" + "6" * 20, level=1, drill=False)
        # 白盒拨表：内存台账行直接置为终态（SQL 侧由 CHECK 约束 + WHERE 保证同一行为）
        assert container.store.alerts._rows[alert_id]["status"] == "raised"
        container.store.alerts._rows[alert_id]["status"] = "closed"
        closed = await client.post(f"/api/v1/alerts/{alert_id}/ack", headers=_headers("op-duty"))
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "E_ALERT_NOT_FOUND"
    assert closed.status_code == 409
    assert closed.json()["detail"]["code"] == "E_ALERT_STATE_CONFLICT"
    assert closed.json()["detail"]["status"] == "closed"
