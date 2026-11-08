"""站内通知读面：断线对账与"标为已读"。

内存容器没有 operators 面（与 `test_operators_api.py` 同一口径，端点如实 503），
所以这里注入最小 fake ledger；通知本身走容器里真实的 `Notifier` + 内存台账，不复制业务规则。
写入不开放 HTTP POST——通知只能由助手 `notify.lead/group` 产生，否则"谁发的"会有第二个答案。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest

from aegis.api.app import create_app
from aegis.config import Settings
from aegis.container import PlatformContainer, create_container
from aegis.domain.operator import Operator, OperatorRole
from aegis.services.alerting.notifier import Notifier
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
        Operator(operator_id="op-lead", display_name="值班长", role=OperatorRole.LEAD),
        Operator(operator_id="op-duty", display_name="值班员", role=OperatorRole.DUTY),
    ):
        await ledger.upsert_operator(operator)
    ctn.operators = OperatorService(ledger)
    assert ctn.notifier is not None and ctn.notifier.available
    await ctn.notifier.notify(recipient="op-duty", message="复核通麦镇预警", kind="lead", sender="op-lead")
    yield ctn
    await ctn.shutdown()


def _headers(operator_id: str) -> dict[str, str]:
    return {"X-Aegis-Operator": operator_id}


@pytest.mark.asyncio
async def test_对账只返回声明席位自己的未读通知(container: PlatformContainer) -> None:
    async with _client(container) as client:
        mine = await client.get("/api/v1/notifications", headers=_headers("op-duty"))
        other = await client.get("/api/v1/notifications", headers=_headers("op-lead"))
    assert mine.status_code == 200
    assert mine.json()["count"] == 1
    assert mine.json()["items"][0]["message"] == "复核通麦镇预警"
    assert other.json() == {"items": [], "count": 0}, "不能读到别人的通知"


@pytest.mark.asyncio
async def test_读面没有席位头或席位无效时明确拒绝(container: PlatformContainer) -> None:
    async with _client(container) as client:
        missing = await client.get("/api/v1/notifications")
        invalid = await client.get("/api/v1/notifications", headers=_headers("op-ghost"))
    assert missing.status_code == 401
    assert invalid.status_code == 403


@pytest.mark.asyncio
async def test_标已读只影响自己的通知且重复标记报404(container: PlatformContainer) -> None:
    async with _client(container) as client:
        listed = (await client.get("/api/v1/notifications", headers=_headers("op-duty"))).json()
        notification_id = listed["items"][0]["id"]

        wrong = await client.post(f"/api/v1/notifications/{notification_id}/read", headers=_headers("op-lead"))
        marked = await client.post(f"/api/v1/notifications/{notification_id}/read", headers=_headers("op-duty"))
        again = await client.post(f"/api/v1/notifications/{notification_id}/read", headers=_headers("op-duty"))
        unread = (await client.get("/api/v1/notifications?unread_only=true", headers=_headers("op-duty"))).json()

    assert wrong.status_code == 404, "不能替别人标已读"
    assert marked.status_code == 200 and marked.json()["notification"]["read_at"] is not None
    assert again.status_code == 404
    assert unread["count"] == 0


@pytest.mark.asyncio
async def test_通知台账未装配时如实503而不是空清单(container: PlatformContainer) -> None:
    container.notifier = Notifier(None, publish=None, subject="ops.assistant.notify", operators=container.operators)
    async with _client(container) as client:
        response = await client.get("/api/v1/notifications", headers=_headers("op-duty"))
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "E_NOTIFIER_UNAVAILABLE"


@pytest.mark.asyncio
async def test_没有开放任意POST写入口(container: PlatformContainer) -> None:
    async with _client(container) as client:
        response = await client.post("/api/v1/notifications", json={"recipient": "op-duty"}, headers=_headers("op-duty"))
    assert response.status_code == 405
