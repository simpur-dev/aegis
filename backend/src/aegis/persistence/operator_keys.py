"""按人令牌台账的 SQL 侧：只做读写与行转换，不做认证判定。

分层与 `persistence/operators.py` 一致：这里只管 SQL、参数与顺序；
"这把令牌能不能用、锁了没有"住在 `services/operator_auth.py`。参数一律绑定，不拼字符串。

三条口径落在代码里而不是注释里：
- 读路径**不返回 `key_hash`**（`_PUBLIC_COLUMNS` 里没有它）：摘要与明文都不出这台机器；
- 失败计数与锁定在**一条语句**里完成（UPSERT + CASE）：并发下不会漏计，
  且已在锁定期的 `locked_until` 不被后续失败往后推；
- 吊销是 `active=false` 而不是 DELETE：审计要追得到"这把钥匙曾经存在"。
"""

from __future__ import annotations

from typing import Any

#: 读路径允许返回的列——**故意不含 `key_hash`**。
_PUBLIC_COLUMNS = "key_id, operator_id, active, created_at, last_used_at"


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _public_row(row: Any) -> dict[str, Any]:
    """行 → 读面的一条 dict。**不含 `key_hash`**：摘要不出这台机器。"""
    return {
        "key_id": str(row["key_id"]),
        "operator_id": str(row["operator_id"]),
        "active": bool(row["active"]),
        "created_at": _iso(row["created_at"]),
        "last_used_at": _iso(row["last_used_at"]),
    }


async def create_operator_key(pool: Any, *, key_id: str, operator_id: str, key_hash: str) -> dict[str, Any]:
    row = await pool.fetchrow(
        """
        INSERT INTO operator_keys (key_id, operator_id, key_hash)
        VALUES ($1, $2, $3)
        RETURNING key_id, operator_id, active, created_at, last_used_at
        """,
        key_id,
        operator_id,
        key_hash,
    )
    return _public_row(row)


async def get_operator_key_by_hash(pool: Any, key_hash: str) -> dict[str, Any] | None:
    """按摘要找钥匙。

    认证路径要用 `active` 与 `key_id`，而 `OperatorKey` 不带摘要——所以这里返回原始 dict，
    由调用方自己挑要用的字段（读接口那条路走 `_public_row`，不暴露摘要）。
    """
    return await pool.fetchrow(
        """
        SELECT key_id, operator_id, key_hash, active, created_at, last_used_at
        FROM operator_keys WHERE key_hash = $1
        """,
        key_hash,
    )


async def list_operator_keys(pool: Any, operator_id: str) -> list[dict[str, Any]]:
    rows = await pool.fetch(
        f"SELECT {_PUBLIC_COLUMNS} FROM operator_keys WHERE operator_id = $1 ORDER BY created_at DESC",
        operator_id,
    )
    return [_public_row(row) for row in rows]


async def revoke_operator_key(pool: Any, key_id: str) -> bool:
    """吊销（不删行）。返回是否真的改到了——对不存在的 key_id 返回 False，调用方据此回 404。"""
    row = await pool.fetchrow(
        "UPDATE operator_keys SET active = false WHERE key_id = $1 AND active = true RETURNING key_id",
        key_id,
    )
    return row is not None


async def touch_operator_key(pool: Any, key_id: str) -> None:
    """记一次使用（不更新摘要，不改 active）。"""
    await pool.execute("UPDATE operator_keys SET last_used_at = now() WHERE key_id = $1", key_id)


async def get_key_attempts(pool: Any, operator_id: str) -> dict[str, Any] | None:
    row = await pool.fetchrow(
        "SELECT operator_id, failed_attempts, locked_until, updated_at FROM operator_key_attempts WHERE operator_id = $1",
        operator_id,
    )
    if row is None:
        return None
    return {
        "operator_id": str(row["operator_id"]),
        "failed_attempts": int(row["failed_attempts"]),
        "locked_until": _iso(row["locked_until"]),
        "updated_at": _iso(row["updated_at"]),
    }


async def record_key_failure(
    pool: Any,
    *,
    operator_id: str,
    max_attempts: int,
    lock_minutes: int,
) -> dict[str, Any]:
    """记一次失败并按阈值锁定——**一条语句**完成读改写。

    已在锁定期的 `locked_until` 保持原样（不往后推）：否则每次失败都把锁定延长一次，
    一次爆破就能让人被永久锁在外面。
    """
    row = await pool.fetchrow(
        """
        INSERT INTO operator_key_attempts (operator_id, failed_attempts, locked_until, updated_at)
        VALUES ($1, 1,
                CASE WHEN 1 >= $2 THEN now() + make_interval(mins => $3::int) ELSE NULL END,
                now())
        ON CONFLICT (operator_id) DO UPDATE SET
            failed_attempts = operator_key_attempts.failed_attempts + 1,
            locked_until = CASE
                WHEN operator_key_attempts.locked_until IS NOT NULL
                     AND operator_key_attempts.locked_until > now()
                    THEN operator_key_attempts.locked_until
                WHEN operator_key_attempts.failed_attempts + 1 >= $2
                    THEN now() + make_interval(mins => $3::int)
                ELSE NULL
            END,
            updated_at = now()
        RETURNING operator_id, failed_attempts, locked_until, updated_at
        """,
        operator_id,
        int(max_attempts),
        int(lock_minutes),
    )
    return {
        "operator_id": str(row["operator_id"]),
        "failed_attempts": int(row["failed_attempts"]),
        "locked_until": _iso(row["locked_until"]),
        "updated_at": _iso(row["updated_at"]),
    }


async def clear_key_failures(pool: Any, operator_id: str) -> None:
    """认证成功：计数归零、锁定解除。"""
    await pool.execute(
        """
        INSERT INTO operator_key_attempts (operator_id, failed_attempts, locked_until, updated_at)
        VALUES ($1, 0, NULL, now())
        ON CONFLICT (operator_id) DO UPDATE
        SET failed_attempts = 0, locked_until = NULL, updated_at = now()
        """,
        operator_id,
    )
