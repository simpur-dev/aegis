"""NASA Global Landslide Catalog（中国 / 西藏子集，public domain）。

美国政府作品，不受版权保护，可再分发。这是本仓第一个收进来的外部灾情目录，
也是**唯一一条没有许可限制**的历史事件源。

选它的理由：能匿名复跑、字段规范（含坐标/触发/伤亡）、有 `event_id` 可回溯。
不选它的理由（也写下来）：只收**降雨触发**的滑坡/泥石流，以媒体报道为线索，
事件日期止于 2016-06；雪崩与冰湖溃决不在覆盖内。

抓取：NASA Open Data Portal 的静态导出快照（`data.nasa.gov/docs/legacy/...`）。
"""

from __future__ import annotations

import csv
import io
from pathlib import Path
from typing import Any

from scripts.external_data.core import (
    DatasetSpec,
    FileMeta,
    count_rows,
    dataset_dir,
    download,
    write_bytes,
    write_envelope,
    write_manifest,
)

DATASET_ID = "nasa-glc"
SOURCE_URL = "https://data.nasa.gov/docs/legacy/Global_Landslide_Catalog_Export/Global_Landslide_Catalog_Export_rows.csv"
LICENSE_ID = "public-domain"
LICENSE_URL = "https://www.nasa.gov/nasa-brand-center/images-and-media/"

#: 源 CSV 的 31 列（缺列即中止：上游字段变了要先核对，不能默默按旧列名取值）。
RAW_FIELDS: tuple[str, ...] = (
    "source_name",
    "source_link",
    "event_id",
    "event_date",
    "event_time",
    "event_title",
    "event_description",
    "location_description",
    "location_accuracy",
    "landslide_category",
    "landslide_trigger",
    "landslide_size",
    "landslide_setting",
    "fatality_count",
    "injury_count",
    "storm_name",
    "photo_link",
    "notes",
    "event_import_source",
    "event_import_id",
    "country_name",
    "country_code",
    "admin_division_name",
    "admin_division_population",
    "gazeteer_closest_point",
    "gazeteer_distance",
    "submitted_date",
    "created_date",
    "last_edited_date",
    "longitude",
    "latitude",
)

#: 源 category → 项目 HazardType。`complex`（复合型）归 landslide，原文保留在 source_category。
CATEGORY_MAP: dict[str, str] = {
    "landslide": "landslide",
    "translational_slide": "landslide",
    "mudslide": "debris_flow",
    "debris_flow": "debris_flow",
    "rock_fall": "rockfall",
    "complex": "landslide",
    "other": "unknown",
    "unknown": "unknown",
    "": "unknown",
}

#: 西藏子集的**人工排除项**：源数据 admin_division 标成西藏，但地点文本明确在境外。
#: 不静默改正源数据，也不把错标当事实带进 normalized——排除理由写在这里，可复现、可复核。
TIBET_EXCLUSIONS: dict[str, str] = {
    "4392": "admin_division 标 Tibet，但 location 为 Dibang, Arunachal Pradesh（印度）",
    "7108": "admin_division 标 Tibet，但 location 为 Tatopani/Araniko Highway（尼泊尔，2015 尼泊尔地震）",
}


def _count(value: str) -> int | None:
    """空字符串 = 源数据未报 → None；`0` = 明确报了零。**两者不能混**。"""
    text = (value or "").strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _date(value: str) -> str:
    text = (value or "").strip()
    date_part = text.partition(" ")[0]
    if not date_part:
        return ""
    try:
        month, day, year = date_part.split("/")
        return f"{year}-{int(month):02d}-{int(day):02d}"
    except ValueError:
        return ""


def spec(source_sha256: str, source_bytes: int, source_rows: int) -> DatasetSpec:
    return DatasetSpec(
        id=DATASET_ID,
        title="NASA Global Landslide Catalog (GLC) Export",
        publisher="NASA Goddard Space Flight Center",
        authors=["Kirschbaum, D. B., Adler, R., Hong, Y., Hill, S., & Lerner-Lam, A."],
        license_id=LICENSE_ID,
        license_url=LICENSE_URL,
        commercial_use=True,
        license_note="NASA 作品不受美国版权保护，可自由再分发；NASA 请求引用（非法律义务）。",
        citation=(
            "Kirschbaum, D. B., Adler, R., Hong, Y., Hill, S., & Lerner-Lam, A. (2010). "
            "A global landslide catalog for hazard applications: method, results, and limitations. "
            "Natural Hazards, 52(3), 561-575. doi:10.1007/s11069-009-9401-4"
        ),
        landing_url="https://data.nasa.gov/dataset/global-landslide-catalog-export",
        source_urls={"file": SOURCE_URL},
        selection={
            "china": "country_name == 'China'",
            "tibet": "country_name == 'China' and admin_division_name == 'Tibet Autonomous Region'",
            "tibet_exclusions": [{"event_id": key, "reason": value} for key, value in TIBET_EXCLUSIONS.items()],
        },
        normalization={
            "raw_encoding": "utf-8-sig",
            "date_rule": "event_date 取日期部分（MM/DD/YYYY → YYYY-MM-DD）；时刻原文单列保留",
            "count_rule": "空字符串 → 空（未报）；0 → 0（明确报了零）",
            "category_map": CATEGORY_MAP,
        },
        known_limitations=[
            "data.nasa.gov 的**静态导出快照**，事件日期中国子集止于 2016-10、西藏子集止于 2016-06；上游持续更新版在 COOLR。",
            "以**降雨触发**的滑坡/泥石流为主（另有地震 7 条、采矿 6 条、冰雪融水 4 条等），以媒体报道为线索；雪崩与冰湖溃决不在覆盖内。",
            "有报道的事件才进目录——无人区、无通讯、无伤亡的事件系统性缺失。",
            "坐标精度多为 10–50 km 档（看 location_accuracy），不是点位级。",
        ],
        extra={
            "source_file": {
                "url": SOURCE_URL,
                "sha256": source_sha256,
                "bytes": source_bytes,
                "rows": source_rows,
            }
        },
    )


