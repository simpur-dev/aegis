"""Zenodo 上两套喜马拉雅地质灾害清单（**CC BY 4.0**，可自由再分发）。

## 为什么重要

这是目前**唯一许可干净**（CC BY 4.0，允许商用与再分发）的喜马拉雅地灾数据源。
前面收的两套（NASA GLC / TPDC / ScienceDB）要么是抽样目录，要么带 NC / NC-SA 限制；
这一套可以直接进任何对外交付物。

## 两个数据集

1. **Large Landslide Inventory of the Eastern Himalaya**（Zenodo 18931430）
   420 个大型滑坡点位（WGS84，KML），覆盖东喜马拉雅—藏东南—尼泊尔东部一带：
   经度 90.40–97.16，纬度 27.03–30.30。编号 `EH-1 … EH-420`。
2. **Earth's largest terrestrial landslides (MEGAS)**（Zenodo 15908068）
   全球 ≥1 km³ 或 ≥10 km² 的巨型滑坡目录 411 条，含中国 26 条（林芝、米林、道孚、
   乔家等）。**CSV 里只有地名没有坐标**，所以坐标列留空、原值保留——
   需要坐标时用地名关联，不臆造。

另附 **Parlung Zangbo 流域冰湖清单**（Zenodo 20305493）——见 `zenodo_lakes` 模块。

抓取：Zenodo REST API（`/api/records/<id>/files/<name>/content`），匿名可下。
"""

from __future__ import annotations

import csv
import io
import re
from pathlib import Path
from typing import Any

from scripts.external_data.core import (
    DatasetSpec,
    FileMeta,
    dataset_dir,
    download,
    write_bytes,
    write_envelope,
    write_manifest,
)

DATASET_ID = "zenodo-himalaya-landslide-inventories"

EASTERN_HIMALAYA_RECORD = 18931430
EASTERN_HIMALAYA_FILE = "Large Landslide Location in Eastern Himalaya.kml"
EASTERN_HIMALAYA_DOI = "10.5281/zenodo.18931430"

MEGAS_RECORD = 15908068
MEGAS_FILE = "MEGAS_MASTER_02_2025.csv"
MEGAS_DOI = "10.5281/zenodo.15908068"

LICENSE_ID = "cc-by-4.0"
LICENSE_URL = "https://creativecommons.org/licenses/by/4.0/"


def _record_meta(record_id: int) -> dict[str, Any]:
    """Zenodo 记录元数据（用于取真实标题/作者/许可，不手写）。"""
    import json

    payload = json.loads(download(f"https://zenodo.org/api/records/{record_id}", timeout=60).decode("utf-8"))
    metadata = payload.get("metadata", {})
    return {
        "title": metadata.get("title", ""),
        "creators": [c.get("name", "") for c in metadata.get("creators", [])],
        "license": (metadata.get("license") or {}).get("id", ""),
        "publication_date": metadata.get("publication_date", ""),
        "description": metadata.get("description", "")[:600],
    }


def _parse_kml(raw: bytes) -> list[dict[str, Any]]:
    """KML 点位：`<name>` + `<coordinates>`。坐标是 `lon,lat,alt` 逗号串。"""
    text = raw.decode("utf-8", "replace")
    records: list[dict[str, Any]] = []
    for block in re.findall(r"<Placemark>(.*?)</Placemark>", text, re.S):
        name_match = re.search(r"<name>(.*?)</name>", block, re.S)
        coord_match = re.search(r"<coordinates>(.*?)</coordinates>", block, re.S)
        if coord_match is None:
            continue
        parts = coord_match.group(1).strip().split(",")
        if len(parts) < 2:
            continue
        records.append({"name": (name_match.group(1).strip() if name_match else ""), "lon": float(parts[0]), "lat": float(parts[1])})
    return records


def _parse_megas(raw: bytes) -> list[dict[str, str]]:
    """MEGAS CSV 是 **latin-1**（含非 UTF-8 字节），按 cp1252 解。"""
    text = raw.decode("cp1252")
    return list(csv.DictReader(io.StringIO(text)))


