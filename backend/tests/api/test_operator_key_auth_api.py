"""强认证在 API 层的门禁（P4，设计 §14.1.1 / §14.1.3）。

钉四件事：
1. **开关默认关时行为不变**：只带席位头的写动作照样能走（D5「指令即执行」不受影响）；
2. **开关打开后写动作必须带有效令牌**：只带席位头 → 401 `E_OPERATOR_KEY_REQUIRED`；
3. **读动作不受影响**：强认证不等于"什么都得登录"（capabilities 仍可匿名读）；
4. **令牌错/席位不匹配 → 401**（"带了但不对"与"没带"是两种事实）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx

from aegis.api.app import create_app
from aegis.config import Settings
from aegis.container import PlatformContainer, create_container
from aegis.domain.operator import Operator
from aegis.services.operator_auth import OperatorAuthService, hash_key
from aegis.services.operators import OperatorService


class _FakeOperatorLedger:
    def __init__(self) -> None:
        self.rows = {
            "duty-01": Operator(operator_id="duty-01", display_name="值班员", role="duty"),
            "lead-01": Operator(operator_id="lead-01", display_name="值班长", role="lead"),
            "admin-01": Operator(operator_id="admin-01", display_name="管理员", role="admin"),
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


class _FakeKeyLedger:
    """内存令牌台账：只实现认证路径用到的那几面。"""

    def __init__(self) -> None:
        self.by_hash: dict[str, dict[str, Any]] = {}
        self.attempts: dict[str, dict[str, Any]] = {}

    async def create_operator_key(self, *, key_id: str, operator_id: str, key_hash: str) -> dict[str, Any]:
        row = {"key_id": key_id, "operator_id": operator_id, "active": True, "key_hash": key_hash}
        self.by_hash[key_hash] = row
        return {k: v for k, v in row.items() if k != "key_hash"}

    async def get_operator_key_by_hash(self, key_hash: str) -> dict[str, Any] | None:
        return self.by_hash.get(key_hash)

    async def list_operator_keys(self, operator_id: str) -> list[dict[str, Any]]:
        return []

    async def revoke_operator_key(self, key_id: str) -> bool:
        return False

    async def touch_operator_key(self, key_id: str) -> None:
        return None

    async def get_key_attempts(self, operator_id: str) -> dict[str, Any] | None:
        return self.attempts.get(operator_id)

    async def record_key_failure(self, *, operator_id: str, max_attempts: int, lock_minutes: int) -> dict[str, Any]:
        cur = self.attempts.setdefault(operator_id, {"failed_attempts": 0, "locked_until": None})
        cur["failed_attempts"] += 1
        return {"operator_id": operator_id, **cur}

    async def clear_key_failures(self, operator_id: str) -> None:
        self.attempts[operator_id] = {"failed_attempts": 0, "locked_until": None}


def _settings(*, key_auth: bool) -> Settings:
    return Settings(
        env="test",
        bus_backend="memory",
        store_backend="memory",
        simulator_enabled=False,
        delivery_mode="mock",
        llm_api_key="",
        operator_key_auth_enabled=key_auth,
    )


@asynccontextmanager
async def _client(container: PlatformContainer) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(container.settings, container=container)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        yield client


async def _build(*, key_auth: bool) -> tuple[PlatformContainer, str]:
    """建容器并签发一把 duty-01 的令牌；返回 (容器, 明文)。"""
    ctn = create_container(_settings(key_auth=key_auth), with_simulator=False)
    await ctn.start()
    operators = OperatorService(_FakeOperatorLedger())
    ctn.operators = operators

    keys = _FakeKeyLedger()
    auth = OperatorAuthService(keys, operators, enabled=key_auth)
    ctn.operator_auth = auth
    issued = await auth.issue_key("duty-01")
    assert hash_key(issued.plaintext) in keys.by_hash
    return ctn, issued.plaintext


class Test开关关时行为不变:
    async def test_只带席位头的写动作照旧(self) -> None:
        """默认关：D5「指令即执行」逐字不变，不强加认证。"""
        ctn, _ = await _build(key_auth=False)
        try:
            async with _client(ctn) as client:
                response = await client.post(
                    "/api/v1/assistant/actions",
                    json={"action": "warning.publish", "params": {"warning_id": "wrn_404"}},
                    headers={"X-Aegis-Operator": "lead-01"},
                )
            # 不是 401——认证不是这道门的职责；业务失败（找不到目标）才是它该报的
            assert response.status_code != 401
        finally:
            await ctn.shutdown()


class Test开关开时写动作要令牌:
    async def test_只带席位头被拒_401(self) -> None:
        ctn, _ = await _build(key_auth=True)
        try:
            async with _client(ctn) as client:
                response = await client.post(
                    "/api/v1/assistant/actions",
                    json={"action": "warning.publish", "params": {"warning_id": "wrn_404"}},
                    headers={"X-Aegis-Operator": "lead-01"},
                )
            assert response.status_code == 401
            assert response.json()["detail"]["code"] == "E_OPERATOR_KEY_REQUIRED"
        finally:
            await ctn.shutdown()

    async def test_带有效令牌放行到业务层(self) -> None:
        ctn, plaintext = await _build(key_auth=True)
        try:
            async with _client(ctn) as client:
                response = await client.post(
                    "/api/v1/assistant/actions",
                    json={"action": "warning.publish", "params": {"warning_id": "wrn_404"}},
                    headers={
                        "X-Aegis-Operator": "duty-01",
                        "X-Aegis-Operator-Key": plaintext,
                    },
                )
            # 通过了认证门（不是 401），后面是业务判定
            assert response.status_code != 401
        finally:
            await ctn.shutdown()

    async def test_令牌错被拒_401(self) -> None:
        ctn, _ = await _build(key_auth=True)
        try:
            async with _client(ctn) as client:
                response = await client.post(
                    "/api/v1/assistant/actions",
                    json={"action": "warning.publish", "params": {"warning_id": "wrn_404"}},
                    headers={"X-Aegis-Operator": "duty-01", "X-Aegis-Operator-Key": "aegis_op_bogus"},
                )
            assert response.status_code == 401
            assert response.json()["detail"]["code"] == "E_OPERATOR_KEY_INVALID"
        finally:
            await ctn.shutdown()

    async def test_凭证与声称席位不一致被拒_401(self) -> None:
        """拿 duty-01 的令牌声称是 lead-01 = 借别人的席位。"""
        ctn, plaintext = await _build(key_auth=True)
        try:
            async with _client(ctn) as client:
                response = await client.post(
                    "/api/v1/assistant/actions",
                    json={"action": "warning.publish", "params": {"warning_id": "wrn_404"}},
                    headers={"X-Aegis-Operator": "lead-01", "X-Aegis-Operator-Key": plaintext},
                )
            assert response.status_code == 401
            assert response.json()["detail"]["code"] == "E_OPERATOR_KEY_MISMATCH"
        finally:
            await ctn.shutdown()


class Test读动作不受影响:
    async def test_capabilities_匿名仍可读(self) -> None:
        ctn, _ = await _build(key_auth=True)
        try:
            async with _client(ctn) as client:
                response = await client.get("/api/v1/assistant/capabilities")
            assert response.status_code == 200
            assert "actions" in response.json()
        finally:
            await ctn.shutdown()

    async def test_capabilities_只带席位头也可读(self) -> None:
        ctn, _ = await _build(key_auth=True)
        try:
            async with _client(ctn) as client:
                response = await client.get("/api/v1/assistant/capabilities", headers={"X-Aegis-Operator": "duty-01"})
            assert response.status_code == 200
        finally:
            await ctn.shutdown()
