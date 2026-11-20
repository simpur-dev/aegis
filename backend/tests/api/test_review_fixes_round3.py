"""子代理审查第三轮修复的回归用例（workflow 核签面 + 记忆治理面）。

- L1：启动 payload 里塞 `decision` 不再绕过人工核签（此前 `POST /instances
  {"payload":{"decision":…}}` 让核签节点首跑直通 approve，人工闸形同虚设）；
- L2：并列两个人工核签叶子，resume 完第一个实例**不得**被判终态（此前第二个
  的决策被记录却永不执行，实例账面 succeeded）；
- 记忆删除的席位闸：无席位 401、DUTY 403、LEAD 200（写入 DUTY、删除 LEAD）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest

from aegis.api.app import create_app
from aegis.config import Settings
from aegis.container import create_container
from aegis.domain.operator import Operator, OperatorRole
from aegis.services.operators import OperatorService


class _StubOperatorLedger:
    """内存席位台账替身（对齐 test_alerts_api / test_workflow_api）。"""

    def __init__(self) -> None:
        self.rows: dict[str, Operator] = {}
        for operator_id, role in (("duty01", OperatorRole.DUTY), ("lead01", OperatorRole.LEAD)):
            self.rows[operator_id] = Operator(operator_id=operator_id, display_name=operator_id, role=role, active=True)

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
    return Settings(env="test", bus_backend="memory", store_backend="memory", simulator_enabled=False, delivery_mode="mock", llm_api_key="")


@pytest.fixture()
async def client() -> AsyncIterator[httpx.AsyncClient]:
    ctn = create_container(_settings(), with_simulator=False)
    ctn.operators = OperatorService(_StubOperatorLedger())
    await ctn.start()
    app = create_app(ctn.settings, container=ctn)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        yield http
    await ctn.shutdown()


DUTY = {"X-Aegis-Operator": "duty01"}
LEAD = {"X-Aegis-Operator": "lead01"}


class TestSignGate:
    @pytest.mark.asyncio
    async def test_payload塞decision不再绕过核签(self, client: httpx.AsyncClient) -> None:
        started = await client.post(
            "/api/v1/workflow/instances",
            json={"workflow_name": "人工上报核签流程", "payload": {"decision": {"choice": "approve", "by": "anyone"}}},
        )
        assert started.status_code == 200, started.text
        detail = (await client.get(f"/api/v1/workflow/instances/{started.json()['instance_id']}")).json()
        review = next(row for row in detail["nodes"] if row["node_id"] == "review")
        assert review["state"] == "awaiting_human", "核签节点必须挂起等人（此前被 payload 决策直通）"
        assert detail["status"] == "waiting"

    @pytest.mark.asyncio
    async def test_并列核签叶子_先签完一个实例不终态(self, client: httpx.AsyncClient) -> None:
        """画布可搭出 start → 两个并列 human_review 的图：签完一个，另一个还得等。"""

        def _node(node_id: str) -> dict:
            return {
                "node_id": node_id,
                "type": "start" if node_id == "start" else "human_review",
                "config": {} if node_id == "start" else {"options": ["approve", "reject"]},
                "sla_ms": 1000,
                "timeout_ms": 1000,
                "on_failure": "abort",
                "retry": {"max_attempts": 1},
            }

        nodes = [_node("start"), _node("review_a"), _node("review_b")]
        edges = [
            {"source": "start", "target": "review_a", "condition": ""},
            {"source": "start", "target": "review_b", "condition": ""},
        ]
        created = await client.post(
            "/api/v1/workflow/definitions",
            json={"name": "并列核签流程", "description": "两个核签叶子", "nodes": nodes, "edges": edges},
        )
        assert created.status_code == 201, created.text
        workflow_id = created.json()["workflow_id"]

        started = await client.post("/api/v1/workflow/instances", json={"workflow_id": workflow_id, "payload": {}})
        instance_id = started.json()["instance_id"]
        detail = (await client.get(f"/api/v1/workflow/instances/{instance_id}")).json()
        assert detail["status"] == "waiting"

        first = await client.post(
            f"/api/v1/workflow/instances/{instance_id}/nodes/review_a/decision",
            headers=DUTY,
            json={"choice": "approve"},
        )
        assert first.status_code == 200, first.text
        after_first = (await client.get(f"/api/v1/workflow/instances/{instance_id}")).json()
        assert after_first["status"] == "waiting", (
            "签完 review_a 后 review_b 还在等——实例不得被判 SUCCEEDED（此前第二个决策被记录却永不执行）"
        )

        second = await client.post(
            f"/api/v1/workflow/instances/{instance_id}/nodes/review_b/decision",
            headers=DUTY,
            json={"choice": "approve"},
        )
        assert second.status_code == 200, second.text
        after_second = (await client.get(f"/api/v1/workflow/instances/{instance_id}")).json()
        assert after_second["status"] == "succeeded", "两个都签完才终态"


class TestMemoryDeleteGate:
    @pytest.mark.asyncio
    async def test_删除记忆的席位闸(self, client: httpx.AsyncClient) -> None:
        lesson = "540121 泥位回落教训，重复升级同一条读数会误报"
        created = await client.post("/api/v1/memory/shared", headers=DUTY, json={"kind": "lesson", "content": lesson})
        assert created.status_code == 201, created.text
        record_id = created.json()["record_id"]

        no_header = await client.delete(f"/api/v1/memory/shared/{record_id}")
        assert no_header.status_code == 401, "无席位头一律 401"
        duty = await client.delete(f"/api/v1/memory/shared/{record_id}", headers=DUTY)
        assert duty.status_code == 403, "删除比写入重：DUTY 不够，要 LEAD"
        lead = await client.delete(f"/api/v1/memory/shared/{record_id}", headers=LEAD)
        assert lead.status_code == 200 and lead.json()["removed"] is True
