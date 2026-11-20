"""站点表生成：县域降水**代理点**（Wikidata CC0 坐标）→ 两条降水腿共用的 stations.csv。

**它解决的是哪一个断点**：两条降水腿（Open-Meteo 逐小时、CHM_PRE 逐日派生）都要
站点表（`station_id, region_code, latitude, longitude`）——按站取最近格点/最邻近请求。
而仓里**没有任何带真实坐标的站点台账**：站点维表从未有过生产写入方，演示档案只有
区划码没有坐标。坐标从哪来是数据质量问题，不是代码问题——**编一个经纬度，
被当成实测证据就是事故**（`import_stations` 的红线）。

**本脚本的取数来源（可回溯、可复跑、许可干净）**：Wikidata（**CC0**，无需署名）。
县域行政中心的坐标声明 `P625` + 海拔 `P2044`，每条坐标带实体号（Q…），
`stations.provenance.jsonl` 逐行记下 `source_url` 与取数时间——任何坐标都能回到
`https://www.wikidata.org/wiki/Q…` 复核。

**站点的诚实定位**：这是**县域降水代理点**（区划代表点），不是实测雨量站。
名字与 `hazard_focus` 都写明"代理"；站端设备接入后（MQTT 推送腿带真实站号），
台账里替换坐标即可，表结构不变。

**区域从哪来**：`data/site_profiles.demo.jsonl` 是平台唯一的区域注册面——
区划码与县名都从那里解析（去掉"（演示）"后缀，剥掉地级市前缀取县名），
不在脚本里另写一份映射。

用法：
    uv run python scripts/build_precip_stations.py                    # 按演示档案生成
    uv run python scripts/build_precip_stations.py --dest other/dir   # 换落盘目录
    uv run python scripts/build_precip_stations.py --dry-run          # 只打印将查的实体

产出（运行态目录，不进 git）：
    backend/data/precip/stations.csv                  # 两条降水腿共用的站点表
    backend/data/precip/stations.provenance.jsonl     # 逐站坐标出处（Q 号 / URL / 取数时间）
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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
DEMO_PROFILES = REPO_ROOT / "backend" / "data" / "site_profiles.demo.jsonl"
DEFAULT_DEST = REPO_ROOT / "backend" / "data" / "precip"
SEARCH_API = "https://www.wikidata.org/w/api.php"
USER_AGENT = "aegis-station-builder/1.0 (precip station proxy table)"
LICENSE = "CC0-1.0"
#: 两条降水腿共用的站点表列契约（`fetch_precip_hourly_openmeteo` 与
#: `derive_precip_station_daily` 的 `load_stations` 都读这四列 + 名称/关注灾种）。
STATION_COLUMNS = ("station_id", "region_code", "latitude", "longitude", "name_zh", "hazard_focus", "elevation_m")
COUNTY_PATTERN = re.compile(r"^(?:.+?(?:市|地区|自治州))(.+(?:区|县|市|旗))$")
STATION_ID_PREFIX = "PR"


@dataclass(frozen=True, slots=True)
class ProxyStation:
    region_code: str
    county: str
    station_id: str
    latitude: float
    longitude: float
    name_zh: str
    hazard_focus: str
    elevation_m: float | None
    wikidata_id: str
    source_url: str


def load_regions(path: Path) -> list[tuple[str, str, str]]:
    """从演示区域档案解析 `(region_code, 县名, hazard_focus)`；名字剥掉"（演示）"。

    前缀剥到市/地区为止取县名（"林芝市巴宜区"→"巴宜区"）；剥不出来就整份失败——
    拿错搜索词查出的实体是别的县，坐标错得无声无息。
    """
    rows: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        text = line.strip()
        if not text:
            continue
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"区域档案第 {line_no} 行不是合法 JSON：{exc}") from exc
        region_code = str(raw.get("region_code") or "").strip()
        name = str(raw.get("name") or "").strip().removesuffix("（演示）").removesuffix("(演示)").strip()
        if not region_code or not name:
            raise SystemExit(f"区域档案第 {line_no} 行缺 region_code/name")
        if region_code in seen:
            raise SystemExit(f"区域档案有重复 region_code：{region_code}")
        seen.add(region_code)
        match = COUNTY_PATTERN.match(name)
        if match is None:
            raise SystemExit(f"区域 {region_code} 的名字剥不出县名：{name!r}（预期能按市/地区前缀切分）")
        county = match.group(1)
        focus = "|".join(raw.get("hazard_types") or [])
        rows.append((region_code, county, focus))
    if not rows:
        raise SystemExit("区域档案没有行")
    return rows


def _get_json(url: str, timeout_seconds: float = 30.0) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"Wikidata HTTP {exc.code}：{exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"Wikidata 网络错误：{exc.reason}") from exc


def search_entity(county: str) -> tuple[str, str]:
    """按县名搜实体，取描述含"西藏"的第一条；没有就报错列出候选——不猜。"""
    query = urllib.parse.urlencode(
        {"action": "wbsearchentities", "search": county, "language": "zh", "uselang": "zh", "format": "json", "limit": 5, "type": "item"}
    )
    payload = _get_json(f"{SEARCH_API}?{query}")
    results = payload.get("search") or []
    candidates = [(str(item.get("id") or ""), str(item.get("description") or "")) for item in results]
    for entity_id, description in candidates:
        if "西藏" in description:
            return entity_id, description
    raise SystemExit(f"{county}: 没有描述含「西藏」的实体，候选如下（人工核对后改用 --entity 指定）：{candidates}")


def entity_coordinates(entity_id: str) -> tuple[float, float, float | None]:
    """取实体的坐标（P625 首选 preferred rank，否则第一条）与海拔（P2044，可选）。"""
    query = urllib.parse.urlencode({"action": "wbgetentities", "ids": entity_id, "props": "claims", "format": "json"})
    payload = _get_json(f"{SEARCH_API}?{query}")
    entity = (payload.get("entities") or {}).get(entity_id)
    if entity is None:
        raise SystemExit(f"{entity_id}: 实体不存在")
    claims = entity.get("claims") or {}
    coordinates = claims.get("P625") or []
    if not coordinates:
        raise SystemExit(f"{entity_id}: 没有 P625 坐标声明")
    preferred = [claim for claim in coordinates if claim.get("rank") == "preferred"] or coordinates
    value = preferred[0]["mainsnak"]["datavalue"]["value"]
    latitude, longitude = float(value["latitude"]), float(value["longitude"])
    elevations = claims.get("P2044") or []
    elevation = float(elevations[0]["mainsnak"]["datavalue"]["value"]["amount"]) if elevations else None
    return latitude, longitude, elevation


def build_stations(profiles: Path, *, entity_override: dict[str, str] | None = None, sleep_seconds: float = 0.2) -> list[ProxyStation]:
    """逐区域解析县名 → 搜实体 → 取坐标；任何一步含糊就整份失败。"""
    stations: list[ProxyStation] = []
    for region_code, county, focus in load_regions(profiles):
        if entity_override and region_code in entity_override:
            entity_id = entity_override[region_code]
        else:
            entity_id, _description = search_entity(county)
        latitude, longitude, elevation = entity_coordinates(entity_id)
        stations.append(
            ProxyStation(
                region_code=region_code,
                county=county,
                station_id=f"{STATION_ID_PREFIX}-{region_code}-01",
                latitude=round(latitude, 6),
                longitude=round(longitude, 6),
                name_zh=f"{county}县域降水代理点",
                hazard_focus=focus,
                elevation_m=elevation,
                wikidata_id=entity_id,
                source_url=f"https://www.wikidata.org/wiki/{entity_id}",
            )
        )
        time.sleep(sleep_seconds)
    return stations


def write_outputs(dest: Path, stations: list[ProxyStation]) -> tuple[Path, Path]:
    dest.mkdir(parents=True, exist_ok=True)
    csv_path = dest / "stations.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(STATION_COLUMNS)
        for station in stations:
            writer.writerow(
                [
                    station.station_id,
                    station.region_code,
                    station.latitude,
                    station.longitude,
                    station.name_zh,
                    station.hazard_focus,
                    "" if station.elevation_m is None else int(station.elevation_m),
                ]
            )
    provenance_path = dest / "stations.provenance.jsonl"
    with provenance_path.open("w", encoding="utf-8", newline="") as handle:
        for station in stations:
            handle.write(
                json.dumps(
                    {
                        "station_id": station.station_id,
                        "region_code": station.region_code,
                        "county": station.county,
                        "wikidata_id": station.wikidata_id,
                        "source_url": station.source_url,
                        "license": LICENSE,
                        "retrieved_at": datetime.now(UTC).isoformat(),
                        "note": "县域降水代理点（区划代表点），非实测站；站端设备接入后替换坐标",
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    return csv_path, provenance_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="县域降水代理点站点表生成（Wikidata CC0 坐标，可回溯）")
    parser.add_argument("--profiles", type=Path, default=DEMO_PROFILES, help="区域档案（region_code/name/hazard_types）")
    parser.add_argument("--dest", type=Path, default=DEFAULT_DEST, help="输出目录（默认 backend/data/precip）")
    parser.add_argument("--entity", action="append", default=[], help="人工指定实体：<region_code>=<Qid>（覆盖自动搜索）")
    parser.add_argument("--dry-run", action="store_true", help="只解析县名与搜索结果，不落盘")
    args = parser.parse_args(argv)

    override = {}
    for item in args.entity:
        region_code, _, entity_id = item.partition("=")
        if not region_code or not entity_id:
            raise SystemExit(f"--entity 形如 540121=Q519708，收到：{item!r}")
        override[region_code] = entity_id

    regions = load_regions(args.profiles)
    for region_code, county, _focus in regions:
        print(f"{region_code} → {county}", file=sys.stderr)
    if args.dry_run:
        return 0

    stations = build_stations(args.profiles, entity_override=override or None)
    csv_path, provenance_path = write_outputs(args.dest, stations)
    print(f"站点表：{csv_path}（{len(stations)} 个县域代理点）")
    print(f"出处：{provenance_path}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
