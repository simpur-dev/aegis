"""处置空间/任务的持久层：幂等 upsert + 带 version 的原子状态迁移。

为什么每个迁移都写成 `UPDATE ... WHERE version = $n AND status IN (...) RETURNING`：
多人协同处置时，两个人可能同时认领同一条任务，或一个人认领、另一个人同时在转派。
"先查后改"会让两个请求都通过；命中 0 行就是"没抢到"，上层据此返回 409，
而不是把别人刚写的结果覆盖掉。`version = version + 1` 与条件里的 version 同一个语句，
保证并发下版本单调递增。
"""

from __future__ import annotations

from typing import Any

_COLUMNS = "task_id, space_id, stu_id, assignee, status, version, updated_at"
_SPACE_COLUMNS = "space_id, warning_id, status, created_at, closed_at"
_OPEN = "('pending', 'claimed', 'in_progress', 'acked')"


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def task_from_row(row: Any) -> dict[str, Any]:
    return {
        "task_id": row["task_id"],
        "space_id": row["space_id"],
        "stu_id": row["stu_id"],
        "assignee": row["assignee"],
        "status": row["status"],
        "version": int(row["version"]),
        "updated_at": _iso(row["updated_at"]),
    }


def space_from_row(row: Any) -> dict[str, Any]:
    return {
        "space_id": row["space_id"],
        "warning_id": row["warning_id"],
        "status": row["status"],
        "created_at": _iso(row["created_at"]),
        "closed_at": _iso(row["closed_at"]),
    }


async def ensure_space(pool: Any, *, space_id: str, warning_id: str) -> dict[str, Any]:
    """一条预警一个空间。space_id 与 warning_id 同值是应用层约定，不是 DB 约束。"""
    row = await pool.fetchrow(
        f"""
        INSERT INTO handling_spaces (space_id, warning_id, status)
        VALUES ($1, $2, 'open')
        ON CONFLICT DO NOTHING
        RETURNING {_SPACE_COLUMNS}
        """,
        space_id,
        warning_id,
    )
    if row is None:
        row = await pool.fetchrow(f"SELECT {_SPACE_COLUMNS} FROM handling_spaces WHERE space_id = $1", space_id)
    return space_from_row(row)


async def ensure_task(pool: Any, *, space_id: str, stu_id: str) -> tuple[dict[str, Any], bool]:
    """同一空间同一 STU 只建一条任务：`UNIQUE (space_id, stu_id)` 兜住重复落库。"""
    row = await pool.fetchrow(
        f"""
        INSERT INTO handling_tasks (task_id, space_id, stu_id, status, version)
        VALUES ($1, $2, $3, 'pending', 1)
        ON CONFLICT (space_id, stu_id) DO NOTHING
        RETURNING {_COLUMNS}
        """,
        stu_id,
        space_id,
        stu_id,
    )
    if row is not None:
        return task_from_row(row), True
    existing = await pool.fetchrow(f"SELECT {_COLUMNS} FROM handling_tasks WHERE space_id = $1 AND stu_id = $2", space_id, stu_id)
    return task_from_row(existing), False


async def get_task(pool: Any, task_id: str) -> dict[str, Any] | None:
    row = await pool.fetchrow(f"SELECT {_COLUMNS} FROM handling_tasks WHERE task_id = $1", task_id)
    return None if row is None else task_from_row(row)


async def list_tasks(
    pool: Any,
    *,
    space_id: str | None = None,
    assignee: str | None = None,
    status: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    args: list[Any] = []
    if space_id:
        args.append(space_id)
        clauses.append(f"space_id = ${len(args)}")
    if assignee:
        args.append(assignee)
        clauses.append(f"assignee = ${len(args)}")
    if status:
        args.append(status)
        clauses.append(f"status = ${len(args)}")
    where = " AND ".join(clauses) if clauses else "TRUE"
    args.append(max(1, min(int(limit), 200)))
    rows = await pool.fetch(f"SELECT {_COLUMNS} FROM handling_tasks WHERE {where} ORDER BY updated_at DESC LIMIT ${len(args)}", *args)
    return [task_from_row(row) for row in rows]


async def _move(
    pool: Any,
    task_id: str,
    *,
    expected_version: int,
    allowed: str,
    status: str | None = None,
    assignee: str | None = None,
) -> dict[str, Any] | None:
    sets = ["version = version + 1", "updated_at = now()"]
    args: list[Any] = [task_id, int(expected_version)]
    if status is not None:
        args.append(status)
        sets.append(f"status = ${len(args)}")
    if assignee is not None:
        args.append(assignee)
        sets.append(f"assignee = ${len(args)}")
    row = await pool.fetchrow(
        f"""
        UPDATE handling_tasks SET {", ".join(sets)}
        WHERE task_id = $1 AND version = $2 AND status IN {allowed}
        RETURNING {_COLUMNS}
        """,
        *args,
    )
    return None if row is None else task_from_row(row)


async def claim_task(pool: Any, task_id: str, operator_id: str, expected_version: int) -> dict[str, Any] | None:
    return await _move(pool, task_id, expected_version=expected_version, allowed="('pending')", status="claimed", assignee=operator_id)


async def ack_task(pool: Any, task_id: str, operator_id: str, expected_version: int) -> dict[str, Any] | None:
    return await _move(pool, task_id, expected_version=expected_version, allowed="('claimed', 'in_progress')", status="acked")


async def assign_task(pool: Any, task_id: str, assignee: str, expected_version: int) -> dict[str, Any] | None:
    return await _move(pool, task_id, expected_version=expected_version, allowed=_OPEN, status="claimed", assignee=assignee)


async def transfer_task(pool: Any, task_id: str, assignee: str, expected_version: int) -> dict[str, Any] | None:
    """转派只换责任人，状态不变：它不该把"进行中"悄悄打回"待认领"。"""
    return await _move(pool, task_id, expected_version=expected_version, allowed=_OPEN, assignee=assignee)


async def close_task(pool: Any, task_id: str, expected_version: int) -> dict[str, Any] | None:
    return await _move(pool, task_id, expected_version=expected_version, allowed=_OPEN, status="closed")
