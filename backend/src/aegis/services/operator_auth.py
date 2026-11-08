"""按人令牌的签发与校验：把"声明身份"升级为"可验证凭据"（设计 §14.1）。

为什么单独一层：认证有三件事混在一起，各自有各自的失效条件——
**签发**（谁能给谁发一把钥匙）、**校验**（这把钥匙是不是这个人、还有效吗）、
**锁定**（连续失败怎么办）。混进 `services/operators.py` 会让"为什么这个人被拒"
变成散在若干分支里的 if，测试也没法单独钉住。

四条口径：
1. **只存摘要**：落库的是 SHA-256，明文只在签发那一次响应里出现，之后谁也拿不回去；
2. **凭证与声称不一致就是错**：带了 A 的席位头却拿 B 的令牌 → 401，不取其一、
   不"以席位头为准"——那等于允许借别人的席位；
3. **失败按人计数**：哈希对不上时没有身份可归属，按哈希计数等于换一把随机令牌就清零；
4. **锁定期内一律拒**（正确令牌也拒）：锁定的目的是拖慢爆破，不是提示谁猜对了。
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from aegis.domain.operator import Operator
from aegis.domain.operator_key import KEY_PREFIX, OperatorKey

#: 认证失败的原因码。与 `audit_ops.after.rejected` 里记的那一串同源，便于对账。
REASON_INVALID = "E_OPERATOR_KEY_INVALID"
REASON_LOCKED = "E_OPERATOR_KEY_LOCKED"
REASON_MISMATCH = "E_OPERATOR_KEY_MISMATCH"
REASON_REQUIRED = "E_OPERATOR_KEY_REQUIRED"
REASON_UNAVAILABLE = "E_OPERATOR_KEY_UNAVAILABLE"

#: 身份种类：审计里那一列 `identity_kind` 的取值。
IDENTITY_DECLARED = "declared"
IDENTITY_KEY = "key"


def hash_key(raw: str) -> str:
    """明文 → SHA-256 小写 hex。**不盐**：盐要落库才有用，而落库的盐与摘要放在一起时，
    离线爆破的成本并不比无盐高多少；真正的防线是"库被读走也拿不到可用凭据"，
    这靠的是摘要不可逆 + 明文不落库，而不是加盐。

    这里刻意不加 pepper：pepper 要有一个"服务端才知道"的秘密，而本仓没有密钥管理设施，
    硬塞一个配置文件里的常量只会制造"以为很安全"的错觉。
    """
    return hashlib.sha256(str(raw).encode("utf-8")).hexdigest()


class KeyLedger(Protocol):
    """令牌台账需要的面：内存与 PostgreSQL 两套实现同语义。"""

    async def create_operator_key(self, *, key_id: str, operator_id: str, key_hash: str) -> dict[str, Any]: ...

    async def get_operator_key_by_hash(self, key_hash: str) -> dict[str, Any] | None: ...

    async def list_operator_keys(self, operator_id: str) -> list[dict[str, Any]]: ...

    async def revoke_operator_key(self, key_id: str) -> bool: ...

    async def touch_operator_key(self, key_id: str) -> None: ...

    async def get_key_attempts(self, operator_id: str) -> dict[str, Any] | None: ...

    async def record_key_failure(self, *, operator_id: str, max_attempts: int, lock_minutes: int) -> dict[str, Any]: ...

    async def clear_key_failures(self, operator_id: str) -> None: ...


class OperatorLookup(Protocol):
    """席位台账：认证通过后要拿回席位（角色、是否停用）。"""

    async def get(self, operator_id: str) -> Operator | None: ...


class OperatorKeyUnavailableError(RuntimeError):
    """没有持久层就没有令牌台账：内存形态不假装能签发（与席位台账同口径）。"""


class OperatorKeyError(RuntimeError):
    """认证失败。`reason` 是给调用方与审计看的原因码，`message` 是给人看的句子。"""

    def __init__(self, reason: str, message: str, *, retry_after_seconds: int | None = None) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.retry_after_seconds = retry_after_seconds


@dataclass(frozen=True, slots=True)
class IssuedKey:
    """一次签发的结果：元数据 + **只此一次可见**的明文。"""

    key: OperatorKey
    plaintext: str


@dataclass(frozen=True, slots=True)
class AuthOutcome:
    """一次认证的结论。`operator` 为 None 表示"没有可用的强身份"（可能是声明身份）。"""

    operator: Operator | None
    identity_kind: str
    reason: str | None = None
    locked_until: str | None = None


class OperatorAuthService:
    """按人令牌的签发与认证。

    `enabled=False` 时本服务整体不生效：写动作仍走声明身份（§14.1 的口径），
    这不是"认证失败"，而是"部署方还没启用强认证"。
    """

    def __init__(
        self,
        ledger: KeyLedger | None,
        operators: OperatorLookup | None,
        *,
        enabled: bool = False,
        max_failed_attempts: int = 5,
        lock_minutes: int = 5,
    ) -> None:
        self._ledger = ledger
        self._operators = operators
        self._enabled = bool(enabled)
        self._max_attempts = max(1, int(max_failed_attempts))
        self._lock_minutes = max(1, int(lock_minutes))

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def available(self) -> bool:
        """能不能签发/校验：有台账、有席位面，且开关打开。"""
        return self._enabled and self._ledger is not None and self._operators is not None

    # ---------- 签发与吊销 ----------

    async def issue_key(self, operator_id: str) -> IssuedKey:
        """给一个席位签发一把新钥匙。**明文只在这里出现一次**。"""
        ledger = self._require_ledger()
        operator = await self._require_operator().get(operator_id)
        if operator is None:
            raise OperatorKeyError(REASON_INVALID, f"席位不存在或已停用：{operator_id}")
        plaintext = KEY_PREFIX + secrets.token_urlsafe(32)
        key_id = f"ok_{secrets.token_hex(8)}"
        row = await ledger.create_operator_key(key_id=key_id, operator_id=operator_id, key_hash=hash_key(plaintext))
        return IssuedKey(key=OperatorKey.model_validate(row), plaintext=plaintext)

    async def list_keys(self, operator_id: str) -> list[dict[str, Any]]:
        ledger = self._require_ledger()
        return [dict(row) for row in await ledger.list_operator_keys(operator_id)]

    async def revoke_key(self, operator_id: str, key_id: str) -> bool:
        """吊销一把钥匙。**只能吊销自己名下的**：别的席位的钥匙要 admin 来撤。"""
        ledger = self._require_ledger()
        revoked = await ledger.revoke_operator_key(key_id)
        if not revoked:
            return False
        # 吊销后该席位的失败计数一并清掉：钥匙换了一把，旧计数不该继续挂着。
        await ledger.clear_key_failures(operator_id)
        return True

    # ---------- 认证 ----------

    async def authenticate(
        self,
        *,
        raw_key: str | None,
        claimed_operator_id: str | None = None,
    ) -> AuthOutcome:
        """校验一把按人令牌。

        返回 `AuthOutcome`：
        - 有效 → `operator` 是令牌绑定的席位，`identity_kind='key'`；
        - 无 key → `operator=None`、`identity_kind='declared'`（**不是失败**，
          是否放行由调用方按"这次操作要不要强身份"决定）；
        - 无效/锁定 → 抛 `OperatorKeyError`（调用方转 401 并留痕）。
        """
        if raw_key is None or not str(raw_key).strip():
            return AuthOutcome(operator=None, identity_kind=IDENTITY_DECLARED)

        ledger = self._require_ledger()
        now = datetime.now(UTC)

        # 第一道锁：带着席位头来的，先看这个席位锁着没有——锁着就不必再查哈希。
        if claimed_operator_id:
            locked = await self._locked_until(ledger, claimed_operator_id, now)
            if locked is not None:
                raise self._locked_error(locked, now)

        row = await ledger.get_operator_key_by_hash(hash_key(raw_key))
        if row is None:
            # 摘要对不上：没有身份可归属，只能在"声称的席位"名下计数（若带了席位头）。
            if claimed_operator_id:
                await self._fail(ledger, claimed_operator_id, now)
            raise OperatorKeyError(REASON_INVALID, "按人令牌无效")

        operator_id = str(row["operator_id"])
        # 第二道锁：令牌所属席位锁着没有。
        locked = await self._locked_until(ledger, operator_id, now)
        if locked is not None:
            raise self._locked_error(locked, now)

        if not bool(row.get("active", True)):
            raise OperatorKeyError(REASON_INVALID, f"按人令牌已吊销：{row.get('key_id')}")

        # 凭证与声称不一致 → 拒。取其一等于允许借别人的席位。
        if claimed_operator_id and claimed_operator_id != operator_id:
            await self._fail(ledger, operator_id, now)
            raise OperatorKeyError(
                REASON_MISMATCH,
                f"按人令牌属于 {operator_id}，与声称的席位 {claimed_operator_id} 不一致",
            )

        operator = await self._require_operator().get(operator_id)
        if operator is None or not operator.active:
            raise OperatorKeyError(REASON_INVALID, f"令牌绑定的席位不存在或已停用：{operator_id}")

        await ledger.touch_operator_key(str(row["key_id"]))
        await ledger.clear_key_failures(operator_id)
        return AuthOutcome(operator=operator, identity_kind=IDENTITY_KEY)

    async def step_up(self, *, raw_key: str | None, operator_id: str) -> None:
        """高危动作的**二次认证**（§14.1.3）：同一个席位再出示一次令牌。

        与双人复核正交——复核是"两个不同席位各投一票"（横向），二次认证是"本人再亮一次钥匙"（纵向）。
        失败抛 `OperatorKeyError`，调用方转 401；失败同样计入锁定。
        """
        if raw_key is None or not str(raw_key).strip():
            raise OperatorKeyError(REASON_REQUIRED, "高危动作需要二次认证：请带上 X-Aegis-Operator-Key")
        ledger = self._require_ledger()
        now = datetime.now(UTC)
        locked = await self._locked_until(ledger, operator_id, now)
        if locked is not None:
            raise self._locked_error(locked, now)
        row = await ledger.get_operator_key_by_hash(hash_key(raw_key))
        if row is None or not bool(row.get("active", True)):
            await self._fail(ledger, operator_id, now)
            raise OperatorKeyError(REASON_INVALID, "二次认证失败：按人令牌无效")
        if str(row["operator_id"]) != operator_id:
            await self._fail(ledger, operator_id, now)
            raise OperatorKeyError(REASON_MISMATCH, "二次认证失败：令牌不属于当前席位")
        await ledger.clear_key_failures(operator_id)

    # ---------- 内部 ----------

    def _require_ledger(self) -> KeyLedger:
        if self._ledger is None:
            raise OperatorKeyUnavailableError("按人令牌台账不可用：当前存储后端没有 operator_keys 面")
        return self._ledger

    def _require_operator(self) -> OperatorLookup:
        if self._operators is None:
            raise OperatorKeyUnavailableError("席位台账不可用：无法校验令牌绑定的席位")
        return self._operators

    async def _locked_until(self, ledger: KeyLedger, operator_id: str, now: datetime) -> datetime | None:
        attempts = await ledger.get_key_attempts(operator_id)
        if not attempts:
            return None
        raw = attempts.get("locked_until")
        if not raw:
            return None
        until = datetime.fromisoformat(str(raw)) if isinstance(raw, str) else raw
        if until.tzinfo is None:
            until = until.replace(tzinfo=UTC)
        return until if until > now else None

    def _locked_error(self, until: datetime, now: datetime) -> OperatorKeyError:
        remaining = max(1, int((until - now).total_seconds()))
        return OperatorKeyError(
            REASON_LOCKED,
            f"连续认证失败已锁定，请 {remaining} 秒后再试",
            retry_after_seconds=remaining,
        )

    async def _fail(self, ledger: KeyLedger, operator_id: str, now: datetime) -> dict[str, Any]:
        """记一次失败并按阈值锁定。返回值给调用方写日志（不外泄给请求方）。"""
        return await ledger.record_key_failure(
            operator_id=operator_id,
            max_attempts=self._max_attempts,
            lock_minutes=self._lock_minutes,
        )


def lock_window(max_failed_attempts: int, lock_minutes: int) -> timedelta:
    """锁定窗口长度（供调用方在文档/响应里说"还有多久"）。"""
    return timedelta(minutes=max(1, int(lock_minutes)))
