"""Science Data Bank《2022 年度西藏地质灾害普查数据》（**CC BY-NC 4.0**）。

内容是**西藏公路沿线**地质灾害普查：崩塌 / 水毁 / 沉陷塌陷 / 泥石流 / 滑坡 五个 CSV，
共 13,768 条。字段是道路与隐患点口径（线路、桩号、断通状况、灾害等级等）。

**许可提醒**：CC BY-NC 4.0（非商业），与仓库 AGPL-3.0 不是一套许可。隔离放置，
`manifest.json` 标 `commercial_use: false`；**商用发行前必须整目录移除或另行取得授权**。

抓取：Science Data Bank 公开文件树 API（`getAllUrl`），无需登录。
"""

from __future__ import annotations

import urllib.parse
from pathlib import Path
from typing import Any

from scripts.external_data.core import (
    DatasetSpec,
    FileMeta,
    dataset_dir,
    download,
    read_csv_rows,
    write_bytes,
    write_envelope,
    write_manifest,
)

DATASET_ID = "sciencedb-tibet-road-geohazard-2022"
DATA_SET_ID = "9b7541457595412c91304d3193198cc5"
FILETREE_URL = f"https://www.scidb.cn/api/gin-sdb-filetree/getAllUrl?dataSetId={DATA_SET_ID}&type=personal&version=V1&global=Asia/Shanghai"
LANDING_URL = "https://www.scidb.cn/doi/10.57760/sciencedb.27813"
DOI = "10.57760/sciencedb.27813"
CSTR = "31253.11.sciencedb.27813"
LICENSE_ID = "cc-by-nc-4.0"
LICENSE_URL = "https://creativecommons.org/licenses/by-nc/4.0/"

#: 源文件名 → 项目 HazardType。沉陷塌陷/水毁在项目枚举里没有对应类，落 unknown（原文保留）。
CATEGORY_MAP: dict[str, str] = {
    "崩塌": "rockfall",
    "滑坡": "landslide",
    "泥石流": "debris_flow",
    "沉陷塌陷": "unknown",
    "水毁": "unknown",
}

NORMALIZED_COLUMNS: tuple[str, ...] = (
    "record_id",
    "source_category",
    "hazard_type",
    "disaster_code",
    "name",
    "road_class",
    "road_name",
    "road_code",
    "admin_code",
    "maintainer",
    "occurred",
    "last_event_date",
    "last_event_date_raw",
    "blocked_status",
    "severity",
    "damage_level",
    "development_degree",
    "tech_level",
    "occurrence_window_years",
    "occurrence_count",
    "treatment",
    "check_status",
    "start_stake",
    "end_stake",
    "longitude",
    "latitude",
    "geometry_points",
)

_DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S")


def _clean(value: str | None) -> str:
    return (value or "").strip()


def _parse_date(raw: str) -> str:
    text = raw.strip()
    if not text:
        return ""
    for fmt in _DATE_FORMATS:
        try:
            from datetime import datetime

            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return ""


def _parse_coord(raw: str) -> tuple[float, float, int]:
    """`90.95,29.97;90.95,29.97;` → 第一个点 + 点数。"""
    points = [part for part in raw.split(";") if part.strip()]
    if not points:
        raise SystemExit("经纬度为空")
    lon_text, lat_text = points[0].split(",")[:2]
    return float(lon_text), float(lat_text), len(points)


def spec() -> DatasetSpec:
    return DatasetSpec(
        id=DATASET_ID,
        title="2022 年度西藏地质灾害普查数据（公路沿线）",
        publisher="Science Data Bank",
        authors=["田建（Tian Jian），中国交通科学研究院（China Academy of Transportation Sciences）"],
        license_id=LICENSE_ID,
        license_url=LICENSE_URL,
        commercial_use=False,
        license_note=(
            "CC BY-NC 4.0：允许署名、非商业性再分发；禁止商业性使用。"
            "本目录与 AGPL-3.0 主仓库隔离，manifest 标 commercial_use=false；"
            "商用发行前必须移除本目录或另行取得授权。"
        ),
        citation=(
            "田建（Tian Jian），中国交通科学研究院. 2022 年度西藏地质灾害普查数据[DS/OL]. V1. "
            f"Science Data Bank, 2025. https://doi.org/{DOI}. CSTR:{CSTR}. CC BY-NC 4.0."
        ),
        landing_url=LANDING_URL,
        doi=DOI,
        cstr=CSTR,
        source_urls={"filetree_api": FILETREE_URL},
        selection={
            "scope": "五个 CSV 的全部记录（崩塌/水毁/沉陷塌陷/泥石流/滑坡）",
            "exclusions": [],
        },
        normalization={
            "raw_encoding": "gb18030",
            "raw_line_endings": "CRLF（raw 按字节原样保留）",
            "normalized_encoding": "utf-8",
            "normalized_line_endings": "LF",
            "date_rule": "最近发生时间 → ISO；解析不了留空，原文在 last_event_date_raw",
            "coord_rule": "经纬度CGCS2K 取第一个点；原串是 'lon,lat;lon,lat;' 折线",
            "category_map": CATEGORY_MAP,
        },
        known_limitations=[
            "公路沿线普查口径，不是行政区域全量灾情；非公路地质灾害点不在内。",
            "2,070 条 是否发生灾害=否 是隐患点，不是已发生事件（normalized.occurred=false）。",
            "最近发生时间 2,056 条为空；有值的范围 2012-07-10 至 2022-09-04。",
            "经纬度是灾害体折线（首点入 normalized），不是单点坐标。",
            "**没有伤亡、经济损失、受灾人口字段**——这些字段在本数据里不存在。",
        ],
    )


