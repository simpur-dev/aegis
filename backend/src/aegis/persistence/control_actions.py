"""动作执行记录的 SQL 侧：状态机 + 乐观迁移，不做权限判定。

为什么每个迁移都写成 `UPDATE ... WHERE status=...`：确认、撤销、过期三条路都可能被并发触发
（两个人同时点确认、确认与过期同时到），"先查后改"会让两个请求都通过。命中 0 行就是"没抢到"，
调用方据此返回 409，而不是覆盖别人的结果。

`result`/`error`/`impact_scope`/`resolved_args` 都是 jsonb：参数直接传 Python 对象，
建连时注册的 orjson 编解码器负责序列化（自己 `json.dumps` 会再包一层引号）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

_COLUMNS = (
    "action_id, operator_id, action, space_id, utterance, resolved_args, status, "
    "idempotency_key, impact_scope, result, error, expires_at, created_at, updated_at"
)
REVOKE_WINDOW_SECONDS = 60
STUCK_AFTER_SECONDS = 600


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def action_from_row(row: Any) -> dict[str, Any]:
    return {
        "action_id": str(row["action_id"]),
        "operator_id": str(row["operator_id"]),
        "action": str(row["action"]),
        "space_id": None if row["space_id"] is None else str(row["space_id"]),
        "utterance": str(row["utterance"] or ""),
        "resolved_args": dict(row["resolved_args"] or {}),
        "status": str(row["status"]),
        "idempotency_key": None if row["idempotency_key"] is None else str(row["idempotency_key"]),
        "impact_scope": dict(row["impact_scope"] or {}),
        "result": None if row["result"] is None else dict(row["result"]),
        "error": None if row["error"] is None else dict(row["error"]),
        "expires_at": _iso(row["expires_at"]),
        "created_at": _iso(row["created_at"]),
        "updated_at": _iso(row["updated_at"]),
    }


async def create_proposed(
    pool: Any,
    *,
    action_id: str,
    operator_id: str,
    action: str,
    space_id: str | None,
    utterance: str,
    resolved_args: dict[str, Any],
    impact_scope: dict[str, Any],
    expires_in_seconds: int,
    idempotency_key: str | None,
) -> tuple[dict[str, Any], bool]:
    """落一条 `proposed`。返回 (记录, 是否幂等重放)。

    幂等键冲突时不覆盖：直接返回首次那条，`replayed=True`——重复提交同一件事不该产生第二个提案。
    """
    row = await pool.fetchrow(
        f"""
        INSERT INTO control_actions
            (action_id, operator_id, action, space_id, utterance, resolved_args, status,
             idempotency_key, impact_scope, expires_at)
        VALUES ($1, $2, $3, $4, $5, $6, 'proposed', $7, $8, now() + make_interval(secs => $9))
        ON CONFLICT (idempotency_key) DO NOTHING
        RETURNING {_COLUMNS}
        """,
        action_id,
        operator_id,
        action,
        space_id,
        utterance[:500],
        resolved_args,
        idempotency_key,
        impact_scope,
        int(expires_in_seconds),
    )
    if row is not None:
        return action_from_row(row), False
    existing = await pool.fetchrow(f"SELECT {_COLUMNS} FROM control_actions WHERE idempotency_key = $1", idempotency_key)
    if existing is None:  # 冲突但不是幂等键（理论上不可能）：如实报错，别静默返回空
        raise RuntimeError("control_actions 写入冲突且找不到原记录")
    return action_from_row(existing), True


async def confirm_action(pool: Any, action_id: str) -> dict[str, Any] | None:
    """`proposed → executing`。命中 0 行 = 已被处置、已过期或不存在。"""
    row = await pool.fetchrow(
        f"""
        UPDATE control_actions SET status = 'executing', updated_at = now()
        WHERE action_id = $1 AND status = 'proposed' AND (expires_at IS NULL OR expires_at > now())
        RETURNING {_COLUMNS}
        """,
        action_id,
    )
    return None if row is None else action_from_row(row)


async def mark_executed(pool: Any, action_id: str, result: dict[str, Any]) -> dict[str, Any] | None:
    row = await pool.fetchrow(
        f"""
        UPDATE control_actions SET status = 'executed', result = $2, error = NULL, updated_at = now()
        WHERE action_id = $1 AND status = 'executing'
        RETURNING {_COLUMNS}
        """,
        action_id,
        result,
    )
    return None if row is None else action_from_row(row)


async def mark_failed(pool: Any, action_id: str, error: dict[str, Any]) -> dict[str, Any] | None:
    row = await pool.fetchrow(
        f"""
        UPDATE control_actions SET status = 'failed', error = $2, updated_at = now()
        WHERE action_id = $1 AND status = 'executing'
        RETURNING {_COLUMNS}
        """,
        action_id,
        error,
    )
    return None if row is None else action_from_row(row)


async def revoke_action(pool: Any, action_id: str) -> dict[str, Any] | None:
    """只有 `proposed/executed` 且落在 60 秒窗口内可撤销。"""
    row = await pool.fetchrow(
        f"""
        UPDATE control_actions SET status = 'revoked', updated_at = now()
        WHERE action_id = $1 AND status IN ('proposed', 'executed')
          AND updated_at > now() - make_interval(secs => $2)
        RETURNING {_COLUMNS}
        """,
        action_id,
        REVOKE_WINDOW_SECONDS,
    )
    return None if row is None else action_from_row(row)


async def get_action(pool: Any, action_id: str) -> dict[str, Any] | None:
    row = await pool.fetchrow(f"SELECT {_COLUMNS} FROM control_actions WHERE action_id = $1", action_id)
    return None if row is None else action_from_row(row)


async def list_actions(pool: Any, *, operator_id: str | None = None, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    capped = max(1, min(int(limit), 200))
    clauses: list[str] = []
    args: list[Any] = []
    if operator_id:
        args.append(operator_id)
        clauses.append(f"operator_id = ${len(args)}")
    if status:
        args.append(status)
        clauses.append(f"status = ${len(args)}")
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    args.append(capped)
    rows = await pool.fetch(
        f"SELECT {_COLUMNS} FROM control_actions{where} ORDER BY created_at DESC LIMIT ${len(args)}",
        *args,
    )
    return [action_from_row(row) for row in rows]


async def expire_actions(pool: Any) -> int:
    status = await pool.execute(
        "UPDATE control_actions SET status = 'expired', updated_at = now() WHERE status = 'proposed' AND expires_at < now()"
    )
    return _affected(status)


async def recover_stuck(pool: Any) -> int:
    """进程在 `executing` 中被重启：超时未收尾的标记失败，错误码写 `process_restart`。"""
    status = await pool.execute(
        """
        UPDATE control_actions
        SET status = 'failed',
            error = '{"code": "process_restart", "message": "执行中进程重启，结果未知"}'::jsonb,
            updated_at = now()
        WHERE status = 'executing' AND updated_at < now() - make_interval(secs => $1)
        """,
        STUCK_AFTER_SECONDS,
    )
    return _affected(status)


def _affected(command_status: str) -> int:
    """asyncpg 的 `execute()` 返回 `UPDATE n`；解析不出就按 0，不猜。"""
    try:
        return int(str(command_status).rsplit(" ", 1)[-1])
    except (TypeError, ValueError):
        return 0