def spec(eastern: dict[str, Any], megas: dict[str, Any]) -> DatasetSpec:
    return DatasetSpec(
        id=DATASET_ID,
        title="喜马拉雅大型滑坡清单（Zenodo 两套：东喜马拉雅 420 点 + 全球巨型滑坡 MEGAS 411 条）",
        publisher="Zenodo（individual contributors）",
        authors=[*eastern.get("creators", []), *megas.get("creators", [])],
        license_id=LICENSE_ID,
        license_url=LICENSE_URL,
        commercial_use=True,
        license_note="CC BY 4.0：允许署名后的任何用途，含商业使用与再分发（须保留署名与许可链接）。",
        citation=(
            f"{eastern.get('title', '')}. Zenodo. https://doi.org/{EASTERN_HIMALAYA_DOI}. CC BY 4.0. "
            f"{megas.get('title', '')}. Zenodo. https://doi.org/{MEGAS_DOI}. CC BY 4.0."
        ),
        landing_url=f"https://zenodo.org/records/{EASTERN_HIMALAYA_RECORD}",
        source_urls={
            "eastern_himalaya_api": f"https://zenodo.org/api/records/{EASTERN_HIMALAYA_RECORD}",
            "eastern_himalaya_file": (f"https://zenodo.org/api/records/{EASTERN_HIMALAYA_RECORD}/files/{EASTERN_HIMALAYA_FILE}/content"),
            "megas_api": f"https://zenodo.org/api/records/{MEGAS_RECORD}",
            "megas_file": f"https://zenodo.org/api/records/{MEGAS_RECORD}/files/{MEGAS_FILE}/content",
        },
        selection={
            "scope": "两个记录的全部记录（420 点位 + 411 巨型滑坡条目）",
            "exclusions": [],
        },
        normalization={
            "east_himalaya": "KML Placemark → 经纬度（WGS84），编号取 Placemark 的 name（EH-n）",
            "megas": "CSV 为 latin-1/cp1252 编码；**只有地名无坐标**，坐标列留空、原值保留",
            "date_rule": "两个来源都没有事件日期字段（是空间清单不是事件目录），日期列留空",
        },
        known_limitations=[
            "**这是空间清单，不是事件目录**：没有发生日期、伤亡、损失字段；不能当灾情事件库用。",
            "MEGAS 的 411 条只有地名（Location），**没有坐标**；坐标列留空，需另行地名关联。",
            "MEGAS 源 CSV 有 **14 行缺 ID**（含 6 条中国记录）：用源 ID 会把它们丢掉，"
            "所以这 14 条按行序编号并在 `source.event_id` 留 null。",
            "东喜马拉雅清单的点位是滑坡体位置，不代表其发生时刻或规模。",
            "KML 覆盖东喜马拉雅—藏东南—尼泊尔东部一带（经度 90.40–97.16，纬度 27.03–30.30），**不覆盖阿里/日喀则西段**。",
        ],
    )


