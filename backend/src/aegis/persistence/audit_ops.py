"""动作审计流水（`audit_ops`，010 建表 / 012 起按月分区）的 SQL 侧：只 INSERT 与 SELECT，没有别的路径。

为什么这个模块小得只有一句 INSERT：审计的难点不在落库而在"每条状态迁移都带一行"——
那由 `PostgresStore` 的三个迁移方法在**同一事务**里调这里保证；本模块刻意不提供
UPDATE/DELETE 代码路径，让"只追加"在代码层面无法被绕过。

读侧（P4 读面需求，设计 §14.3）：`query()` 按操作者/动作/阶段/空间/时间过滤，最近在前。
两条口径写在这里：
- **不给总数**：按时间倒序翻页时"总条数"要扫全表，而这张表按月分区、越往后越大；
- **只 SELECT**：读面自己不再产生审计行（否则每次查账都多一行，账本会被读操作本身淹没）。
"""

from __future__ import annotations

from typing import Any

_COLUMNS = (
    "action_id, phase, operator_id, role, action, space_id, target, before, after, utterance, resolved_args, idempotency_key, identity_kind"
)

#: 读面返回的列：比写面多 `id` 与 `ts`（读的人要能定位到具体哪一行、什么时候）。
_READ_COLUMNS = (
    "id, ts, action_id, phase, operator_id, role, action, space_id, target, "
    "before, after, utterance, resolved_args, idempotency_key, identity_kind"
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


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def audit_row_to_dict(row: Any) -> dict[str, Any]:
    """行 → 读面的一条。`ts` 转 ISO 字符串，其余原样（JSON 列由驱动解成 dict）。"""
    return {
        "id": int(row["id"]),
        "ts": _iso(row["ts"]),
        "action_id": str(row["action_id"]),
        "phase": str(row["phase"]),
        "operator_id": str(row["operator_id"]),
        "role": str(row["role"]),
        "action": str(row["action"]),
        "space_id": row["space_id"],
        "target": row["target"],
        "before": row["before"],
        "after": row["after"],
        "utterance": row["utterance"],
        "resolved_args": row["resolved_args"],
        "idempotency_key": row["idempotency_key"],
        "identity_kind": str(row["identity_kind"]),
    }


async def query(
    pool_or_conn: Any,
    *,
    operator_id: str | None = None,
    action: str | None = None,
    phase: str | None = None,
    space_id: str | None = None,
    action_id: str | None = None,
    since: Any = None,
    until: Any = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """按过滤条件读审计流水，最近在前。

    过滤条件用**参数绑定**（值进 `$n`），只有条件本身的片段是拼上去的固定字符串——
    拼的是列名与比较符，不含任何用户输入。
    """
    clauses: list[str] = []
    params: list[Any] = []

    def bind(value: Any) -> str:
        params.append(value)
        return f"${len(params)}"

    if operator_id:
        clauses.append(f"operator_id = {bind(operator_id)}")
    if action:
        clauses.append(f"action = {bind(action)}")
    if phase:
        clauses.append(f"phase = {bind(phase)}")
    if space_id:
        clauses.append(f"space_id = {bind(space_id)}")
    if action_id:
        clauses.append(f"action_id = {bind(action_id)}")
    if since is not None:
        clauses.append(f"ts >= {bind(since)}")
    if until is not None:
        clauses.append(f"ts <= {bind(until)}")

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    capped = max(1, min(int(limit), 200))
    skip = max(0, int(offset))
    params.append(capped)
    limit_pos = f"${len(params)}"
    params.append(skip)
    offset_pos = f"${len(params)}"

    rows = await pool_or_conn.fetch(
        f"SELECT {_READ_COLUMNS} FROM audit_ops {where} ORDER BY ts DESC, id DESC LIMIT {limit_pos} OFFSET {offset_pos}",
        *params,
    )
    return [audit_row_to_dict(row) for row in rows]
