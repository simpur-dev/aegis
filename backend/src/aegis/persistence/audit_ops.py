"""动作审计流水（`audit_ops`，010 建表 / 012 起按月分区）的 SQL 侧：只 INSERT，没有别的路径。

为什么这个模块小得只有一句 INSERT：审计的难点不在落库而在"每条状态迁移都带一行"——
那由 `PostgresStore` 的三个迁移方法在**同一事务**里调这里保证；本模块刻意不提供
UPDATE/DELETE/SELECT 之外的任何写入形态，让"只追加"在代码层面无法被绕过。
读侧（按 operator/action 查审计）属于 P4 的读面需求，需要时再加，不预先写空壳。
"""

from __future__ import annotations

from typing import Any

_COLUMNS = (
    "action_id, phase, operator_id, role, action, space_id, target, before, after, utterance, resolved_args, idempotency_key, identity_kind"
)


async def insert(
    pool_or_conn: Any,
    *,
    phase: str,
    action_id: str,
    operator_id: str,
    role: str,
    action: str,
    space_id: str | None,
    target: str,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    utterance: str,
    resolved_args: dict[str, Any],
    idempotency_key: str | None,
    identity_kind: str,
) -> None:
    """追加一行审计。`before/after/resolved_args` 直接传 Python 对象，orjson 编解码器负责序列化。"""
    await pool_or_conn.execute(
        f"INSERT INTO audit_ops ({_COLUMNS}) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)",
        action_id,
        phase,
        operator_id,
        role,
        action,
        space_id,
        target[:500],
        before,
        after,
        utterance[:500],
        resolved_args,
        idempotency_key,
        identity_kind,
    )
