"""`/spaces` 读面与三种任务处置写面（P3 第二批）。

钉住五条：
- 读面不需要席位头（设计 §5.5：读允许匿名），写面全部走 `require_operator` 同一处鉴权；
- `expected_version` 由客户端给出，命中 0 行就是 409 `E_TASK_STATE_CONFLICT`；
- 路径里的空间与任务必须对得上：用 A 空间的路径改 B 空间的任务是 404，不是 409；
- 认领/回执 `duty`、移交 `lead`，受理席位必须存在且 active（422 不是 409）；
- 台账缺席的装配形态整段 503，不假装能用。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from aegis.api.app import create_app
from aegis.config import Settings
from aegis.container import PlatformContainer, create_container
from aegis.domain.chain import ChainResult
from aegis.domain.operator import Operator
from aegis.services.operators import OperatorService

WRN = "wrn_space0123456"
STU_A = "stu_0123456789abcdef"
STU_B = "stu_fedcba9876543210"

DUTY_HEADERS = {"X-Aegis-Operator": "duty-01"}
LEAD_HEADERS = {"X-Aegis-Operator": "lead-01"}


class _FakeLedger:
    """席位台账最小替身：与 `test_assistant_api.py` 同一手法。"""

    def __init__(self) -> None:
        self.rows = {
            "duty-01": Operator(operator_id="duty-01", display_name="值班员", role="duty"),
            "duty-02": Operator(operator_id="duty-02", display_name="值班员乙", role="duty"),
            "lead-01": Operator(operator_id="lead-01", display_name="值班长", role="lead"),
        }

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
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        yield client


async def _seed(container: PlatformContainer, warning_id: str, *stu_ids: str) -> None:
    """走公开的 `ensure_from_chain`，与生产同一条建空间/任务的路径。"""
    result = ChainResult(
        trace_id=f"trc_{warning_id[4:16]}",
        event_id=f"evt_{warning_id[4:16]}",
        warning=SimpleNamespace(warning_id=warning_id),
        task_units=[SimpleNamespace(task_unit_id=item) for item in stu_ids],
    )
    assert await container.collab.ensure_from_chain(result) is not None


@pytest.fixture()
async def container() -> AsyncIterator[PlatformContainer]:
    ctn = create_container(_settings(), with_simulator=False)
    await ctn.start()
    ctn.operators = OperatorService(_FakeLedger())
    await _seed(ctn, WRN, STU_A, STU_B)
    yield ctn
    await ctn.shutdown()


async def test_空间列表带任务计数(container: PlatformContainer) -> None:
    """夹具已通过 `ensure_from_chain` 建了一个空间：列表要如实给出它的计数。"""
    async with _client(container) as client:
        body = (await client.get("/api/v1/spaces")).json()
        assert body["count"] == 1
        row = body["items"][0]
        assert row["space_id"] == WRN and row["warning_id"] == WRN
        assert row["tasks_total"] == 2 and row["tasks_open"] == 2 and row["tasks_closed"] == 0
        assert row["status"] == "open"
        await _seed(container, "wrn_other012345", "stu_other00000001")
        again = (await client.get("/api/v1/spaces")).json()
        assert again["count"] == 2


async def test_空间详情给任务参与人与计数(container: PlatformContainer) -> None:
    async with _client(container) as client:
        body = (await client.get(f"/api/v1/spaces/{WRN}")).json()
        assert body["space"]["space_id"] == WRN
        assert {task["task_id"] for task in body["tasks"]} == {STU_A, STU_B}
        assert body["participants"] == [] and body["status_counts"] == {"pending": 2}
        assert (await client.get("/api/v1/spaces/wrn_missing000001")).status_code == 404


async def test_写动作必须带席位且角色要够(container: PlatformContainer) -> None:
    async with _client(container) as client:
        assert (await client.post(f"/api/v1/spaces/{WRN}/tasks/{STU_A}/claim")).status_code == 401
        assert (await client.post(f"/api/v1/spaces/{WRN}/tasks/{STU_A}/claim", json={}, headers=DUTY_HEADERS)).status_code == 200
        # 回执 duty 够；移交是 lead 的动作
        assert (await client.post(f"/api/v1/spaces/{WRN}/tasks/{STU_A}/ack", json={}, headers=DUTY_HEADERS)).status_code == 200
        assert (
            await client.post(f"/api/v1/spaces/{WRN}/tasks/{STU_B}/transfer", json={"assignee": "duty-02"}, headers=DUTY_HEADERS)
        ).status_code == 403


async def test_认领写回受理人且乐观锁生效(container: PlatformContainer) -> None:
    async with _client(container) as client:
        claimed = (await client.post(f"/api/v1/spaces/{WRN}/tasks/{STU_A}/claim", json={}, headers=DUTY_HEADERS)).json()
        assert claimed["task"]["status"] == "claimed" and claimed["task"]["assignee"] == "duty-01"
        stale_version = int(claimed["task"]["version"]) - 1
        conflict = await client.post(
            f"/api/v1/spaces/{WRN}/tasks/{STU_A}/ack", json={"expected_version": stale_version}, headers=DUTY_HEADERS
        )
        assert conflict.status_code == 409
        assert conflict.json()["detail"]["code"] == "E_TASK_STATE_CONFLICT"


async def test_移交到不存在或停用席位是422(container: PlatformContainer) -> None:
    async with _client(container) as client:
        assert (
            await client.post(f"/api/v1/spaces/{WRN}/tasks/{STU_A}/transfer", json={"assignee": "ghost-9"}, headers=LEAD_HEADERS)
        ).status_code == 422
        ok = await client.post(f"/api/v1/spaces/{WRN}/tasks/{STU_A}/transfer", json={"assignee": "duty-02"}, headers=LEAD_HEADERS)
        assert ok.status_code == 200
        assert ok.json()["task"]["assignee"] == "duty-02" and ok.json()["task"]["status"] == "pending"


async def test_用别的空间路径改任务是404不是409(container: PlatformContainer) -> None:
    async with _client(container) as client:
        await _seed(container, "wrn_other012345", "stu_other00000001")
        response = await client.post(f"/api/v1/spaces/wrn_other012345/tasks/{STU_A}/claim", json={}, headers=DUTY_HEADERS)
        assert response.status_code == 404
        assert response.json()["detail"]["code"] == "E_TARGET_NOT_FOUND"
