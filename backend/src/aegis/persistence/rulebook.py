"""触发条件规则库的读路径（完善计划批次 C2）。

写路径**不在这里**：版本生效/退役必须经人工审核（C3 的标定报表只出建议、不自动改库），
所以本模块只提供带过滤的 SELECT。参数一律绑定，不拼字符串——规则库和真值表一样，
一旦被注入就是判据被人改掉，那不是"查询出错"而是"预警被操纵"。
"""

from __future__ import annotations

from typing import Any

RULE_COLUMNS = (
    "rule_id",
    "version",
    "status",
    "hazard_type",
    "description",
    "mode",
    "triggered_level",
    "weight",
    "conditions",
    "calibration_basis",
    "reviewer",
    "created_at",
    "activated_at",
)

#: 过滤取值与 SQL 的 CHECK 同集合：写错的状态名不该返回空集（看起来像"库里没有规则"），
#: 而是当场拒绝。
STATUS_FILTERS = ("draft", "active", "retired", "all")
MAX_LIMIT = 500


class RulebookArgumentError(ValueError):
    """调用方参数不合法：还没触达数据库就该拒掉。

    带 `detail` 是为了与持久层其它参数错误同形状（HTTP 层按同一份结构外显原因）。
    """

    def __init__(self, message: str, *, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.detail: dict[str, Any] = detail or {}


def check_limit(limit: int) -> int:
    if not 1 <= int(limit) <= MAX_LIMIT:
        raise RulebookArgumentError(f"limit 取值区间为 1..{MAX_LIMIT}", detail={})
    return int(limit)


def check_status(status: str) -> str:
    value = str(status or "").strip().lower()
    if value not in STATUS_FILTERS:
        raise RulebookArgumentError(f"status 只允许 {list(STATUS_FILTERS)}", detail={"status": status})
    return value


def build_query(*, status: str, hazard_type: str | None, limit: int) -> tuple[str, list[Any]]:
    """产出 (SQL, 参数)。单独成函数是为了让"过滤条件到底长什么样"能被直接断言，而不必连库。"""
    clauses: list[str] = []
    params: list[Any] = []
    if status != "all":
        params.append(status)
        clauses.append(f"status = ${len(params)}")
    if hazard_type:
        params.append(hazard_type)
        clauses.append(f"hazard_type = ${len(params)}")
    params.append(check_limit(limit))
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"SELECT {', '.join(RULE_COLUMNS)} FROM trigger_rules{where} ORDER BY rule_id ASC, version DESC LIMIT ${len(params)}"
    return sql, params


async def fetch_rules(conn: Any, *, status: str = "active", hazard_type: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    """按状态读规则行。空表就是空列表——是不是"库里还没种过规则"由调用方判断并外显。"""
    sql, params = build_query(status=check_status(status), hazard_type=hazard_type, limit=limit)
    records = await conn.fetch(sql, *params)
    return [_as_dict(record) for record in records]


def _as_dict(record: Any) -> dict[str, Any]:
    row = dict(record)
    conditions = row.get("conditions")
    if conditions is not None and not isinstance(conditions, (list, dict, str)):
        # asyncpg 把 jsonb 解成 Python 对象；非预期形状原样交给装配层报错，不在这里圆场
        row["conditions"] = str(conditions)
    return row
