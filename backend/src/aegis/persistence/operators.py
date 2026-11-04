"""席位台账的 SQL 侧：只做读写与行转换，不做权限判定。

分层与 `suggestions.py` 一致：这里只管 SQL、参数与顺序；"谁能建席位、建完谁生效"
住在 `services/operators.py`。参数一律绑定，不拼字符串。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from aegis.domain.operator import Operator, OperatorRole

_COLUMNS = "operator_id, display_name, role, active"
MAX_LIST = 500


def operator_from_row(row: Mapping[str, Any]) -> Operator:
    return Operator(
        operator_id=str(row["operator_id"]),
        display_name=str(row["display_name"]),
        role=OperatorRole(str(row["role"])),
        active=bool(row["active"]),
    )


async def upsert_operator(pool: Any, operator: Operator) -> Operator:
    await pool.execute(
        """
        INSERT INTO operators (operator_id, display_name, role, active)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (operator_id) DO UPDATE
        SET display_name = EXCLUDED.display_name,
            role = EXCLUDED.role,
            active = EXCLUDED.active
        """,
        operator.operator_id,
        operator.display_name,
        operator.role.value,
        operator.active,
    )
    return operator


async def create_operator(pool: Any, operator: Operator) -> Operator | None:
    """只创建、不覆盖：并发建同一 ID 时只有一个成功，另一个拿到 None。

    不用 `upsert_operator` + 先查一次：那样两个请求都能通过"不存在"检查，
    后到的会把先到的那条静默改写。
    """
    row = await pool.fetchrow(
        """
        INSERT INTO operators (operator_id, display_name, role, active)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (operator_id) DO NOTHING
        RETURNING operator_id, display_name, role, active
        """,
        operator.operator_id,
        operator.display_name,
        operator.role.value,
        operator.active,
    )
    return operator_from_row(row) if row is not None else None


async def get_operator(pool: Any, operator_id: str) -> Operator | None:
    row = await pool.fetchrow(f"SELECT {_COLUMNS} FROM operators WHERE operator_id = $1", operator_id)
    return operator_from_row(row) if row is not None else None


async def list_operators(pool: Any, *, active_only: bool = True, limit: int = 200) -> list[Operator]:
    capped = max(1, min(int(limit), MAX_LIST))
    if active_only:
        rows = await pool.fetch(
            f"SELECT {_COLUMNS} FROM operators WHERE active = true ORDER BY operator_id LIMIT $1",
            capped,
        )
    else:
        rows = await pool.fetch(f"SELECT {_COLUMNS} FROM operators ORDER BY operator_id LIMIT $1", capped)
    return [operator_from_row(row) for row in rows]
