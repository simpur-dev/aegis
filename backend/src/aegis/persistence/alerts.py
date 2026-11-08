"""告警实例的 SQL 侧：去重插入 + 状态迁移，不做权限判定。

去重靠 `UNIQUE (warning_id)` + `ON CONFLICT DO NOTHING`：一条预警在多个实例上
被同时广播（NATS 每个订阅者都会收到）时，只有一个能插入成功，其余读回同一条。
这比"先查再插"可靠——两个进程同时查都会得到"不存在"。

处置类迁移全部写成 `UPDATE ... WHERE status IN (...)`：接手、转派、稍后可能被
两个人同时点，命中 0 行就是"没抢到或已终态"，调用方据此区分 404 与 409。

`title`/`hazard_type` 不存本表：真源是 `warnings`，列表查询时 LEFT JOIN 现取，
避免同一件事在两张表里各有一份文案。预警行不在（例如只注入过告警）时为空串。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

_COLUMNS = "alert_id, warning_id, level, region_code, status, ack_by, ack_at, assigned_to, snooze_until, escalated_at, drill, created_at"
_ALIASED = ", ".join(f"a.{column.strip()}" for column in _COLUMNS.split(","))
#: 还在桌上的状态。与 `domain.alert.OPEN_STATUSES` 同一口径，SQL 里必须写死字面量。
_OPEN = "('raised', 'delivered', 'acked', 'escalated')"
#: 可以接手的源状态：已确认的不再重复确认，已合并/关闭的不接受处置。
_ACTIONABLE = "('raised', 'delivered', 'escalated')"
#: 会被升级的源状态：已接手（acked）就不该再升级；演练永不升级。
_ESCALATABLE = "('raised', 'delivered')"


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def alert_from_row(row: Any, *, with_warning: bool = False) -> dict[str, Any]:
    data = {
        "alert_id": str(row["alert_id"]),
        "warning_id": str(row["warning_id"]),
        "level": int(row["level"]),
        "region_code": str(row["region_code"]),
        "status": str(row["status"]),
        "ack_by": None if row["ack_by"] is None else str(row["ack_by"]),
        "ack_at": _iso(row["ack_at"]),
        "assigned_to": None if row["assigned_to"] is None else str(row["assigned_to"]),
        "snooze_until": _iso(row["snooze_until"]),
        "escalated_at": _iso(row["escalated_at"]),
        "drill": bool(row["drill"]),
        "created_at": _iso(row["created_at"]),
    }
    if with_warning:
        # 预警行可能不在（只注入告警、或库刚建）：空串表示"未取到"，不是"没有灾害类型"。
        keys = row.keys() if hasattr(row, "keys") else ()
        data["title"] = str(row["title_zh"] or "") if "title_zh" in keys else ""
        data["hazard_type"] = str(row["hazard_type"] or "") if "hazard_type" in keys else ""
    return data


async def raise_alert(
    pool: Any,
    *,
    alert_id: str,
    warning_id: str,
    level: int,
    region_code: str,
    drill: bool,
) -> tuple[dict[str, Any], bool]:
    """落一条告警。返回 (记录, 是否新建)。

    `created=False` 表示这条预警已经有人落过账（别的实例或本实例的上一轮），
    直接返回既有记录——重复广播不改等级、不改状态，升级计时以首次为准。
    """
    row = await pool.fetchrow(
        f"""
        INSERT INTO assistant_alerts (alert_id, warning_id, level, region_code, status, drill)
        VALUES ($1, $2, $3, $4, 'raised', $5)
        ON CONFLICT (warning_id) DO NOTHING
        RETURNING {_COLUMNS}
        """,
        alert_id,
        warning_id,
        int(level),
        region_code,
        bool(drill),
    )
    if row is not None:
        return alert_from_row(row), True
    existing = await pool.fetchrow(f"SELECT {_COLUMNS} FROM assistant_alerts WHERE warning_id = $1", warning_id)
    if existing is None:  # pragma: no cover - 冲突键只有 warning_id，走不到这里
        raise RuntimeError("assistant_alerts 写入冲突且找不到原记录")
    return alert_from_row(existing), False


async def get_alert(pool: Any, alert_id: str) -> dict[str, Any] | None:
    row = await pool.fetchrow(f"SELECT {_COLUMNS} FROM assistant_alerts WHERE alert_id = $1", alert_id)
    return None if row is None else alert_from_row(row)


async def list_alerts(
    pool: Any,
    *,
    open_only: bool = True,
    status: str | None = None,
    level: int | None = None,
    drill: bool | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    capped = max(1, min(int(limit), 200))
    clauses: list[str] = []
    args: list[Any] = []
    if status:
        args.append(status)
        clauses.append(f"a.status = ${len(args)}")
    elif open_only:
        clauses.append(f"a.status IN {_OPEN}")
    if level is not None:
        args.append(int(level))
        clauses.append(f"a.level = ${len(args)}")
    if drill is not None:
        args.append(bool(drill))
        clauses.append(f"a.drill = ${len(args)}")
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    args.append(capped)
    rows = await pool.fetch(
        f"""
        SELECT {_ALIASED}, w.title_zh, w.hazard_type
        FROM assistant_alerts a
        LEFT JOIN warnings w ON w.warning_id = a.warning_id
        {where}
        ORDER BY a.level ASC, a.created_at DESC
        LIMIT ${len(args)}
        """,
        *args,
    )
    return [alert_from_row(row, with_warning=True) for row in rows]


async def ack_alert(pool: Any, alert_id: str, operator_id: str) -> dict[str, Any] | None:
    """接手：`status → acked`，同时把处置人补成操作者（已有受理人不覆盖）。"""
    row = await pool.fetchrow(
        f"""
        UPDATE assistant_alerts
        SET status = 'acked', ack_by = $2, ack_at = now(), assigned_to = COALESCE(assigned_to, $2)
        WHERE alert_id = $1 AND status IN {_ACTIONABLE}
        RETURNING {_COLUMNS}
        """,
        alert_id,
        operator_id,
    )
    return None if row is None else alert_from_row(row)


async def transfer_alert(pool: Any, alert_id: str, assignee: str) -> dict[str, Any] | None:
    """转派只改 `assigned_to`：`ack_by` 是"誰先接的手"，不能被转派抹掉。"""
    row = await pool.fetchrow(
        f"""
        UPDATE assistant_alerts SET assigned_to = $2
        WHERE alert_id = $1 AND status IN {_OPEN}
        RETURNING {_COLUMNS}
        """,
        alert_id,
        assignee,
    )
    return None if row is None else alert_from_row(row)


async def snooze_alert(pool: Any, alert_id: str, minutes: int) -> dict[str, Any] | None:
    """稍后提醒：只改 `snooze_until`，状态不变——它仍是未确认，只是暂时不弹。"""
    row = await pool.fetchrow(
        f"""
        UPDATE assistant_alerts SET snooze_until = now() + make_interval(mins => $2)
        WHERE alert_id = $1 AND status IN {_OPEN}
        RETURNING {_COLUMNS}
        """,
        alert_id,
        int(minutes),
    )
    return None if row is None else alert_from_row(row)


async def snooze_by_warning(pool: Any, warning_id: str, minutes: int) -> dict[str, Any] | None:
    """按预警静默：一条预警只有一张告警卡（UNIQUE(warning_id)），故可直接按 warning_id 迁移。

    只改 `snooze_until`，状态不变——静默是推迟升级，不是把告警关掉。
    """
    row = await pool.fetchrow(
        f"""
        UPDATE assistant_alerts SET snooze_until = now() + make_interval(mins => $2)
        WHERE warning_id = $1 AND status IN {_OPEN}
        RETURNING {_COLUMNS}
        """,
        warning_id,
        int(minutes),
    )
    return None if row is None else alert_from_row(row)


async def escalate_due(pool: Any, *, after_minutes: int) -> list[dict[str, Any]]:
    """把超时未确认的真实告警升级，返回**本实例抢到**的那些。

    多副本下每个实例都跑定时器，`UPDATE ... WHERE status='raised' RETURNING` 保证
    同一条只会被一个实例更新成功——只有 `RETURNING` 到行的实例发通知，天然幂等，
    不依赖单实例锁，也不依赖"谁是主"。
    """
    rows = await pool.fetch(
        f"""
        UPDATE assistant_alerts
        SET status = 'escalated', escalated_at = now()
        WHERE status IN {_ESCALATABLE}
          AND drill = false
          AND created_at < now() - make_interval(mins => $1)
          AND (snooze_until IS NULL OR snooze_until < now())
        RETURNING {_COLUMNS}
        """,
        int(after_minutes),
    )
    return [alert_from_row(row) for row in rows]
