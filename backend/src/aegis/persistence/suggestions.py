"""阈值建议台账的 SQL 侧，与"人工确认 → 规则库新版本"这一笔事务（§3.5 后半截）。

`rulebook.py` 的开头就写着"写路径不在这里：版本生效/退役必须经人工审核"。本模块就是那句
审核的落点：**唯一**能把 `trigger_rules` 改一版的入口，必须带签字人、带 before/after，
并且与"这条建议已销账"落在同一个事务里——分两笔写就会出现"库里的阈值变了，而台账上还挂着
一条待确认建议"，那正是审计最不想看到的形状。

分层照旧与 `accuracy.py`/`vectors.py` 一致：这里只管 SQL、参数与事务边界；
"要不要重载引擎、怎么向外显"住在 `services/threshold_suggestions.py`。
参数一律绑定，不拼字符串：这张表连着判据，注入成功的表现是预警等级被人改掉。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from aegis.persistence.errors import PersistenceError

#: 与建议生产侧 (`agents/feedback.py`) 同名字面量：状态只有三种，第四种是拼错了。
SUGGESTION_STATUSES = ("pending_human_review", "confirmed", "rejected")
PENDING = "pending_human_review"

SUGGESTION_COLUMNS = (
    "suggestion_id",
    "trace_id",
    "warning_id",
    "region_code",
    "hazard_type",
    "suggestion",
    "attribution",
    "lag_stage",
    "effect_score",
    "status",
    "created_at",
    "rule_id",
    "decided_by",
    "decided_at",
    "before_value",
    "after_value",
    "new_version",
    "decision_note",
)

#: 一次读最多返回多少条：待确认清单是给值班员看的，不是给报表全表扫的。
MAX_LIST = 500
_READ_PREFIX = f"SELECT {', '.join(SUGGESTION_COLUMNS)} FROM threshold_suggestions"
#: `suggestion` 与 `attribution` 的字数上限，与智能体那侧的 max_length 同口径。
MAX_TEXT = 500


class SuggestionArgumentError(ValueError):
    """调用方参数不合法：还没触达数据库就该拒掉（与 `RulebookArgumentError` 同形状）。"""

    def __init__(self, message: str, *, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.detail: dict[str, Any] = detail or {}


class SuggestionConflictError(PersistenceError):
    """这一笔裁决的前提已经不成立：建议被人先裁了，或规则不是提交者看到的那一版。

    映射成 409 而不是 400——调用方参数没错，错在"它依据的现场已经不是现在这个现场"。
    """


def check_status(status: str) -> str:
    value = str(status or "").strip().lower()
    if value not in SUGGESTION_STATUSES:
        raise SuggestionArgumentError(f"status 只允许 {list(SUGGESTION_STATUSES)}", detail={"status": status})
    return value


def check_signer(decided_by: str) -> str:
    """裁决必须署名：接口不替人编一个 "unknown" 顶上去（与 D9 核签那条同源）。"""
    return _text(decided_by, field="decided_by", required=True)


def check_limit(limit: int) -> int:
    if not 1 <= int(limit) <= MAX_LIST:
        raise SuggestionArgumentError(f"limit 取值区间为 1..{MAX_LIST}", detail={"limit": limit})
    return int(limit)


def _text(value: Any, *, field: str, required: bool = False) -> str:
    text = str(value if value is not None else "").strip()
    if required and text == "":
        raise SuggestionArgumentError(f"{field} 不能为空", detail={field: value})
    return text[:MAX_TEXT]


def build_insert(row: Mapping[str, Any]) -> tuple[str, list[Any]]:
    columns = (
        "suggestion_id",
        "trace_id",
        "warning_id",
        "region_code",
        "hazard_type",
        "suggestion",
        "attribution",
        "lag_stage",
        "effect_score",
        "status",
    )
    params: list[Any] = [row[name] for name in columns]
    placeholders = ", ".join(f"${index}" for index in range(1, len(params) + 1))
    sql = f"INSERT INTO threshold_suggestions ({', '.join(columns)}) VALUES ({placeholders})"
    return sql, params


def build_select(*, status: str, limit: int) -> tuple[str, list[Any]]:
    params: list[Any] = []
    where = ""
    if status != "all":
        params.append(check_status(status))
        where = f" WHERE status = ${len(params)}"
    params.append(check_limit(limit))
    sql = f"{_READ_PREFIX}{where} ORDER BY created_at DESC, suggestion_id ASC LIMIT ${len(params)}"
    return sql, params


def build_read_one() -> str:
    return f"{_READ_PREFIX} WHERE suggestion_id = $1"


#: 与 `build_read_one` 同一份列，只是加了行锁：读法两套会漂移，锁不加就并发打架。
_READ_ONE_LOCK = f"{_READ_PREFIX} WHERE suggestion_id = $1 FOR UPDATE"


def parse_conditions(raw: Any) -> list[dict[str, Any]]:
    """把 `trigger_rules.conditions` 读成对象数组。

    jsonb 编解码器已注册（`postgres.py` 建连时挂的 orjson），正常就是 list；
    留一条 str 分支是因为手工导入/旧驱动可能给文本，而"解不出来"必须说清是哪一列，
    不能让它变成一句 `TypeError: string indices must be integers`。
    """
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError as exc:
            raise SuggestionArgumentError("规则的条件载荷解不开 JSON", detail={"conditions": raw[:120]}) from exc
    if not isinstance(raw, list) or not all(isinstance(item, dict) for item in raw):
        raise SuggestionArgumentError("规则的条件载荷不是对象数组，无法按指标改阈值", detail={"conditions": str(raw)[:120]})
    return [dict(item) for item in raw]


def apply_threshold(
    conditions: Sequence[Mapping[str, Any]],
    *,
    metric: str,
    threshold: float,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """把规则条件里名为 `metric` 的那一条的阈值换成 `threshold`，返回 (新条件, before 值)。

    三条拒绝都是刻意的：
    - 指标不在这条规则里 → 拒。放过它就等于往规则里塞一条从没有过的条件，
      而报表上它和"真的调过阈值"长得一模一样；
    - 同名指标出现两次 → 拒。改哪一条不该由数组顺序决定；
    - 阈值不是数或为负 → 拒（判据值不该由字符串悄悄表示）。
    """
    name = _text(metric, field="metric", required=True)
    try:
        value = float(threshold)
    except (TypeError, ValueError) as exc:
        raise SuggestionArgumentError("threshold 必须是数", detail={"threshold": threshold}) from exc
    if value < 0:
        raise SuggestionArgumentError("threshold 不应为负（现有指标的判据值都是非负量级）", detail={"threshold": threshold})
    matched = [index for index, item in enumerate(conditions) if str(item.get("metric")) == name]
    if not matched:
        available = [str(item.get("metric")) for item in conditions]
        raise SuggestionArgumentError(f"这条规则的条件里没有指标 {name}", detail={"metric": name, "available": available})
    if len(matched) > 1:
        raise SuggestionArgumentError(f"指标 {name} 在这条规则里出现 {len(matched)} 次，改哪一条不该靠顺序决定", detail={"metric": name})
    index = matched[0]
    before = dict(conditions[index])
    updated = [dict(item) for item in conditions]
    updated[index]["threshold"] = value
    return updated, before


async def put_suggestion(conn: Any, row: Mapping[str, Any]) -> dict[str, Any]:
    """写一条建议并**从库里读回**：调用方要拿到的是库里的形状，不是它自己拼的那个 dict。"""
    sql, params = build_insert(row)
    await conn.execute(sql, *params)
    record = await conn.fetchrow(build_read_one(), row["suggestion_id"])
    if record is None:
        raise PersistenceError("建议写入后读不到", detail={"suggestion_id": row["suggestion_id"]})
    return dict(record)


async def fetch_suggestions(conn: Any, *, status: str = PENDING, limit: int = 100) -> list[dict[str, Any]]:
    sql, params = build_select(status=status, limit=limit)
    records = await conn.fetch(sql, *params)
    return [dict(record) for record in records]


async def fetch_one(conn: Any, suggestion_id: str) -> dict[str, Any] | None:
    record = await conn.fetchrow(build_read_one(), _text(suggestion_id, field="suggestion_id", required=True))
    return None if record is None else dict(record)


async def lock_and_read(conn: Any, suggestion_id: str) -> dict[str, Any] | None:
    """事务内锁住这一行再读：两个人同时点"确认"时，后一个必须看见前一个已经改过的状态。

    没有这把锁，两次确认会各自读到"待确认"、各自插入一个新版本，而 `trigger_rules`
    的部分唯一索引只会让第二笔炸成一条看不懂的数据库异常——冲突要用人话说出来。
    """
    record = await conn.fetchrow(_READ_ONE_LOCK, _text(suggestion_id, field="suggestion_id", required=True))
    return None if record is None else dict(record)


async def require_pending(conn: Any, suggestion_id: str) -> dict[str, Any]:
    """确认与驳回共用的前置：这条建议必须存在，而且还没被裁过。

    两处各写一份 if 就会各说一句话，而"没这条建议"(422) 与"已经裁过了"(409) 是两种事实——
    客户端要能分辨自己该改参数还是该重新拉清单。
    """
    pending = await lock_and_read(conn, suggestion_id)
    if pending is None:
        raise SuggestionArgumentError("没有这条阈值建议", detail={"suggestion_id": suggestion_id})
    if pending["status"] != PENDING:
        raise SuggestionConflictError("这条建议已经被裁决过了", detail={"suggestion_id": suggestion_id, "status": pending["status"]})
    return pending


#: 退役旧 active 版本 + 插入新 active 版本 + 销账，三条语句一起进事务。
LOCK_ACTIVE_RULE = (
    "SELECT rule_id, version, status, hazard_type, description, mode, triggered_level, weight, conditions, calibration_basis"
    " FROM trigger_rules WHERE rule_id = $1 AND status = 'active' FOR UPDATE"
)
_RETIRE_ACTIVE = (
    "UPDATE trigger_rules SET status = 'retired', note = concat(note, ' | 被 v', $2::text, ' 取代')"
    " WHERE rule_id = $1 AND status = 'active'"
)
_INSERT_VERSION = (
    "INSERT INTO trigger_rules (rule_id, version, status, hazard_type, description, mode, triggered_level,"
    " weight, conditions, calibration_basis, reviewer, activated_at, note)"
    " VALUES ($1, $2, 'active', $3, $4, $5, $6, $7, $8::jsonb, $9, $10, now(), $11)"
)
#: 两条裁决语句都带 `AND status = 'pending_human_review'`：这是"一条建议只裁一次"的应用侧执行点。
_DECIDE = (
    "UPDATE threshold_suggestions SET status = $2, decided_by = $3, decided_at = now(), decision_note = $4,"
    " rule_id = $5, before_value = $6::jsonb, after_value = $7::jsonb, new_version = $8"
    " WHERE suggestion_id = $1 AND status = 'pending_human_review'"
)
_REJECT = (
    "UPDATE threshold_suggestions SET status = 'rejected', decided_by = $2, decided_at = now(), decision_note = $3"
    " WHERE suggestion_id = $1 AND status = 'pending_human_review'"
)


def build_decide_statements(
    *,
    suggestion_id: str,
    decided_by: str,
    note: str,
    active: Mapping[str, Any],
    new_conditions: Sequence[Mapping[str, Any]],
    before: Mapping[str, Any],
    threshold: float,
) -> list[tuple[str, list[Any]]]:
    """产出确认这一笔的三条语句（纯函数：形状与参数可以在不连库的情况下断言）。"""
    new_version = int(active["version"]) + 1
    basis = f"人工确认阈值建议 {suggestion_id}（原阈值 {before.get('threshold')} → {threshold}，指标 {before.get('metric')}）"
    after = {"metric": before.get("metric"), "threshold": threshold, "from_version": int(active["version"])}
    before_value = {"metric": before.get("metric"), "threshold": before.get("threshold"), "version": int(active["version"])}
    return [
        # 退役语句里那个版本号出现在 `concat(... $2::text ...)` 里，asyncpg 因此把 $2 认成 text：
        # 传 int 会在驱动层就报 DataError。版本号本身在插入语句里仍是整数，这里只是拼接文案。
        (_RETIRE_ACTIVE, [active["rule_id"], str(new_version)]),
        (
            _INSERT_VERSION,
            [
                active["rule_id"],
                new_version,
                active["hazard_type"],
                active["description"],
                active["mode"],
                int(active["triggered_level"]),
                float(active["weight"]),
                # jsonb 三格都传 **Python 对象**：建连时注册的 orjson 编解码器负责序列化。
                # 自己先 `json.dumps` 反而错——编解码器会把那串文本再包一层引号，
                # 存进去就成了 `jsonb_typeof='string'` 的标量，被库的 CHECK 当场拒。
                [dict(item) for item in new_conditions],
                basis[:MAX_TEXT],
                decided_by,
                note[:MAX_TEXT],
            ],
        ),
        (
            _DECIDE,
            [
                suggestion_id,
                "confirmed",
                decided_by,
                note[:MAX_TEXT],
                active["rule_id"],
                before_value,
                after,
                new_version,
            ],
        ),
    ]


async def reject(conn: Any, *, suggestion_id: str, decided_by: str, note: str) -> dict[str, Any] | None:
    """驳回：只有还挂着"待确认"的建议能被动一次，第二次匹配 0 行 → 读不回新状态就是冲突。"""
    sql, params = build_reject(suggestion_id=suggestion_id, decided_by=decided_by, note=note)
    return await execute_decide(conn, sql, params, suggestion_id=suggestion_id)


def build_reject(*, suggestion_id: str, decided_by: str, note: str) -> tuple[str, list[Any]]:
    signer = _text(decided_by, field="decided_by", required=True)
    return _REJECT, [suggestion_id, signer, _text(note, field="note")]


async def execute_decide(conn: Any, sql: str, params: Sequence[Any], *, suggestion_id: str) -> dict[str, Any] | None:
    """执行一条裁决语句并把结果读回来。

    `UPDATE ... WHERE status = 'pending_human_review'` 匹配 0 行时 asyncpg 只回一个
    "UPDATE 0" 的标签、不回行——所以这里读回一次：读到的状态仍然挂着待确认，
    就等于这一票没落地，调用方据此报冲突，而不是把"没改成"当成"改好了"。
    """
    await conn.execute(sql, *params)
    row = await fetch_one(conn, suggestion_id)
    if row is None or row["status"] == PENDING:
        return None
    return row
