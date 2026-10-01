"""地理查询：站点半径检索 + 灾害轨迹面的包含与计数。

距离口径取舍（本模块唯一决定）：**用 geography 而非 geometry** —— 西藏横跨约 78°–99°E、26°–37°N，
同一经度差在高原两端的地面距离相差约三成，geometry 的平面度数口径必须先按区域选投影带才能保证
"半径 5km" 真是 5km；geography 直接按椭球面算米，边端上报的经纬度不必再携带投影信息。
代价是 geography 的函数与索引略慢于 geometry，而本层查询都是"小半径 + 时窗"，
预筛后候选集很小 —— 用这点开销换掉整条投影选择链是划算的。

本模块只做 SQL 构造与执行，不含业务规则：命中之后做什么，由调用方决定。
占位符一律按 `args` 追加顺序编号（`${len(args) + 1}`），值永远不进 SQL 文本 —— 见注入防护测试。
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from aegis.persistence.errors import GeoArgumentError

MAX_RADIUS_M = 500_000.0
MAX_LIMIT = 1_000
_POLYGON_HEADS = frozenset({"POLYGON", "MULTIPOLYGON"})

STATION_FIELDS = "station_id, name_zh, region_code, hazard_focus, elevation_m"

# 进程内连接句柄：asyncpg.Connection / Pool 都只需 fetch，不做类型绑定以免引入驱动依赖
Connection = Any


def check_point(lon: float, lat: float) -> tuple[float, float]:
    if not (math.isfinite(lon) and math.isfinite(lat)):
        raise GeoArgumentError("经纬度必须是有限数值", detail={"lon": lon, "lat": lat})
    if not -180.0 <= lon <= 180.0:
        raise GeoArgumentError("经度越界", detail={"lon": lon})
    if not -90.0 <= lat <= 90.0:
        raise GeoArgumentError("纬度越界", detail={"lat": lat})
    return lon, lat


def check_radius(radius_m: float) -> float:
    """半径必须是正实数：0 退化为"恰好重合"、负数在 geography 口径下无意义，都不如直接拒绝。"""
    if not math.isfinite(radius_m) or radius_m <= 0.0:
        raise GeoArgumentError("半径必须为正的有限米数", detail={"radius_m": radius_m})
    if radius_m > MAX_RADIUS_M:
        raise GeoArgumentError("半径超出单次查询上限", detail={"radius_m": radius_m, "max_m": MAX_RADIUS_M})
    return float(radius_m)


def check_limit(limit: int) -> int:
    if limit <= 0:
        raise GeoArgumentError("limit 必须为正", detail={"limit": limit})
    if limit > MAX_LIMIT:
        raise GeoArgumentError("limit 超出上限", detail={"limit": limit, "max": MAX_LIMIT})
    return int(limit)


def check_polygon(wkt: str) -> str:
    text = (wkt or "").strip()
    head, _, rest = text.upper().partition("(")
    if head not in _POLYGON_HEADS or not (rest.endswith(")") or rest.endswith("))")):
        raise GeoArgumentError("轨迹面必须是 POLYGON/MULTIPOLYGON 的 WKT", detail={"wkt": text[:64]})
    return text


def check_window(since: datetime, until: datetime | None) -> None:
    if since.tzinfo is None or (until is not None and until.tzinfo is None):
        raise GeoArgumentError("时间窗必须带时区")
    if until is not None and until <= since:
        raise GeoArgumentError("时间窗逆序或为空窗：until 必须晚于 since", detail={"since": str(since), "until": str(until)})


def point_text(lon: float, lat: float) -> str:
    """绑定值用的 WGS84 点文本（经度在前，OGC 口径）。"""
    return f"SRID=4326;POINT({lon:.6f} {lat:.6f})"


# ---------- SQL 构造 ----------


def build_stations_within(*, lon: float, lat: float, radius_m: float, limit: int, region_code: str | None = None) -> tuple[str, list[Any]]:
    check_point(lon, lat)
    radius = check_radius(radius_m)
    check_limit(limit)
    args: list[Any] = [point_text(lon, lat), radius]
    where = ["geom IS NOT NULL", "ST_DWithin(geom, ST_GeogFromText($1), $2)"]
    if region_code:
        where.append(f"region_code = ${len(args) + 1}")
        args.append(region_code)
    args.append(limit)
    sql = (
        f"SELECT {STATION_FIELDS}, ST_Distance(geom, ST_GeogFromText($1)) AS distance_m\n"
        f"FROM monitoring_stations\n"
        f"WHERE {' AND '.join(where)}\n"
        f"ORDER BY distance_m ASC\n"
        f"LIMIT ${len(args)}\n"
    )
    return sql, args


def build_stations_in_polygon(*, polygon_wkt: str, region_code: str | None = None) -> tuple[str, list[Any]]:
    args: list[Any] = [check_polygon(polygon_wkt)]
    where = ["geom IS NOT NULL", "ST_Covers(ST_GeogFromText($1), geom)"]
    if region_code:
        where.append(f"region_code = ${len(args) + 1}")
        args.append(region_code)
    sql = f"SELECT {STATION_FIELDS}\nFROM monitoring_stations\nWHERE {' AND '.join(where)}\nORDER BY station_id\n"
    return sql, args


def build_stations_list(*, region_code: str | None = None, limit: int = MAX_LIMIT) -> tuple[str, list[Any]]:
    """站点维表清单：与半径/面查询不同，这里不带任何距离条件，唯一要求是 `station_id` 稳定升序。

    坐标是这一路的重点，所以必须把 `geom` 显式取回来（`STATION_FIELDS` 那四条查询不带坐标，
    它们只回答"哪些站在范围内"）。geom 是 geography，取经纬度与 EWKT 都要先转 geometry。
    """
    check_limit(limit)
    args: list[Any] = []
    where = "1=1"
    if region_code:
        args.append(region_code)
        where = f"region_code = ${len(args)}"
    args.append(limit)
    sql = (
        f"SELECT {STATION_FIELDS},\n"
        f"       ST_X(geom::geometry) AS lon,\n"
        f"       ST_Y(geom::geometry) AS lat,\n"
        f"       ST_AsEWKT(geom::geometry) AS geom\n"
        f"FROM monitoring_stations\n"
        f"WHERE {where}\n"
        f"ORDER BY station_id\n"
        f"LIMIT ${len(args)}\n"
    )
    return sql, args


def build_trace_reading_count(*, polygon_wkt: str, since: datetime, until: datetime | None) -> tuple[str, list[Any]]:
    """轨迹面内的读数计数。

    遥测按观测量（雨量/泥位）而非灾种建模，故此处不接灾种过滤：灾种过滤只作用于预警计数。
    """
    check_window(since, until)
    args: list[Any] = [check_polygon(polygon_wkt), since]
    where = ["s.geom IS NOT NULL", "ST_Covers(ST_GeogFromText($1), s.geom)", "t.observed_at >= $2"]
    if until is not None:
        where.append(f"t.observed_at < ${len(args) + 1}")
        args.append(until)
    sql = f"SELECT count(*) AS n\nFROM telemetry_readings t\nJOIN monitoring_stations s USING (station_id)\nWHERE {' AND '.join(where)}\n"
    return sql, args


def build_trace_warning_count(
    *, polygon_wkt: str, since: datetime, until: datetime | None, hazard_type: str | None
) -> tuple[str, list[Any]]:
    check_window(since, until)
    args: list[Any] = [check_polygon(polygon_wkt), since]
    where = ["w.generated_at >= $2"]
    if until is not None:
        where.append(f"w.generated_at < ${len(args) + 1}")
        args.append(until)
    if hazard_type:
        where.append(f"w.hazard_type = ${len(args) + 1}")
        args.append(hazard_type)
    sql = (
        "SELECT count(*) AS n, coalesce(array_agg(w.warning_id), '{}') AS warning_ids\n"
        "FROM warnings w\n"
        "WHERE w.region_code IN (\n"
        "  SELECT region_code FROM monitoring_stations s WHERE s.geom IS NOT NULL AND ST_Covers(ST_GeogFromText($1), s.geom)\n"
        ")\n"
        f"  AND {' AND '.join(where)}\n"
    )
    return sql, args


# ---------- 执行 ----------


async def stations_within(
    conn: Connection,
    *,
    lon: float,
    lat: float,
    radius_m: float,
    limit: int = 50,
    region_code: str | None = None,
) -> list[dict[str, Any]]:
    """半径内的站点，按距离升序；空候选集返回空表而不是抛错。"""
    sql, args = build_stations_within(lon=lon, lat=lat, radius_m=radius_m, limit=limit, region_code=region_code)
    return [dict(row) for row in await conn.fetch(sql, *args)]


async def stations_in_polygon(conn: Connection, *, polygon_wkt: str, region_code: str | None = None) -> list[dict[str, Any]]:
    sql, args = build_stations_in_polygon(polygon_wkt=polygon_wkt, region_code=region_code)
    return [dict(row) for row in await conn.fetch(sql, *args)]


async def stations_list(conn: Connection, *, region_code: str | None = None, limit: int = MAX_LIMIT) -> list[dict[str, Any]]:
    sql, args = build_stations_list(region_code=region_code, limit=limit)
    return [dict(row) for row in await conn.fetch(sql, *args)]


async def trace_summary(
    conn: Connection,
    *,
    polygon_wkt: str,
    since: datetime,
    until: datetime | None = None,
    hazard_type: str | None = None,
) -> dict[str, Any]:
    """灾害轨迹面汇总：面内站点、面内读数条数、面内预警条数。

    三条查询**必须串行**：调用方给的是池里的同一条连接，而 asyncpg 的 Connection
    不允许并发操作（并发 gather 会抛 InterfaceError: another operation is in progress）。
    要并发得各查各借一条连接，那属于调用层的连接编排，不在本查询函数职责内。
    """
    stations_sql, stations_args = build_stations_in_polygon(polygon_wkt=polygon_wkt)
    readings_sql, readings_args = build_trace_reading_count(polygon_wkt=polygon_wkt, since=since, until=until)
    warnings_sql, warnings_args = build_trace_warning_count(polygon_wkt=polygon_wkt, since=since, until=until, hazard_type=hazard_type)
    stations_rows = await conn.fetch(stations_sql, *stations_args)
    readings_row = await conn.fetchrow(readings_sql, *readings_args)
    warnings_row = await conn.fetchrow(warnings_sql, *warnings_args)
    return {
        "polygon": polygon_wkt,
        "stations": [dict(row) for row in stations_rows],
        "station_count": len(stations_rows),
        "readings": int(readings_row["n"]),
        "warnings": int(warnings_row["n"]),
        "warning_ids": list(warnings_row["warning_ids"]),
    }
