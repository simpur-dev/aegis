"""Zenodo《Glacier Lake Outburst Flood Database V3.1》（CC BY 4.0）。

**这是什么**：Lützow, Veh & Korup (2023, ESSD) 维护的全球冰湖溃决**事件库**——
按 8 个冰川区分为 8 张工作表，共 **3,151 条**事件，每条给地点、日期、溃决机制、
湖泊体积/洪峰流量（部分估算）、影响与死亡人数、参考文献。

**为什么重要**：平台的五灾种里有 `lake_outburst`，此前仓里只有冰湖**清单**
（帕隆藏布 3,224 个湖）与 TPDC 的 19 个溃决隐患湖，**没有溃决事件**。这套补上的正是事件维度。

**许可**：CC BY 4.0（Zenodo 记录 `license.id=cc-by-4.0`，匿名可下、可再分发）。
库的 README 说明底层湖泊清单来自各来源、可能另有许可——本目录只收**编译后的库**
（CC BY 4.0），不拆底层清单。

**一个必须说清的差异**：外部交付的标准化 CSV 标称 3,167 条，实际把每张表的
**字段说明行 + 示例行**都当成了数据（8 表 × 2 行 = 16 条幽灵记录）。
本目录直接读原始 ODS、跳过前 3 行，得 **3,151 条**；坐标数与说明一致（3,062 条有坐标）。
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandera.polars as pa
import polars as pl

from scripts.external_data.core import (
    DatasetSpec,
    FileMeta,
    dataset_dir,
    download,
    write_bytes,
    write_envelope,
    write_manifest,
)
from scripts.external_data.table import read_all_sheets

DATASET_ID = "glof-db"
ZENODO_RECORD = 7924369
ZENODO_FILE = "glofdatabase_V3-1.ods"
DOI = "10.5281/zenodo.7924369"
LANDING_URL = f"https://zenodo.org/records/{ZENODO_RECORD}"
DOWNLOAD_URL = f"https://zenodo.org/api/records/{ZENODO_RECORD}/files/{ZENODO_FILE}/content"
LICENSE_ID = "cc-by-4.0"
LICENSE_URL = "https://creativecommons.org/licenses/by/4.0/"

#: 每张表的前 3 行：0=表头，1=字段说明，2=示例行（"e.g. …"）。数据从 3 开始。
_DATA_START = 3

#: 列名 → 短名（只取规范化用到的列；其余列原样不进 normalized）。
_COLUMNS = {
    "ID": "ordinal",
    "Country": "country",
    "Glacier": "glacier",
    "RGI_Glacier_Id": "rgi_glacier_id",
    "Lake": "lake",
    "Lake_type": "lake_type",
    "Longitude": "longitude",
    "Latitude": "latitude",
    "River": "river",
    "Date": "date",
    "Date_Min": "date_min",
    "Date_Max": "date_max",
    "Mechanism": "mechanism",
    "Mean_Lake_Volume_VL": "mean_lake_volume_vl",
    "Mean_Flood_Volume_V0": "mean_flood_volume_v0",
    "Peak_discharge_Qp": "peak_discharge_qp",
    "reported_fatalities": "reported_fatalities",
    "Certainty_level_before": "certainty_before",
    "Certainty_level_after": "certainty_after",
    "Reference": "reference",
    "Further_comments": "comments",
}

RAW_SCHEMA = pa.DataFrameSchema(
    columns={
        "ordinal": pa.Column(pl.Int64, pa.Check.ge(1)),
        "country": pa.Column(pl.String, nullable=True),
        "lake": pa.Column(pl.String, nullable=True),
        "longitude": pa.Column(pl.Float64, pa.Check.in_range(-180.0, 180.0), nullable=True),
        "latitude": pa.Column(pl.Float64, pa.Check.in_range(-90.0, 90.0), nullable=True),
        "reported_fatalities": pa.Column(pl.Float64, pa.Check.ge(0.0), nullable=True),
    },
    strict=True,
)


def _slug(name: str) -> str:
    return name.lower().replace(" ", "-")


def _sheet_rows(rows: list[list[Any]]) -> list[dict[str, Any]]:
    """按位置把原始行映射成字典：跳过前 3 行，丢弃空列与整行空。"""
    header = [("" if cell is None else str(cell)).strip() for cell in rows[0]]
    index = {name: position for position, name in enumerate(header) if name}
    out: list[dict[str, Any]] = []
    for row in rows[_DATA_START:]:
        if not any(cell is not None and cell != "" for cell in row):
            continue
        out.append(
            {short: (row[index[source]] if index[source] < len(row) else None) for source, short in _COLUMNS.items() if source in index}
        )
    return out


def _iso_date(value: Any) -> tuple[str | None, str]:
    """日期归一：能确定到日才给 ISO，只有年份就留 `date=None`（不臆造月日）。"""
    if value is None or value == "":
        return None, ""
    if isinstance(value, datetime):
        return value.date().isoformat(), value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat(), value.isoformat()
    if isinstance(value, int | float):
        year = int(value)
        # 只有年份：`event.date` 必须留空（信封要求 ISO 日期或 null），年份进 date_raw
        return (None, f"{year:04d}") if 1000 <= year <= 2100 else (None, str(value))
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d.%m.%Y", "%Y"):
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if fmt == "%Y":
            return None, f"{parsed.year:04d}"
        return parsed.date().isoformat(), text
    return None, text


def _float_or_none(value: Any) -> float | None:
    if isinstance(value, int | float):
        return float(value)
    text = str(value).strip() if value is not None else ""
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _validated_frame(records: list[dict[str, Any]]) -> pl.DataFrame:
    frame = pl.DataFrame(
        {
            "ordinal": [_float_or_none(row.get("ordinal")) for row in records],
            "country": [str(row.get("country") or "").strip() or None for row in records],
            "lake": [str(row.get("lake") or "").strip() or None for row in records],
            "longitude": [_float_or_none(row.get("longitude")) for row in records],
            "latitude": [_float_or_none(row.get("latitude")) for row in records],
            "reported_fatalities": [_float_or_none(row.get("reported_fatalities")) for row in records],
        },
        schema_overrides={
            "ordinal": pl.Float64,
            "longitude": pl.Float64,
            "latitude": pl.Float64,
            "reported_fatalities": pl.Float64,
        },
    ).with_columns(pl.col("ordinal").cast(pl.Int64, strict=False))
    return RAW_SCHEMA.validate(frame)


def _records(sheets: dict[str, list[list[Any]]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for sheet_name, raw_rows in sheets.items():
        for row in _sheet_rows(raw_rows):
            row["_region"] = sheet_name
            rows.append(row)
    _validated_frame(rows)  # 类型/值域不过就当场失败，不落盘

    out: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for row in rows:
        ordinal = _float_or_none(row.get("ordinal"))
        if ordinal is None:
            raise SystemExit(f"{row['_region']} 有记录 ID 不是数字：{row.get('ordinal')!r}")
        ordinal_int = int(ordinal)
        key = (row["_region"], ordinal_int)
        if key in seen:
            raise SystemExit(f"重复 ID：{row['_region']} #{ordinal_int}")
        seen.add(key)

        date_iso, date_raw = _iso_date(row.get("date"))
        lon = _float_or_none(row.get("longitude"))
        lat = _float_or_none(row.get("latitude"))
        has_point = lon is not None and lat is not None
        fatalities = _float_or_none(row.get("reported_fatalities"))
        attributes = {
            "database_region": row["_region"],
            "mechanism": str(row.get("mechanism") or "").strip() or None,
            "lake_type": str(row.get("lake_type") or "").strip() or None,
            "river": str(row.get("river") or "").strip() or None,
            "glacier": str(row.get("glacier") or "").strip() or None,
            "rgi_glacier_id": str(row.get("rgi_glacier_id") or "").strip() or None,
            "mean_lake_volume_vl": _float_or_none(row.get("mean_lake_volume_vl")),
            "mean_flood_volume_v0": _float_or_none(row.get("mean_flood_volume_v0")),
            "peak_discharge_qp": _float_or_none(row.get("peak_discharge_qp")),
            "date_min_raw": str(row.get("date_min") or "").strip() or None,
            "date_max_raw": str(row.get("date_max") or "").strip() or None,
            "certainty_before": str(row.get("certainty_before") or "").strip() or None,
            "certainty_after": str(row.get("certainty_after") or "").strip() or None,
            "reference": str(row.get("reference") or "").strip() or None,
            "place_has_coordinates": has_point,
        }
        if date_iso is None and date_raw[:4].isdigit():
            attributes["year"] = int(date_raw[:4])
        out.append(
            {
                "schema_version": "1.0.0",
                "record_id": f"{DATASET_ID}:{_slug(row['_region'])}:{ordinal_int}",
                "source": {
                    "dataset": DATASET_ID,
                    "event_id": f"{_slug(row['_region'])}:{ordinal_int}",
                    "source_name": "Glacier Lake Outburst Flood Database V3.1 (Lützow, Veh & Korup 2023)",
                    "source_url": LANDING_URL,
                    "license": LICENSE_ID,
                },
                "event": {
                    "date": date_iso,
                    "date_raw": date_raw or None,
                    "title": str(row.get("lake") or "").strip() or None,
                    "description": str(row.get("comments") or "").strip() or None,
                    "location": str(row.get("country") or "").strip() or None,
                    "location_accuracy": None,
                    "storm_name": None,
                    "notes": None,
                },
                "geo": {
                    "latitude": lat if has_point else None,
                    "longitude": lon if has_point else None,
                    "country": str(row.get("country") or "").strip() or None,
                    "admin_division": row["_region"],
                },
                "hazard": {
                    "type": "lake_outburst",
                    "source_category": "glof",
                    "attributes": attributes,
                },
                "impact": {
                    "fatalities": int(fatalities) if fatalities is not None else None,
                    "injuries": None,
                },
            }
        )
    return out


def _record_meta() -> dict[str, Any]:
    import json

    payload = json.loads(download(f"https://zenodo.org/api/records/{ZENODO_RECORD}", timeout=60).decode("utf-8"))
    metadata = payload.get("metadata", {})
    return {
        "title": metadata.get("title", ""),
        "creators": [c.get("name", "") for c in metadata.get("creators", [])],
        "license": (metadata.get("license") or {}).get("id", ""),
    }


def spec(meta: dict[str, Any]) -> DatasetSpec:
    return DatasetSpec(
        id=DATASET_ID,
        title="Glacier Lake Outburst Flood Database V3.1（全球冰湖溃决事件库）",
        publisher="Zenodo（University of Potsdam）",
        authors=meta.get("creators", []),
        license_id=LICENSE_ID,
        license_url=LICENSE_URL,
        commercial_use=True,
        license_note="CC BY 4.0：允许署名后的再分发与商业使用（须保留署名与许可链接）。",
        citation=(
            "Lützow, N., Veh, G., & Korup, O. (2023). A global database of historic glacier lake outburst floods. "
            f"Earth System Science Data, 15, 2983–3000. https://doi.org/10.5194/essd-15-2983-2023; "
            f"database: https://doi.org/{DOI} (CC BY 4.0)."
        ),
        landing_url=LANDING_URL,
        doi=DOI,
        source_urls={"file": DOWNLOAD_URL},
        selection={
            "scope": "8 个区域工作表的全部事件（每表跳过前 3 行：表头/字段说明/示例行）",
            "exclusions": [],
        },
        normalization={
            "read_stack": "python-calamine 读 ODS 全部工作表 + pandera（polars 后端）校验 ID/坐标/伤亡",
            "date_rule": "能确定到日才写 event.date（ISO）；只有年份写 attributes.year，不臆造月日",
            "geo_rule": "经纬度必须成对且为数值才写；否则 lat/lon 均 null 并置 place_has_coordinates=false",
            "count_note": "原始 ODS 得 3,151 条；外部标准化 CSV 的 3,167 条把每表字段说明行与示例行也当成了数据（+16）",
        },
        known_limitations=[
            "**事件库不是完整普查**：以文献/报告/新闻为线索，无人区与无记录事件缺席。",
            "约 3% 的记录没有可用坐标（3,151 中 3,062 有坐标）；影响与伤亡字段部分缺失。",
            "湖泊体积/洪峰流量多为估算（有 `VL_calculation`/`Qp_calculation` 口径字段，未逐条附）。",
            "库的 README 明确底层湖泊清单来自各自来源、可能另有许可；本目录只收编译后的库。",
        ],
    )


def refresh(source: Path | None = None) -> Path:
    meta = _record_meta()
    raw = source.read_bytes() if source is not None else download(DOWNLOAD_URL)
    records = _records(read_all_sheets(raw))
    root = dataset_dir(DATASET_ID)
    files: dict[str, FileMeta] = {
        "raw/glofdatabase_V3-1.ods": write_bytes(raw, root / "raw" / "glofdatabase_V3-1.ods"),
        "normalized/glof_events.v1.jsonl": write_envelope(records, root / "normalized" / "glof_events.v1.jsonl"),
    }
    manifest_path = write_manifest(spec(meta), files)
    print(f"规范化 {len(records)} 条冰湖溃决事件（8 个区域表）")
    print(f"manifest: {manifest_path}")
    return manifest_path


def check() -> list[str]:
    from scripts.external_data.core import verify_dataset

    return verify_dataset(DATASET_ID)


if __name__ == "__main__":  # pragma: no cover
    import json

    print(json.dumps(check(), ensure_ascii=False))