def refresh() -> Path:
    eastern_meta = _record_meta(EASTERN_HIMALAYA_RECORD)
    megas_meta = _record_meta(MEGAS_RECORD)
    root = dataset_dir(DATASET_ID)

    eastern_raw = download(f"https://zenodo.org/api/records/{EASTERN_HIMALAYA_RECORD}/files/{EASTERN_HIMALAYA_FILE}/content")
    megas_raw = download(f"https://zenodo.org/api/records/{MEGAS_RECORD}/files/{MEGAS_FILE}/content")

    eastern_records: list[dict[str, Any]] = []
    for index, point in enumerate(_parse_kml(eastern_raw), start=1):
        label = point["name"] or f"point-{index:04d}"
        eastern_records.append(
            {
                "schema_version": "1.0.0",
                "record_id": f"{DATASET_ID}:eh:{label}",
                "source": {
                    "dataset": DATASET_ID,
                    "collection": "eastern-himalaya",
                    "source_name": point["name"] or None,
                    "source_url": f"https://zenodo.org/records/{EASTERN_HIMALAYA_RECORD}",
                    "license": LICENSE_ID,
                },
                "event": {
                    "date": None,
                    "title": point["name"] or None,
                    "description": None,
                    "location": None,
                    "location_accuracy": None,
                    "storm_name": None,
                    "notes": None,
                },
                "geo": {
                    "latitude": point["lat"],
                    "longitude": point["lon"],
                    "country": None,
                    "admin_division": None,
                },
                "hazard": {"type": "landslide", "source_category": "large_landslide", "attributes": {}},
                "impact": {"fatalities": None, "injuries": None},
            }
        )

    megas_records: list[dict[str, Any]] = []
    for row_index, row in enumerate(_parse_megas(megas_raw), start=1):
        event_id = (row.get("ID") or "").strip()
        # 源 CSV 有 14 行缺 ID（其中 6 条是中国的：Dehenglong/Jiaobuong/Kangyang/
        # Qiaojia2/Suozi/Xijitan）——按 ID 过滤会把它们悄悄丢掉，所以用行序兜底，
        # 并在 record_id 里标出来源是行序而不是源 ID。
        if event_id:
            record_id = f"{DATASET_ID}:megas:{event_id}"
            source_id: str | None = event_id
        else:
            record_id = f"{DATASET_ID}:megas:row-{row_index:04d}"
            source_id = None
        megas_records.append(
            {
                "schema_version": "1.0.0",
                "record_id": record_id,
                "source": {
                    "dataset": DATASET_ID,
                    "collection": "megas",
                    "event_id": source_id,
                    "source_name": (row.get("Location") or "").strip() or None,
                    "source_url": f"https://zenodo.org/records/{MEGAS_RECORD}",
                    "license": LICENSE_ID,
                },
                "event": {
                    "date": None,
                    "title": (row.get("Location") or "").strip() or None,
                    "description": None,
                    "location": (row.get("Location") or "").strip() or None,
                    "location_accuracy": None,
                    "storm_name": None,
                    "notes": None,
                },
                "geo": {
                    "latitude": None,
                    "longitude": None,
                    "country": (row.get("Country") or "").strip() or None,
                    "admin_division": None,
                },
                "hazard": {
                    "type": "landslide",
                    "source_category": "mega_landslide",
                    "attributes": {
                        "country": (row.get("Country") or "").strip() or None,
                        "rock_type": (row.get("Rock (general)") or "").strip() or None,
                        "landslide_type": (row.get("LSType") or "").strip() or None,
                        "deposition_area_km2": (row.get("Adepo (km2)") or "").strip() or None,
                        "deposition_volume_1e6_m3": (row.get("Vdepo (106m3)") or "").strip() or None,
                        "scarp_volume_1e6_m3": (row.get("Vscar (106m3)") or "").strip() or None,
                        "distance_to_active_fault_km": (row.get("Distance to active fault (km)") or "").strip() or None,
                        "topographic_setting": (row.get("Topographic setting") or "").strip() or None,
                    },
                },
                "impact": {"fatalities": None, "injuries": None},
            }
        )

    files: dict[str, FileMeta] = {
        "raw/eastern_himalaya_large_landslides.kml": write_bytes(eastern_raw, root / "raw" / "eastern_himalaya_large_landslides.kml"),
        "raw/megas_master_2025.csv": write_bytes(megas_raw, root / "raw" / "megas_master_2025.csv"),
        "normalized/eastern_himalaya_large_landslides.v1.jsonl": write_envelope(
            eastern_records, root / "normalized" / "eastern_himalaya_large_landslides.v1.jsonl"
        ),
        "normalized/megas_largest_landslides.v1.jsonl": write_envelope(
            megas_records, root / "normalized" / "megas_largest_landslides.v1.jsonl"
        ),
    }
    manifest_path = write_manifest(spec(eastern_meta, megas_meta), files)
    print(f"东喜马拉雅 {len(eastern_records)} 点；MEGAS {len(megas_records)} 条")
    print(f"manifest: {manifest_path}")
    return manifest_path


def check() -> list[str]:
    from scripts.external_data.core import verify_dataset

    return verify_dataset(DATASET_ID)


if __name__ == "__main__":  # pragma: no cover
    import json

    print(json.dumps(check(), ensure_ascii=False))