def _source_files(source_dir: Path | None) -> list[tuple[str, bytes]]:
    if source_dir is not None:
        files = sorted(source_dir.glob("*.csv"))
        if len(files) != 5:
            raise SystemExit(f"--source 里应有 5 个 CSV，实际 {len(files)} 个: {[p.name for p in files]}")
        return [(path.name, path.read_bytes()) for path in files]
    listing = download(FILETREE_URL, timeout=60).decode("utf-8")
    urls = [line.strip() for line in listing.splitlines() if line.strip()]
    if len(urls) != 5:
        raise SystemExit(f"ScienceDB 文件树应返回 5 个文件，实际 {len(urls)} 个；上游结构变了，先核对")
    out: list[tuple[str, bytes]] = []
    for url in urls:
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        name = query.get("fileName", [""])[0]
        if not name:
            raise SystemExit(f"文件 URL 缺 fileName: {url[:120]}")
        print(f"下载 {name} …")
        out.append((name, download(url)))
    return out


def refresh(source: Path | None = None) -> Path:
    """重抓并重写 raw / normalized / manifest。

    `source` 给定时从本地目录读那 5 个 CSV（离线复跑用），不给则走 ScienceDB 接口。
    """
    root = dataset_dir(DATASET_ID)
    files: dict[str, FileMeta] = {}
    normalized_rows: list[dict[str, Any]] = []
    for name, raw in _source_files(source):
        rows = read_csv_rows(raw, encoding="gb18030")
        files[f"raw/{name}"] = FileMeta(write_bytes(raw, root / "raw" / name).sha256, len(raw), rows=len(rows))
        category = name.removesuffix(".csv")
        hazard_type = CATEGORY_MAP.get(category)
        if hazard_type is None:
            raise SystemExit(f"未知灾害类型文件: {name}")
        for row in rows:
            code = _clean(row.get("灾害编号"))
            if not code:
                raise SystemExit(f"{name} 有记录缺灾害编号")
            lon, lat, points = _parse_coord(_clean(row.get("经纬度CGCS2K")))
            raw_date = _clean(row.get("最近发生时间"))
            window = "10" if "近十年发生次数" in row else "5"
            treatment = _clean(row.get("灾害处治情况")) or _clean(row.get("灾害修复情况"))
            normalized_rows.append(
                {
                    "schema_version": "1.0.0",
                    "record_id": f"{DATASET_ID}:{code}",
                    "source": {
                        "dataset": DATASET_ID,
                        "event_id": code,
                        "source_name": category,
                        "source_url": LANDING_URL,
                        "license": LICENSE_ID,
                    },
                    "event": {
                        "date": _parse_date(raw_date),
                        "date_raw": raw_date or None,
                        "title": _clean(row.get("灾害名称")) or None,
                        "description": None,
                        "location": _clean(row.get("所在地区")) if row.get("所在地区") else None,
                        "location_accuracy": None,
                        "storm_name": None,
                        "notes": None,
                    },
                    "geo": {
                        "latitude": lat,
                        "longitude": lon,
                        "country": "China",
                        "admin_division": _clean(row.get("区划代码")) or None,
                    },
                    "hazard": {
                        "type": hazard_type,
                        "source_category": category,
                        "attributes": {
                            "occurred": {"是": True, "否": False}.get(_clean(row.get("是否发生灾害"))),
                            "road_class": _clean(row.get("道路类别")) or None,
                            "road_name": _clean(row.get("线路名称")) or None,
                            "road_code": _clean(row.get("线路编号")) or None,
                            "maintainer": _clean(row.get("管养单位")) or None,
                            "blocked_status": _clean(row.get("断通状况")) or None,
                            "severity": _clean(row.get("危害程度")) or None,
                            "damage_level": _clean(row.get("灾害等级")) or None,
                            "development_degree": _clean(row.get("灾害发育程度")) or None,
                            "tech_level": _clean(row.get("技术等级")) or None,
                            "occurrence_window_years": window,
                            "occurrence_count": _clean(row.get(f"近{'十' if window == '10' else '五'}年发生次数")) or None,
                            "treatment": treatment or None,
                            "check_status": _clean(row.get("核查状态")) or None,
                            "start_stake": _clean(row.get("起点桩号")) or None,
                            "end_stake": _clean(row.get("止点桩号")) or None,
                            "geometry_points": points,
                        },
                    },
                    "impact": {"fatalities": None, "injuries": None},
                }
            )
    files["normalized/tibet_road_geohazard_2022.v1.jsonl"] = write_envelope(
        normalized_rows, root / "normalized" / "tibet_road_geohazard_2022.v1.jsonl"
    )
    manifest_path = write_manifest(spec(), files)
    print(f"规范化 {len(normalized_rows)} 行")
    print(f"manifest: {manifest_path}")
    return manifest_path


def check() -> list[str]:
    from scripts.external_data.core import verify_dataset

    return verify_dataset(DATASET_ID)


if __name__ == "__main__":  # pragma: no cover
    import json

    print(json.dumps(check(), ensure_ascii=False))
