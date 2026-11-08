"""按人令牌的服务层判据（P4 强认证，设计 §14.1）。

fake ledger 照着 SQL 侧的关键语义写（尤其"已锁定的窗口不被后续失败延长"），
所以这组用例钉的是**服务层与台账层共同认得的口径**，不是 fake 自己的行为。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from aegis.domain.operator import Operator, OperatorRole
from aegis.services.operator_auth import (
    REASON_INVALID,
    REASON_LOCKED,
    REASON_MISMATCH,
    OperatorAuthService,
    OperatorKeyError,
    hash_key,
)


class FakeKeyLedger:
    """内存令牌台账：语义对齐 `persistence/operator_keys.py`。"""

    def __init__(self) -> None:
        self.keys: dict[str, dict[str, Any]] = {}
        self.by_hash: dict[str, str] = {}
        self.attempts: dict[str, dict[str, Any]] = {}
        self.touched: list[str] = []

    async def create_operator_key(self, *, key_id: str, operator_id: str, key_hash: str) -> dict[str, Any]:
        row = {
            "key_id": key_id,
            "operator_id": operator_id,
            "key_hash": key_hash,
            "active": True,
            "created_at": "2026-11-08T00:00:00+00:00",
            "last_used_at": None,
        }
        self.keys[key_id] = row
        self.by_hash[key_hash] = key_id
        return {k: v for k, v in row.items() if k != "key_hash"}

    async def get_operator_key_by_hash(self, key_hash: str) -> dict[str, Any] | None:
        key_id = self.by_hash.get(key_hash)
        return self.keys.get(key_id) if key_id else None

    async def list_operator_keys(self, operator_id: str) -> list[dict[str, Any]]:
        return [{k: v for k, v in row.items() if k != "key_hash"} for row in self.keys.values() if row["operator_id"] == operator_id]

    async def revoke_operator_key(self, key_id: str) -> bool:
        row = self.keys.get(key_id)
        if row is None or not row["active"]:
            return False
        row["active"] = False
        return True

    async def touch_operator_key(self, key_id: str) -> None:
        self.touched.append(key_id)

    async def get_key_attempts(self, operator_id: str) -> dict[str, Any] | None:
        return self.attempts.get(operator_id)

    async def record_key_failure(self, *, operator_id: str, max_attempts: int, lock_minutes: int) -> dict[str, Any]:
        now = datetime.now(UTC)
        current = self.attempts.get(operator_id)
        if current is None:
            current = {"failed_attempts": 0, "locked_until": None}
        locked_until = current.get("locked_until")
        if locked_until is not None and locked_until > now:
            # 已在锁定期：窗口不延长（与 SQL 侧 CASE 一致）
            pass
        elif current["failed_attempts"] + 1 >= max_attempts:
            locked_until = now + timedelta(minutes=lock_minutes)
        else:
            locked_until = None
        current["failed_attempts"] += 1
        current["locked_until"] = locked_until
        self.attempts[operator_id] = current
        return {
            "operator_id": operator_id,
            "failed_attempts": current["failed_attempts"],
            "locked_until": locked_until.isoformat() if locked_until else None,
            "updated_at": now.isoformat(),
        }

    async def clear_key_failures(self, operator_id: str) -> None:
        self.attempts[operator_id] = {"failed_attempts": 0, "locked_until": None}


class FakeOperators:
    def __init__(self, rows: dict[str, Operator] | None = None) -> None:
        self.rows = rows or {}

    async def get(self, operator_id: str) -> Operator | None:
        return self.rows.get(operator_id)


def _operator(operator_id: str = "duty01", role: OperatorRole = OperatorRole.DUTY) -> Operator:
    return Operator(operator_id=operator_id, display_name="值班员", role=role, active=True)


def _service(
    ledger: FakeKeyLedger,
    *,
    operators: FakeOperators | None = None,
    enabled: bool = True,
    max_failed_attempts: int = 5,
    lock_minutes: int = 5,
) -> OperatorAuthService:
    return OperatorAuthService(
        ledger,
        operators or FakeOperators({"duty01": _operator(), "lead01": _operator("lead01", OperatorRole.LEAD)}),
        enabled=enabled,
        max_failed_attempts=max_failed_attempts,
        lock_minutes=lock_minutes,
    )


class Test签发:
    async def test_明文只在签发响应里出现一次_库里只有摘要(self) -> None:
        ledger = FakeKeyLedger()
        service = _service(ledger)
        issued = await service.issue_key("duty01")

        assert issued.plaintext.startswith("aegis_op_")
        stored = next(iter(ledger.keys.values()))
        assert stored["key_hash"] == hash_key(issued.plaintext)
        # 库里没有明文：这是"库被读走也拿不到可用凭据"那条口径的实现
        assert issued.plaintext not in stored.values()

    async def test_读路径不返回摘要(self) -> None:
        ledger = FakeKeyLedger()
        service = _service(ledger)
        await service.issue_key("duty01")
        listed = await service.list_keys("duty01")
        assert len(listed) == 1
        assert "key_hash" not in listed[0]
        assert "plaintext" not in listed[0]

    async def test_席位不存在时签不了(self) -> None:
        service = _service(FakeKeyLedger())
        with pytest.raises(OperatorKeyError):
            await service.issue_key("nobody")


class Test认证:
    async def test_有效令牌通过且身份是_key(self) -> None:
        ledger = FakeKeyLedger()
        service = _service(ledger)
        issued = await service.issue_key("duty01")

        outcome = await service.authenticate(raw_key=issued.plaintext, claimed_operator_id="duty01")
        assert outcome.operator is not None
        assert outcome.operator.operator_id == "duty01"
        assert outcome.identity_kind == "key"

    async def test_没有令牌不是失败_是声明身份(self) -> None:
        outcome = await _service(FakeKeyLedger()).authenticate(raw_key=None)
        assert outcome.operator is None
        assert outcome.identity_kind == "declared"

    async def test_令牌错时拒绝并计一次失败(self) -> None:
        ledger = FakeKeyLedger()
        service = _service(ledger)
        await service.issue_key("duty01")

        with pytest.raises(OperatorKeyError) as exc:
            await service.authenticate(raw_key="aegis_op_wrong", claimed_operator_id="duty01")
        assert exc.value.reason == REASON_INVALID
        assert ledger.attempts["duty01"]["failed_attempts"] == 1

    async def test_凭证与声称不一致时拒绝(self) -> None:
        """带着 A 的席位头拿 B 的令牌 = 借别人的席位，必须拒而不是取其一。"""
        ledger = FakeKeyLedger()
        service = _service(ledger)
        issued = await service.issue_key("lead01")

        with pytest.raises(OperatorKeyError) as exc:
            await service.authenticate(raw_key=issued.plaintext, claimed_operator_id="duty01")
        assert exc.value.reason == REASON_MISMATCH

    async def test_吊销后令牌不再可用(self) -> None:
        ledger = FakeKeyLedger()
        service = _service(ledger)
        issued = await service.issue_key("duty01")
        await service.revoke_key("duty01", issued.key.key_id)

        with pytest.raises(OperatorKeyError):
            await service.authenticate(raw_key=issued.plaintext)

    async def test_认证成功清零失败计数(self) -> None:
        ledger = FakeKeyLedger()
        service = _service(ledger)
        issued = await service.issue_key("duty01")
        with pytest.raises(OperatorKeyError):
            await service.authenticate(raw_key="aegis_op_wrong", claimed_operator_id="duty01")
        assert ledger.attempts["duty01"]["failed_attempts"] == 1

        await service.authenticate(raw_key=issued.plaintext)
        assert ledger.attempts["duty01"]["failed_attempts"] == 0


class Test连续失败锁定:
    async def test_连续失败达阈值即锁定(self) -> None:
        ledger = FakeKeyLedger()
        service = _service(ledger, max_failed_attempts=3, lock_minutes=5)
        await service.issue_key("duty01")

        for _ in range(3):
            with pytest.raises(OperatorKeyError):
                await service.authenticate(raw_key="aegis_op_wrong", claimed_operator_id="duty01")

        attempts = ledger.attempts["duty01"]
        assert attempts["failed_attempts"] == 3
        assert attempts["locked_until"] is not None

    async def test_锁定期内正确令牌也拒(self) -> None:
        """锁定的目的是拖慢爆破——正确令牌在锁定期内同样被拒。"""
        ledger = FakeKeyLedger()
        service = _service(ledger, max_failed_attempts=2, lock_minutes=5)
        issued = await service.issue_key("duty01")
        for _ in range(2):
            with pytest.raises(OperatorKeyError):
                await service.authenticate(raw_key="aegis_op_wrong", claimed_operator_id="duty01")

        with pytest.raises(OperatorKeyError) as exc:
            await service.authenticate(raw_key=issued.plaintext)
        assert exc.value.reason == REASON_LOCKED
        assert exc.value.retry_after_seconds is not None

    async def test_锁定窗口不被后续失败延长(self) -> None:
        """一次爆破不能把人永久锁在外面：已在锁定期的失败不再推后窗口。"""
        ledger = FakeKeyLedger()
        service = _service(ledger, max_failed_attempts=2, lock_minutes=5)
        await service.issue_key("duty01")
        for _ in range(2):
            with pytest.raises(OperatorKeyError):
                await service.authenticate(raw_key="aegis_op_wrong", claimed_operator_id="duty01")
        first = ledger.attempts["duty01"]["locked_until"]

        with pytest.raises(OperatorKeyError):
            await service.authenticate(raw_key="aegis_op_wrong", claimed_operator_id="duty01")
        assert ledger.attempts["duty01"]["locked_until"] == first

    async def test_锁定到期自动解除(self) -> None:
        ledger = FakeKeyLedger()
        service = _service(ledger, max_failed_attempts=1, lock_minutes=5)
        issued = await service.issue_key("duty01")
        with pytest.raises(OperatorKeyError):
            await service.authenticate(raw_key="aegis_op_wrong", claimed_operator_id="duty01")
        assert ledger.attempts["duty01"]["locked_until"] is not None

        # 把窗口挪到过去 = 到期；不需要定时任务来解锁
        past = datetime.now(UTC) - timedelta(minutes=1)
        ledger.attempts["duty01"]["locked_until"] = past

        outcome = await service.authenticate(raw_key=issued.plaintext)
        assert outcome.operator is not None


class Test二次认证:
    async def test_有效令牌通过(self) -> None:
        ledger = FakeKeyLedger()
        service = _service(ledger)
        issued = await service.issue_key("duty01")
        await service.step_up(raw_key=issued.plaintext, operator_id="duty01")

    async def test_没有令牌时拒(self) -> None:
        service = _service(FakeKeyLedger())
        with pytest.raises(OperatorKeyError):
            await service.step_up(raw_key=None, operator_id="duty01")

    async def test_别人的令牌不算二次认证(self) -> None:
        ledger = FakeKeyLedger()
        service = _service(ledger)
        issued = await service.issue_key("lead01")
        with pytest.raises(OperatorKeyError) as exc:
            await service.step_up(raw_key=issued.plaintext, operator_id="duty01")
        assert exc.value.reason == REASON_MISMATCH

    async def test_失败同样计入锁定(self) -> None:
        ledger = FakeKeyLedger()
        service = _service(ledger)
        with pytest.raises(OperatorKeyError):
            await service.step_up(raw_key="aegis_op_wrong", operator_id="duty01")
        assert ledger.attempts["duty01"]["failed_attempts"] == 1
