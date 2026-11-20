"""外部小时降水取数脚本：Open-Meteo archive API → 站点逐小时 CSV（运行态目录）。

**它解决的是哪一个未清项**：`rainfall_mm_h`（小时雨强）此前没有任何真实源——
逐日格点只能给 24 小时累计，"日累计不可近似折算"写死在派生表的 UNMAPPED_REASONS 里。
Open-Meteo archive API 的 `hourly=precipitation` 就是逐小时累计（mm，即 mm/h），
**小时口径第一次有了真数据**。

**许可与条款**：Open-Meteo 数据 **CC BY 4.0**（引用要求见
<https://open-meteo.com/en/license>：非商业用途免费、商用需订阅，注明出处即可）。
API 匿名可调；本脚本按站逐个请求、每次请求前停 `--sleep 0.3s`，不给免费口子加压。

**为什么落运行态目录**：取数结果是按时间的滚动实况（每次跑都会更新），不是
可再分发的固定数据集——与 CHM_PRE V2 同一条纪律：数据本体不进 git，平台只读
派生/合并后的 CSV（`backend/data/precip/openmeteo/`，路径可用 `--dest` 改）。

**幂等合并**：CSV 按 `(datetime_utc, station_id)` 去重，旧值保留、新值不覆盖
（同一小时重复取数以先到的为准——archive 的历史值不变，重跑只补缺）。

用法：
    python scripts/fetch_precip_hourly_openmeteo.py                       # 站点表 + 默认参数
    python scripts/fetch_precip_hourly_openmeteo.py --days 7              # 取最近 7 天
    python scripts/fetch_precip_hourly_openmeteo.py --stations path.csv --dest path
    python scripts/fetch_precip_hourly_openmeteo.py --dry-run             # 只打印将请求的 URL

站点表 CSV 必须有列：`station_id, region_code, latitude, longitude`
（与 `scripts/import_stations.py` / 逐日派生脚本同一份站点口径）。
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DEST = REPO_ROOT / "backend" / "data" / "precip" / "openmeteo"
#: 站点表默认从逐日派生链的同一个位置取（运行态目录），没有就要求显式给。
DEFAULT_STATIONS = REPO_ROOT / "backend" / "data" / "precip" / "stations.csv"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
USER_AGENT = "aegis-precip-hourly-fetch/1.0"
#: 输出 CSV 的列契约（连接器 `PrecipHourlySource._REQUIRED_COLUMNS` 同一套）。
OUT_COLUMNS = ("datetime_utc", "station_id", "region_code", "precip_mm_h")


@dataclass(frozen=True, slots=True)
class Station:
    station_id: str
    region_code: str
    latitude: float
    longitude: float


def load_stations(path: Path) -> list[Station]:
    """读站点表；缺列/缺值/坐标越界逐行报错——站点口径坏了就该当场停，不静默跳过。"""
    required = ("station_id", "region_code", "latitude", "longitude")
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = [column for column in required if column not in (reader.fieldnames or [])]
        if missing:
            raise SystemExit(f"站点表缺列 {missing}；现有列 {list(reader.fieldnames or [])}")
        stations: list[Station] = []
        seen: set[str] = set()
        for row in reader:
            station_id = (row.get("station_id") or "").strip()
            region_code = (row.get("region_code") or "").strip()
            try:
                latitude = float(row.get("latitude") or "")
                longitude = float(row.get("longitude") or "")
            except ValueError as exc:
                raise SystemExit(f"站点 {station_id or '(无 id)'} 的坐标不是数字：{exc}") from exc
            if not station_id or not region_code:
                raise SystemExit(f"站点表有行缺 station_id/region_code：{row}")
            if not -90.0 <= latitude <= 90.0 or not -180.0 <= longitude <= 180.0:
                raise SystemExit(f"站点 {station_id} 坐标越界：{latitude},{longitude}")
            if station_id in seen:
                raise SystemExit(f"站点表有重复 station_id：{station_id}")
            seen.add(station_id)
            stations.append(Station(station_id, region_code, latitude, longitude))
    if not stations:
        raise SystemExit("站点表没有行")
    return stations


def build_url(station: Station, start: date, end: date) -> str:
    """单站请求 URL（archive API 一次只吃一个坐标点，多站就多次请求）。"""
    query = urllib.parse.urlencode(
        {
            "latitude": f"{station.latitude:.4f}",
            "longitude": f"{station.longitude:.4f}",
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "hourly": "precipitation",
            "timezone": "UTC",
        }
    )
    return f"{ARCHIVE_URL}?{query}"


def _clamp_url_to_allowed(url: str, reason: str) -> str | None:
    """archive 对 `end_date` 越界（本地日期已超过 UTC 数据可及日）会回 400 并带允许范围——
    从 reason 里解析上界，把窗口收敛进去后重试；解析不出来就放弃（返回 None）。"""
    match = re.search(r"from (\d{4}-\d{2}-\d{2}) to (\d{4}-\d{2}-\d{2})", reason)
    if match is None:
        return None
    allowed_max = match.group(2)
    parsed = urllib.parse.urlparse(url)
    params = dict(urllib.parse.parse_qsl(parsed.query))
    if params.get("end_date", "") > allowed_max:
        params["end_date"] = allowed_max
    if params.get("start_date", "") > params["end_date"]:
        params["start_date"] = params["end_date"]
    return urllib.parse.urlunparse(parsed._replace(query=urllib.parse.urlencode(params)))


def fetch_one(url: str, timeout_seconds: float = 30.0) -> dict:
    """拉一个站的小时序列并解析成 `{iso_hour: mm}`；HTTP/解析错误说人话。"""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            raise SystemExit("Open-Meteo 限流（429）：加大 --sleep 后重试；免费口子不建议并发") from exc
        if exc.code == 400:
            # archive 的可及上界随 UTC 推进（本地深夜已越界时最常见）：收敛窗口重试一次
            clamped = _clamp_url_to_allowed(url, exc.read().decode("utf-8", errors="replace"))
            if clamped is not None and clamped != url:
                return fetch_one(clamped, timeout_seconds)
        raise SystemExit(f"Open-Meteo HTTP {exc.code}：{exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"Open-Meteo 网络错误：{exc.reason}") from exc
    hourly = payload.get("hourly") or {}
    times = hourly.get("time") or []
    values = hourly.get("precipitation") or []
    if len(times) != len(values):
        raise SystemExit("Open-Meteo 返回的 time 与 precipitation 长度不一致，响应可能变了")
    out: dict[str, float | None] = {}
    for raw_time, value in zip(times, values, strict=True):
        # 归一成分钟精度的整点（小时数据秒位是噪声），落盘统一一种形态：
        # `2026-11-20T13:00Z`——连接器两种形态都认，但盘上不该有两种。
        moment = datetime.fromisoformat(raw_time).replace(tzinfo=UTC)
        out[moment.strftime("%Y-%m-%dT%H:%MZ")] = None if value is None else float(value)
    return out


def merge_csv(existing: dict[tuple[str, str], float | None], fetched: dict[tuple[str, str], float | None]) -> tuple[int, int]:
    """按 (datetime_utc, station_id) 合并：已有键保留旧值，返回 (合并后总行数, 新增行数)。"""
    added = 0
    for key, value in fetched.items():
        if key not in existing:
            existing[key] = value
            added += 1
    return len(existing), added


def read_existing(path: Path) -> dict[tuple[str, str], float | None]:
    if not path.exists():
        return {}
    rows: dict[tuple[str, str], float | None] = {}
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            key = ((row.get("datetime_utc") or "").strip(), (row.get("station_id") or "").strip())
            raw = (row.get("precip_mm_h") or "").strip()
            rows[key] = None if not raw else float(raw)
    return rows


def write_csv(path: Path, rows: dict[tuple[str, str], float | None], stations: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(rows.items())
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(OUT_COLUMNS)
        for (moment, station_id), value in ordered:
            writer.writerow([moment, station_id, stations.get(station_id, ""), "" if value is None else f"{value:.1f}"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Open-Meteo 逐小时降水取数（CC BY 4.0，匿名可调）")
    parser.add_argument("--stations", type=Path, default=DEFAULT_STATIONS, help="站点表 CSV（station_id/region_code/latitude/longitude）")
    parser.add_argument("--dest", type=Path, default=DEFAULT_DEST, help="输出目录（默认 backend/data/precip/openmeteo）")
    parser.add_argument("--days", type=int, default=3, help="取最近 N 天（含今天；archive 的今天可能只有部分小时）")
    parser.add_argument("--sleep", type=float, default=0.3, help="每站请求间隔秒数（免费口子不加压）")
    parser.add_argument("--dry-run", action="store_true", help="只打印将请求的 URL，不联网")
    args = parser.parse_args(argv)

    stations_list = load_stations(args.stations)
    end = date.today()
    start = end - timedelta(days=max(args.days, 1) - 1)
    urls = [build_url(station, start, end) for station in stations_list]
    if args.dry_run:
        for url in urls:
            print(url)
        return 0

    out_path = args.dest / "openmeteo_hourly.csv"
    existing = read_existing(out_path)
    stations_map = {station.station_id: station.region_code for station in stations_list}
    added_total = 0
    for station, url in zip(stations_list, urls, strict=True):
        series = fetch_one(url)
        _rows, added = merge_csv(existing, {(moment, station.station_id): value for moment, value in series.items()})
        added_total += added
        print(f"{station.station_id}: 取到 {len(series)} 小时，新增 {added} 行", file=sys.stderr)
        time.sleep(args.sleep)
    write_csv(out_path, existing, stations_map)
    print(f"合并完成：{out_path}（共 {len(existing)} 行，本次新增 {added_total}）")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