def refresh(source: Path | None = None) -> Path:
    """重抓并重写 raw / normalized / manifest。

    `source` 给定时从本地副本读（离线复跑用），不给则联网下载。
    """
    raw = source.read_bytes() if source is not None else download(SOURCE_URL)
    text = raw.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    fields = list(reader.fieldnames or [])
    missing = [name for name in RAW_FIELDS if name not in fields]
    if missing:
        raise SystemExit(f"源文件缺列: {missing}；上游字段变了，先核对再改脚本")
    rows = list(reader)

    china = [row for row in rows if (row["country_name"] or "").strip() == "China"]
    tibet_raw = [row for row in china if (row["admin_division_name"] or "").strip() == "Tibet Autonomous Region"]
    tibet_kept = [row for row in tibet_raw if row["event_id"] not in TIBET_EXCLUSIONS]

    def by_date(row: dict[str, str]) -> tuple[str, int]:
        return (_date(row["event_date"]), int(row["event_id"]))

    def normalize(selected: list[dict[str, str]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for row in sorted(selected, key=by_date):
            category = (row["landslide_category"] or "").strip()
            raw_date = (row["event_date"] or "").strip()
            out.append(
                {
                    "schema_version": "1.0.0",
                    "record_id": f"{DATASET_ID}:{row['event_id']}",
                    "source": {
                        "dataset": DATASET_ID,
                        "event_id": row["event_id"],
                        "source_name": (row["source_name"] or "").strip() or None,
                        "source_url": (row["source_link"] or "").strip() or None,
                        "license": LICENSE_ID,
                    },
                    "event": {
                        "date": _date(row["event_date"]),
                        "time_raw": raw_date.partition(" ")[2] or None,
                        "title": (row["event_title"] or "").strip() or None,
                        "description": (row["event_description"] or "").strip() or None,
                        "location": (row["location_description"] or "").strip() or None,
                        "location_accuracy": (row["location_accuracy"] or "").strip() or None,
                        "storm_name": (row["storm_name"] or "").strip() or None,
                        "notes": (row["notes"] or "").strip() or None,
                    },
                    "geo": {
                        "latitude": float(row["latitude"]),
                        "longitude": float(row["longitude"]),
                        "country": (row["country_name"] or "").strip() or None,
                        "admin_division": (row["admin_division_name"] or "").strip() or None,
                    },
                    "hazard": {
                        "type": CATEGORY_MAP.get(category, "unknown"),
                        "source_category": category or None,
                        "attributes": {
                            "trigger": (row["landslide_trigger"] or "").strip() or None,
                            "size": (row["landslide_size"] or "").strip() or None,
                            "setting": (row["landslide_setting"] or "").strip() or None,
                        },
                    },
                    "impact": {
                        "fatalities": _count(row["fatality_count"]),
                        "injuries": _count(row["injury_count"]),
                    },
                }
            )
        return out

    root = dataset_dir(DATASET_ID)
    china_path = root / "raw" / "glc_export_rows.china.csv"
    tibet_path = root / "raw" / "glc_export_rows.tibet_admin_labeled.csv"
    files: dict[str, FileMeta] = {
        "raw/glc_export_rows.china.csv": write_bytes(_csv_bytes(fields, china), china_path),
        "raw/glc_export_rows.tibet_admin_labeled.csv": write_bytes(_csv_bytes(fields, tibet_raw), tibet_path),
    }
    files["normalized/china_landslide_events.v1.jsonl"] = write_envelope(
        normalize(china), root / "normalized" / "china_landslide_events.v1.jsonl"
    )
    files["normalized/tibet_landslide_events.v1.jsonl"] = write_envelope(
        normalize(tibet_kept), root / "normalized" / "tibet_landslide_events.v1.jsonl"
    )
    # raw 投影的行数也登记进去（走真正的 CSV 解析：描述字段里有内嵌换行，数行会数错）。
    for key in ("raw/glc_export_rows.china.csv", "raw/glc_export_rows.tibet_admin_labeled.csv"):
        path = root / key
        files[key] = FileMeta(files[key].sha256, files[key].bytes, rows=count_rows(path))
    manifest_path = write_manifest(
        spec(
            _sha(raw),
            len(raw),
            len(rows),
        ),
        files,
    )
    print(f"源文件 {len(rows)} 行；中国 {len(china)}；西藏（源标注）{len(tibet_raw)}；西藏（规范化）{len(tibet_kept)}")
    print(f"manifest: {manifest_path}")
    return manifest_path


def _sha(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


def _csv_bytes(fields: list[str], rows: list[dict[str, str]]) -> bytes:
    """原始列投影：表头 + 原始行，一个字段不改。"""
    import csv as _csv

    buffer = io.StringIO()
    writer = _csv.DictWriter(buffer, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({name: row.get(name, "") for name in fields})
    return buffer.getvalue().encode("utf-8")


def check() -> list[str]:
    from scripts.external_data.core import verify_dataset

    return verify_dataset(DATASET_ID)


if __name__ == "__main__":  # pragma: no cover
    import json

    print(json.dumps(check(), ensure_ascii=False))
