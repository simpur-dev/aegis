"""审计读面的判据（P4，设计 §14.3）。

钉四件事：
1. **`duty` 只能读自己的**：`operator_id` 被强制改写成请求者本人（传别人的也不生效）；
2. **`lead` / `admin` 读全量**；
3. **过滤维度真的生效**（operator/action/phase/action_id/时间）；
4. **读面不写审计**：查一次账不该多一行账。
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
from aegis.storage.store import AuditRecord


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
        Operator(operator_id="op-duty2", display_name="值班员二", role="duty"),
    ):
        await ledger.upsert_operator(operator)
    ctn.operators = OperatorService(ledger)

    # 种三行审计：两个人的动作、两个阶段
    store = ctn.store
    await store.record_audit(
        AuditRecord(
            phase="intent",
            action_id="act_aaaa000000001",
            operator_id="op-duty",
            role="duty",
            action="warning.publish",
            target="wrn_1",
            utterance="把这条发出去",
        )
    )
    await store.record_audit(
        AuditRecord(
            phase="result",
            action_id="act_aaaa000000001",
            operator_id="op-duty",
            role="duty",
            action="warning.publish",
            after={"executed": True},
        )
    )
    await store.record_audit(
        AuditRecord(
            phase="intent",
            action_id="act_bbbb000000002",
            operator_id="op-lead",
            role="lead",
            action="rule.update",
            target="rule_1",
        )
    )
    yield ctn
    await ctn.shutdown()


def _headers(operator_id: str) -> dict[str, str]:
    return {"X-Aegis-Operator": operator_id}


@pytest.mark.asyncio
async def test_duty_sees_only_own_rows(container: PlatformContainer) -> None:
    """`duty` 传别人的 operator_id 不生效——这个参数不能是越权开关。"""
    async with _client(container) as client:
        own = await client.get("/api/v1/audit/ops", headers=_headers("op-duty"))
        spoofed = await client.get("/api/v1/audit/ops", params={"operator_id": "op-lead"}, headers=_headers("op-duty"))

    assert own.status_code == 200
    assert {row["operator_id"] for row in own.json()["items"]} == {"op-duty"}
    assert spoofed.json()["scope"] == "op-duty", "duty 的范围被强制收成自己"
    assert {row["operator_id"] for row in spoofed.json()["items"]} == {"op-duty"}


@pytest.mark.asyncio
async def test_lead_sees_all_and_can_filter_by_operator(container: PlatformContainer) -> None:
    async with _client(container) as client:
        everything = await client.get("/api/v1/audit/ops", headers=_headers("op-lead"))
        only_lead = await client.get("/api/v1/audit/ops", params={"operator_id": "op-lead"}, headers=_headers("op-lead"))
    assert everything.json()["count"] == 3
    assert only_lead.json()["count"] == 1
    assert only_lead.json()["items"][0]["action"] == "rule.update"


@pytest.mark.asyncio
async def test_filters_by_phase_action_and_action_id(container: PlatformContainer) -> None:
    async with _client(container) as client:
        intents = await client.get("/api/v1/audit/ops", params={"phase": "intent"}, headers=_headers("op-lead"))
        by_action = await client.get("/api/v1/audit/ops", params={"action": "warning.publish"}, headers=_headers("op-lead"))
        by_id = await client.get("/api/v1/audit/ops", params={"action_id": "act_aaaa000000001"}, headers=_headers("op-lead"))
    assert intents.json()["count"] == 2
    assert by_action.json()["count"] == 2
    assert by_id.json()["count"] == 2, "同一动作的 intent 与 result 两条都要能一起查出来"


@pytest.mark.asyncio
async def test_newest_first_and_pagination(container: PlatformContainer) -> None:
    async with _client(container) as client:
        first = await client.get("/api/v1/audit/ops", params={"limit": 2}, headers=_headers("op-lead"))
        second = await client.get("/api/v1/audit/ops", params={"limit": 2, "offset": 2}, headers=_headers("op-lead"))
    ids_first = [row["id"] for row in first.json()["items"]]
    ids_second = [row["id"] for row in second.json()["items"]]
    assert ids_first == sorted(ids_first, reverse=True), "最近在前"
    assert set(ids_first).isdisjoint(ids_second)


@pytest.mark.asyncio
async def test_reading_does_not_write_audit(container: PlatformContainer) -> None:
    """读接口不写审计：查一次账不该多一行账（§4.9）。"""
    before = len(await container.store.read_audit(limit=200))
    async with _client(container) as client:
        await client.get("/api/v1/audit/ops", headers=_headers("op-lead"))
        await client.get("/api/v1/audit/ops", headers=_headers("op-lead"))
    after = len(await container.store.read_audit(limit=200))
    assert after == before


@pytest.mark.asyncio
async def test_requires_seat_header(container: PlatformContainer) -> None:
    async with _client(container) as client:
        response = await client.get("/api/v1/audit/ops")
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "E_OPERATOR_REQUIRED"
