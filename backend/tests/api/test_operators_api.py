"""席位 API：三个端点的可区分行为。

内存容器没有 operators 面（503），所以这里注入一个最小 fake ledger。
fake 只实现协议、不复制业务规则；生产实现仍只有 `PostgresStore` 一份。
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


@pytest.fixture()
async def container() -> AsyncIterator[PlatformContainer]:
    ctn = create_container(_settings(), with_simulator=False)
    await ctn.start()
    ledger = _FakeLedger()
    for operator in (
        Operator(operator_id="op-admin", display_name="管理员", role="admin"),
        Operator(operator_id="op-lead", display_name="值班长", role="lead"),
        Operator(operator_id="op-duty", display_name="值班员", role="duty"),
    ):
        await ledger.upsert_operator(operator)
    ctn.operators = OperatorService(ledger)
    yield ctn
    await ctn.shutdown()


def _headers(operator_id: str) -> dict[str, str]:
    return {"X-Aegis-Operator": operator_id}


@pytest.mark.asyncio
async def test_me_without_header_is_empty_but_200(container: PlatformContainer) -> None:
    async with _client(container) as client:
        response = await client.get("/api/v1/operators/me")
    assert response.status_code == 200
    assert response.json() == {"operator_id": None, "display_name": None, "role": None, "active": None}


@pytest.mark.asyncio
async def test_me_returns_seat_facts(container: PlatformContainer) -> None:
    async with _client(container) as client:
        response = await client.get("/api/v1/operators/me", headers=_headers("op-duty"))
    assert response.status_code == 200
    assert response.json()["operator_id"] == "op-duty"
    assert response.json()["role"] == "duty"
    assert response.json()["active"] is True


@pytest.mark.asyncio
async def test_list_requires_lead(container: PlatformContainer) -> None:
    async with _client(container) as client:
        no_header = await client.get("/api/v1/operators")
        as_duty = await client.get("/api/v1/operators", headers=_headers("op-duty"))
        as_lead = await client.get("/api/v1/operators", headers=_headers("op-lead"))
    assert no_header.status_code == 401
    assert no_header.json()["detail"]["code"] == "E_OPERATOR_REQUIRED"
    assert as_duty.status_code == 403
    assert as_duty.json()["detail"]["code"] == "E_OPERATOR_FORBIDDEN"
    assert as_lead.status_code == 200
    assert as_lead.json()["count"] == 3


@pytest.mark.asyncio
async def test_create_requires_admin_and_rejects_duplicate(container: PlatformContainer) -> None:
    payload = {"operator_id": "op-new", "display_name": "新值班员", "role": "duty"}
    async with _client(container) as client:
        as_duty = await client.post("/api/v1/operators", json=payload, headers=_headers("op-duty"))
        created = await client.post("/api/v1/operators", json=payload, headers=_headers("op-admin"))
        duplicate = await client.post("/api/v1/operators", json=payload, headers=_headers("op-admin"))
    assert as_duty.status_code == 403
    assert created.status_code == 201
    assert created.json()["operator_id"] == "op-new"
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"]["code"] == "E_OPERATOR_CONFLICT"


@pytest.mark.asyncio
async def test_inactive_seat_is_out_of_list_and_cannot_list(container: PlatformContainer) -> None:
    payload = {"operator_id": "op-off", "display_name": "离岗", "role": "lead", "active": False}
    async with _client(container) as client:
        await client.post("/api/v1/operators", json=payload, headers=_headers("op-admin"))
        me = await client.get("/api/v1/operators/me", headers=_headers("op-off"))
        listed = await client.get("/api/v1/operators", headers=_headers("op-lead"))
        rejected = await client.get("/api/v1/operators", headers=_headers("op-off"))
    assert me.status_code == 200
    assert me.json()["active"] is False
    assert "op-off" not in {row["operator_id"] for row in listed.json()["items"]}
    assert rejected.status_code == 403
    assert rejected.json()["detail"]["code"] == "E_OPERATOR_INVALID"


@pytest.mark.asyncio
async def test_memory_store_reports_unavailable() -> None:
    """没有持久层时端点必须 503，不能假装"没有席位"（那会被读成"所有人都没权限"）。"""
    ctn = create_container(_settings(), with_simulator=False)
    await ctn.start()
    try:
        async with _client(ctn) as client:
            response = await client.get("/api/v1/operators/me")
        assert response.status_code == 503
        assert response.json()["detail"]["code"] == "E_OPERATOR_UNAVAILABLE"
    finally:
        await ctn.shutdown()
