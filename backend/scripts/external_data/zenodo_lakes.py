"""Zenodo《帕隆藏布流域冰湖清单 2020/2025》（**CC BY 4.0**）。

## 为什么收它

平台的五灾种里有**冰湖溃决（lake_outburst）**，但到今天为止仓里没有任何真实冰湖点位数据
——NASA GLC 只有滑坡/泥石流，公路普查只有公路沿线五类。冰湖清单补的就是这一块：
帕隆藏布（雅鲁藏布江最大支流，林芝—米林—墨脱一带）的**冰湖本体**，
2020 与 2025 两期，可算湖群扩张。

每条记录带：面积、体积（2025 版）、水面高程区间、坡度、是否与冰川接触、最近冰川、
冰湖类型与分类。**这是评估溃决风险的直接底数**。

坐标：源文件同时给度分秒字符串与投影坐标（EPSG:32645 附近），normalized 取度分秒
解析出的十进制度（`parse_dms`），投影列原值保留。

许可：CC BY 4.0——允许商用与再分发（须保留署名）。
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
from typing import Any

from scripts.external_data.core import (
    DatasetSpec,
    FileMeta,
    dataset_dir,
    download,
    parse_dms,
    write_bytes,
    write_jsonl,
    write_manifest,
)
from scripts.external_data.xlsx import read_first_sheet

DATASET_ID = "zenodo-parlung-zangbo-glacial-lakes"
ZENODO_RECORD = 20305493
ZENODO_FILE = "datasets.zip"
DOI = "10.5281/zenodo.20305493"
LICENSE_ID = "cc-by-4.0"
LICENSE_URL = "https://creativecommons.org/licenses/by/4.0/"

_INNER = "datasets/Parlung_Zangbo_Basin_Glacial_Lake_Inventory_{year}.xlsx"


def _record_meta() -> dict[str, Any]:
    import json

    payload = json.loads(download(f"https://zenodo.org/api/records/{ZENODO_RECORD}", timeout=60).decode("utf-8"))
    metadata = payload.get("metadata", {})
    return {
        "title": metadata.get("title", ""),
        "creators": [c.get("name", "") for c in metadata.get("creators", [])],
        "license": (metadata.get("license") or {}).get("id", ""),
    }


def _to_float(value: Any) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _records_from_sheet(rows: list[dict[str, Any]], year: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for index, row in enumerate(rows, start=1):
        longitude = parse_dms(str(row.get("X coordinate", "")))
        latitude = parse_dms(str(row.get("Y coordinate", "")))
        if longitude is None or latitude is None:
            # 2020 版用 DMS 字符串；2025 版把 DMS 放在 X/Y coordinate，仍是度分秒。
            continue
        out.append(
            {
                "schema_version": "1.0.0",
                "record_id": f"{DATASET_ID}:{year}:{index:04d}",
                "source": {
                    "dataset": DATASET_ID,
                    "inventory_year": year,
                    "source_name": str(row.get("FID", "")) or None,
                    "source_url": f"https://zenodo.org/records/{ZENODO_RECORD}",
                    "license": LICENSE_ID,
                },
                "event": {
                    "date": None,
                    "title": str(row.get("Nearest glacier", "") or "") or None,
                    "description": None,
                    "location": "帕隆藏布流域（雅鲁藏布江支流，藏东南）",
                    "location_accuracy": None,
                    "storm_name": None,
                    "notes": None,
                },
                "geo": {
                    "latitude": latitude,
                    "longitude": longitude,
                    "country": "China",
                    "admin_division": "Tibet Autonomous Region",
                },
                "hazard": {
                    "type": "lake_outburst",
                    "source_category": "glacial_lake",
                    "attributes": {
                        "inventory_year": year,
                        "surface_area_km2": _to_float(row.get("Area")),
                        "volume_1e6_m3": _to_float(row.get("Volume")),
                        "elevation_min_m": _to_float(row.get("Z_Min")),
                        "elevation_max_m": _to_float(row.get("Z_Max")),
                        "elevation_mean_m": _to_float(row.get("Z_Mean")),
                        "slope_min_deg": _to_float(row.get("Min_Slope")),
                        "slope_max_deg": _to_float(row.get("Max_Slope")),
                        "slope_avg_deg": _to_float(row.get("Avg_Slope")),
                        "nearest_glacier_m": _to_float(row.get("Nearest glacier")),
                        "glacier_contact": str(row.get("Presence", "") or "") or None,
                        "lake_type": str(row.get("Glacial lake type", "") or "") or None,
                        "lake_classification": str(row.get("Glacial lake classification", "") or "") or None,
                        "projected_x": _to_float(row.get("Centroid X coordinate")),
                        "projected_y": _to_float(row.get("Centroid Y coordinate")),
                    },
                },
                "impact": {"fatalities": None, "injuries": None},
            }
        )
    return out


def spec(meta: dict[str, Any]) -> DatasetSpec:
    return DatasetSpec(
        id=DATASET_ID,
        title="帕隆藏布流域冰湖清单（2020 / 2025 两期）",
        publisher="Zenodo（individual contributors）",
        authors=meta.get("creators", []),
        license_id=LICENSE_ID,
        license_url=LICENSE_URL,
        commercial_use=True,
        license_note="CC BY 4.0：允许署名后的任何用途，含商业使用与再分发（须保留署名与许可链接）。",
        citation=f"{meta.get('title', '')}. Zenodo. https://doi.org/{DOI}. CC BY 4.0.",
        landing_url=f"https://zenodo.org/records/{ZENODO_RECORD}",
        doi=DOI,
        source_urls={
            "api": f"https://zenodo.org/api/records/{ZENODO_RECORD}",
            "file": f"https://zenodo.org/api/records/{ZENODO_RECORD}/files/{ZENODO_FILE}/content",
        },
        selection={
            "scope": "两期清单的全部记录（2020 与 2025）",
            "exclusions": [],
        },
        normalization={
            "coord_rule": "取 X/Y coordinate 的度分秒字符串解析为十进制度；投影坐标列原值保留",
            "missing_rule": "无坐标的记录跳过并在 manifest 记数（源表里坐标列可能为空）",
        },
        known_limitations=[
            "**是冰湖清单，不是溃决事件**：没有溃决发生时间、伤亡、损失字段。",
            "只覆盖帕隆藏布流域（藏东南），**不覆盖全藏冰湖**。",
            "两期清单的字段不完全一致（2025 版多了体积与冰湖分类），缺失项留 null。",
        ],
    )


def refresh() -> Path:
    meta = _record_meta()
    root = dataset_dir(DATASET_ID)
    archive = download(f"https://zenodo.org/api/records/{ZENODO_RECORD}/files/{ZENODO_FILE}/content")

    files: dict[str, FileMeta] = {
        "raw/parlung_zangbo_glacial_lakes.zip": write_bytes(archive, root / "raw" / "parlung_zangbo_glacial_lakes.zip")
    }
    records: list[dict[str, Any]] = []
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        for year in (2020, 2025):
            inner_name = _INNER.format(year=year)
            inner_bytes = bundle.read(inner_name)
            files[f"raw/parlung_glacial_lakes_{year}.xlsx"] = write_bytes(inner_bytes, root / "raw" / f"parlung_glacial_lakes_{year}.xlsx")
            rows = read_first_sheet(inner_bytes)
            records.extend(_records_from_sheet(rows, year))
    files["normalized/parlung_zangbo_glacial_lakes.v1.jsonl"] = write_jsonl(
        records, root / "normalized" / "parlung_zangbo_glacial_lakes.v1.jsonl"
    )
    manifest_path = write_manifest(spec(meta), files)
    print(f"规范化 {len(records)} 个冰湖（2020 + 2025）")
    print(f"manifest: {manifest_path}")
    return manifest_path


def check() -> list[str]:
    from scripts.external_data.core import verify_dataset

    return verify_dataset(DATASET_ID)


if __name__ == "__main__":  # pragma: no cover
    import json

    print(json.dumps(check(), ensure_ascii=False))
