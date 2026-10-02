"""预警准确率回放的 SQL 侧：真值标注表 ↔ 已落库 `warnings` 的配对。

分层规矩（与 `geo.py`/`vectors.py` 同一套）：本模块只负责**取配对**，
判定 TP/FP/FN/TN、比率与"样本不足"的口径全部住在 `replay.py` 里且只住那一处。
在 SQL 里再算一遍混淆矩阵，就等于给"预警准确率"造第二套口径——
两边不一致时没人能看出哪边在骗人。

predicted 侧的一条硬约定：**只认观测时刻之后发出的预警**。
把事件之前就已存在的预警算成命中，等于用"它本来就报了"冒充"它预报了"，
这样的准确率数字在验收现场是站不住的。
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from aegis.domain.messages import parse_iso
from aegis.persistence.errors import AccuracyArgumentError
from aegis.persistence.geo import check_window
from aegis.persistence.replay import ReplayCase, case_from_row

Connection = Any

_ALLOWED_KINDS = frozenset({"field", "synthetic", "unspecified"})

#: 现场标注一次导入允许的最大行数：超过就该分批，而不是把一次请求变成无界内存占用。
MAX_LABEL_ROWS = 5_000
#: 默认配对窗（秒）：真值时刻之后多久内的预警算作该事件的产出。
DEFAULT_WINDOW_SECONDS = 3_600
MAX_WINDOW_SECONDS = 6 * 3_600

LABEL_COLUMNS = (
    "case_id",
    "region_code",
    "hazard_type",
    "observed_at",
    "truth_warning",
    "truth_level",
    "source",
    "labelled_by",
    "note",
)


def parse_moment(raw: str, *, field: str = "time") -> datetime:
    """时间参数必须带时区：裸时间会被按本地时区解释，整个回放窗平移几小时没人看得出来。

    入口脚本与 HTTP 端点共用这一份——两处各写一个 ISO 解析，就会出现"CLI 拒的日期
    界面收下了"这种两边都对不上的口径。
    """
    text = str(raw or "").strip().replace("Z", "+00:00")
    try:
        moment = datetime.fromisoformat(text)
    except ValueError as exc:
        raise AccuracyArgumentError(f"时间参数不是合法 ISO 8601：{raw}", detail={field: raw}) from exc
    if moment.tzinfo is None:
        raise AccuracyArgumentError(f"时间参数必须带时区（如 …+00:00 或 …Z）：{raw}", detail={field: raw})
    return moment


@runtime_checkable
class SupportsAccuracyReplay(Protocol):
    """存储侧的库内回放能力：只由 Postgres 实现提供，不在内存里伪造第二套配对口径。"""

    async def accuracy_replay_cases(
        self,
        *,
        since: datetime,
        until: datetime | None = None,
        window_seconds: int = DEFAULT_WINDOW_SECONDS,
        region_code: str | None = None,
    ) -> Sequence[ReplayCase]: ...


def accuracy_replay_port(store: object) -> SupportsAccuracyReplay | None:
    """能力探测：内存 store 没有这个方法时返回 None，由调用方给出"缺什么"的回答。"""
    return store if isinstance(store, SupportsAccuracyReplay) else None


def check_lead_window(seconds: float) -> int:
    """配对窗必须是正的有限秒数且不超上限：0 窗永远配不上，超大窗会把不相干的预警算成命中。"""
    value = float(seconds)
    if value != value or value in (float("inf"), float("-inf")) or value <= 0:
        raise AccuracyArgumentError("配对窗必须为正的有限秒数", detail={"window_seconds": seconds})
    if value > MAX_WINDOW_SECONDS:
        raise AccuracyArgumentError("配对窗超出单次回放上限", detail={"window_seconds": value, "max_seconds": MAX_WINDOW_SECONDS})
    return round(value)


def normalize_label(raw: Mapping[str, Any]) -> dict[str, Any]:
    """把一条标注读成落库形状：缺字段与错类型一律响亮拒绝，不按默认值猜。"""
    case_id = str(raw.get("case_id", "")).strip()
    region_code = str(raw.get("region_code", "")).strip().upper()
    hazard_type = str(raw.get("hazard_type", "")).strip()
    if not case_id:
        raise AccuracyArgumentError("标注缺少 case_id", detail={"keys": sorted(str(key) for key in raw)})
    if not region_code:
        raise AccuracyArgumentError(f"标注 {case_id} 缺少 region_code", detail={"case_id": case_id})
    if not hazard_type:
        raise AccuracyArgumentError(f"标注 {case_id} 缺少 hazard_type", detail={"case_id": case_id})
    observed_at = raw.get("observed_at")
    if not isinstance(observed_at, datetime):
        raise AccuracyArgumentError(
            f"标注 {case_id} 的 observed_at 不是时间值", detail={"case_id": case_id, "type": type(observed_at).__name__}
        )
    if observed_at.tzinfo is None:
        raise AccuracyArgumentError(f"标注 {case_id} 的 observed_at 缺时区", detail={"case_id": case_id})
    truth = raw.get("truth_warning")
    if not isinstance(truth, bool):
        raise AccuracyArgumentError(
            f"标注 {case_id} 的 truth_warning 必须是布尔", detail={"case_id": case_id, "type": type(truth).__name__}
        )
    level = raw.get("truth_level")
    if level is not None:
        level = int(level)
        if not 1 <= level <= 5:
            raise AccuracyArgumentError(f"标注 {case_id} 的真值等级超出 1-5", detail={"truth_level": level})
    return {
        "case_id": case_id,
        "region_code": region_code,
        "hazard_type": hazard_type,
        "observed_at": observed_at,
        "truth_warning": truth,
        "truth_level": level,
        "source": str(raw.get("source", "")).strip(),
        "labelled_by": str(raw.get("labelled_by", "")).strip(),
        "note": str(raw.get("note", "")).strip(),
    }


def build_label_upsert(rows: Sequence[dict[str, Any]]) -> tuple[str, list[tuple[Any, ...]]]:
    """按 `case_id` 幂等 upsert：重复导入修标注是常态，不该撞主键也不该攒重复行。"""
    placeholders = ", ".join(f"${index + 1}" for index in range(len(LABEL_COLUMNS)))
    updates = ", ".join(f"{name} = EXCLUDED.{name}" for name in LABEL_COLUMNS if name != "case_id")
    sql = (
        f"INSERT INTO warning_truth_labels ({', '.join(LABEL_COLUMNS)})\n"
        f"VALUES ({placeholders})\n"
        f"ON CONFLICT (case_id) DO UPDATE SET {updates}\n"
    )
    values = [tuple(row[name] for name in LABEL_COLUMNS) for row in rows]
    return sql, values


def build_replay_rows(
    *, since: datetime, until: datetime | None, window_seconds: int, region_code: str | None = None
) -> tuple[str, list[Any]]:
    """真值行 + 该事件之后窗口内**第一条**预警（不限灾种，所以"报对灾种"仍可判）。"""
    check_window(since, until)
    args: list[Any] = [since, until if until is not None else datetime.max.replace(tzinfo=since.tzinfo)]
    where = ["l.observed_at >= $1", "l.observed_at < $2"]
    if region_code:
        args.append(region_code)
        where.append(f"l.region_code = ${len(args)}")
    args.append(window_seconds)
    window_param = f"${len(args)}"
    sql = (
        "SELECT l.case_id, l.region_code, l.hazard_type, l.truth_warning, l.truth_level, l.observed_at,\n"
        "       p.hazard_type AS predicted_hazard_type,\n"
        "       p.risk_level  AS predicted_level,\n"
        "       p.generated_at AS predicted_generated_at\n"
        "FROM warning_truth_labels l\n"
        "LEFT JOIN LATERAL (\n"
        "  SELECT w.hazard_type, w.risk_level, w.generated_at\n"
        "  FROM warnings w\n"
        "  WHERE (w.region_code = l.region_code OR l.region_code = ANY (w.region_codes))\n"
        f"    AND w.generated_at >= l.observed_at AND w.generated_at < l.observed_at + make_interval(secs => {window_param})\n"
        "    AND w.generated_at >= $1 AND w.generated_at < $2\n"
        "  ORDER BY w.generated_at\n"
        "  LIMIT 1\n"
        ") p ON true\n"
        f"WHERE {' AND '.join(where)}\n"
        "ORDER BY l.observed_at, l.case_id\n"
    )
    return sql, args


async def put_labels(conn: Connection, rows: Iterable[Mapping[str, Any]]) -> int:
    """导入现场标注（幂等）。空集直接返回 0：没有标注就没有回放，但不该报错。"""
    normalized = [normalize_label(row) for row in rows]
    if not normalized:
        return 0
    if len(normalized) > MAX_LABEL_ROWS:
        raise AccuracyArgumentError("单次导入行数超出上限", detail={"rows": len(normalized), "max": MAX_LABEL_ROWS})
    sql, values = build_label_upsert(normalized)
    await conn.executemany(sql, values)
    return len(normalized)


def read_label_file(path: str | Path) -> tuple[list[dict[str, Any]], str, str, str]:
    """读标注 JSONL，返回 `(行, kind, source, note)`。

    可选的 `{"dataset": {...}}` 头行与回放数据集同一约定：`kind` 决定这份数据能不能
    被判"达标"，所以它必须由数据自己声明，而不是由读它的脚本替它假定成 `field`。
    """
    file = Path(path)
    if not file.is_file():
        raise AccuracyArgumentError("标注文件不存在", detail={"path": str(file)})
    kind, source, note = "unspecified", str(file), ""
    rows: list[dict[str, Any]] = []
    for line_no, line in enumerate(file.read_text(encoding="utf-8").splitlines(), start=1):
        text = line.strip()
        if not text:
            continue
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            raise AccuracyArgumentError(f"标注文件第 {line_no} 行不是合法 JSON", detail={"line": line_no}) from exc
        if not isinstance(raw, dict):
            raise AccuracyArgumentError(f"标注文件第 {line_no} 行不是对象", detail={"line": line_no})
        if "dataset" in raw:
            meta = raw["dataset"] if isinstance(raw["dataset"], dict) else {}
            kind = str(meta.get("kind", "unspecified"))
            source = str(meta.get("source") or source)
            note = str(meta.get("note", ""))
            continue
        payload = dict(raw)
        observed = payload.get("observed_at")
        if isinstance(observed, str):
            try:
                payload["observed_at"] = parse_iso(observed)
            except ValueError as exc:
                raise AccuracyArgumentError(f"标注文件第 {line_no} 行的 observed_at 不是合法时间", detail={"line": line_no}) from exc
        rows.append(payload)
    if kind not in _ALLOWED_KINDS:
        raise AccuracyArgumentError(f"标注数据集的 kind 不合法：{kind}", detail={"allowed": sorted(_ALLOWED_KINDS)})
    return rows, kind, source, note


async def replay_rows(
    conn: Connection,
    *,
    since: datetime,
    until: datetime | None = None,
    window_seconds: int = DEFAULT_WINDOW_SECONDS,
    region_code: str | None = None,
) -> list[ReplayCase]:
    """把库里的真值与产出配成回放案例：判定口径全部交给 `replay.case_from_row`。"""
    window = check_lead_window(window_seconds)
    sql, args = build_replay_rows(since=since, until=until, window_seconds=window, region_code=region_code)
    fetched: list[ReplayCase] = []
    for index, row in enumerate(await conn.fetch(sql, *args), start=1):
        fetched.append(case_from_row(dict(row), line_no=index))
    return fetched


__all__ = [
    "DEFAULT_WINDOW_SECONDS",
    "LABEL_COLUMNS",
    "MAX_LABEL_ROWS",
    "MAX_WINDOW_SECONDS",
    "build_label_upsert",
    "build_replay_rows",
    "check_lead_window",
    "normalize_label",
    "put_labels",
    "read_label_file",
    "replay_rows",
]
