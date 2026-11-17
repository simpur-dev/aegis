"""TPDC《喜马拉雅山地区重大地质灾害统计数据集》（CC BY-NC-SA 4.0）。

**这是目前西藏地质灾害点位质量最高的公开数据**：525 个调查点（泥石流 353 / 滑坡 153 /
溃决隐患冰川终碛湖 19），带**专业属性**——滑坡有前缘/后缘高程、滑体坡度、主滑方向、
平面面积、地层；泥石流有规模、沟口高程、坡向、流域形态、比降、发育阶段、水源类型；
冰湖有海拔、面积、成灾风险。坐标是 WGS84 点位，覆盖喜马拉雅山全段。

数据来自《喜马拉雅山地区重大地质灾害遥感调查报告》的数字化成果（童立强等，2006–2009）。

**许可**：CC BY-NC-SA 4.0——非商业 + 相同方式共享。与仓库 AGPL-3.0 隔离放置，
商用发行前必须整目录移除。

抓取：ScienceDB/TPDC 公开接口（`batchDownloadFile` + `{"noToken":true}`），无需登录。
"""

from __future__ import annotations

import json
import struct
import zipfile
from pathlib import Path
from typing import Any

from scripts.external_data.core import (
    DatasetSpec,
    FileMeta,
    dataset_dir,
    post_json,
    write_bytes,
    write_jsonl,
    write_manifest,
)

DATASET_ID = "tpdc-himalaya-geohazard-2006-2009"
METADATA_ID = "a2128e3f-a913-40ba-a7b1-3318cb4031d5"
LANDING_URL = f"https://data.tpdc.ac.cn/en/data/{METADATA_ID}/"
DOI = "10.11888/Disas.tpdc.271000"
CSTR = "18406.11.Disas.tpdc.271000"
DOWNLOAD_URL = f"https://data.tpdc.ac.cn/file/file/batchDownloadFile?metadataId={METADATA_ID}"
METADATA_URL = f"https://data.tpdc.ac.cn/view/metadataView/detail/?metadataId={METADATA_ID}"

#: 源 shapefile 三个图层 → 项目 HazardType。
LAYER_TYPES: dict[str, str] = {
    "泥石流": "debris_flow",
    "滑坡": "landslide",
    "溃决隐患冰川终碛湖": "lake_outburst",
}

#: 各图层的 DBF 数值字段 → 规范化字段名。DBF 里的数字是**科学计数法字符串**
#: （如 `7.92296416667e+001`），直接 float() 即可。
DEBRIS_FIELDS = {
    "沟口_高程_": "mouth_elevation_m",
    "比降__": "channel_gradient",
    "发育_阶段": "development_stage",
}
LANDSLIDE_FIELDS = {
    "前缘高程": "toe_elevation_m",
    "后缘高程": "head_elevation_m",
    "滑体坡度_": "slope_angle_deg",
    "主滑方向_": "aspect_deg",
    "平面面积_": "plan_area",
    "地层": "stratum",
}
LAKE_FIELDS = {
    "湖面海拔_m": "surface_elevation_m",
    "冰湖面积_": "surface_area",
    "成灾风险": "outburst_risk",
}


def _read_dbf(raw: bytes) -> tuple[list[tuple[str, str, int]], list[dict[str, str]]]:
    """读 DBF：返回 (字段定义, 行字典)。DBF 每条记录首字节是删除标志，别漏。"""
    record_count = int.from_bytes(raw[4:8], "little")
    header_len = int.from_bytes(raw[8:10], "little")
    record_len = int.from_bytes(raw[10:12], "little")
    fields: list[tuple[str, str, int]] = []
    pos = 32
    while raw[pos] != 0x0D:
        descriptor = raw[pos : pos + 32]
        name = descriptor[:11].split(b"\x00")[0].decode("gb18030", "replace")
        fields.append((name, chr(descriptor[11]), descriptor[16]))
        pos += 32
    rows: list[dict[str, str]] = []
    for index in range(record_count):
        offset = header_len + index * record_len + 1  # +1 跳过删除标志
        values: dict[str, str] = {}
        for name, _kind, length in fields:
            values[name] = raw[offset : offset + length].decode("gb18030", "replace").strip()
            offset += length
        rows.append(values)
    return fields, rows


def _read_shp_points(raw: bytes) -> list[tuple[float, float]]:
    """读 shapefile 里的点几何（Point/MultiPoint）。

    记录布局：记录号(4B BE) + 内容长度(4B BE, 以 16-bit 字计) + 内容(内容长度×2 字节)，
    **内容首 4 字节就是 shape type**。下一条记录从「内容起点 + 内容长度×2」开始。
    """
    points: list[tuple[float, float]] = []
    offset = 100  # 跳过文件头
    while offset < len(raw):
        _record_no, content_len = struct.unpack(">II", raw[offset : offset + 8])
        content_start = offset + 8
        shape_type = struct.unpack("<I", raw[content_start : content_start + 4])[0]
        if shape_type == 1:  # Point
            x, y = struct.unpack("<dd", raw[content_start + 4 : content_start + 20])
            points.append((x, y))
        elif shape_type == 8:  # MultiPoint
            count = struct.unpack("<I", raw[content_start + 4 : content_start + 8])[0]
            for i in range(count):
                base = content_start + 8 + i * 16
                x, y = struct.unpack("<dd", raw[base : base + 16])
                points.append((x, y))
        offset = content_start + content_len * 2
    return points


def _number(value: str) -> float | None:
    """DBF 数值字段是科学计数法字符串；空值 → None（源数据没填，不是 0）。"""
    text = (value or "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _records_from_layer(name: str, archive: zipfile.ZipFile, prefix: str) -> list[dict[str, Any]]:
    _fields, rows = _read_dbf(archive.read(prefix + name + ".dbf"))
    geometry = _read_shp_points(archive.read(prefix + name + ".shp"))
    if len(geometry) != len(rows):
        raise SystemExit(f"{name}: 几何点数({len(geometry)})与属性行数({len(rows)})不符，源数据变了")
    hazard_type = LAYER_TYPES[name]
    numeric_map = {"泥石流": DEBRIS_FIELDS, "滑坡": LANDSLIDE_FIELDS, "溃决隐患冰川终碛湖": LAKE_FIELDS}[name]
    out: list[dict[str, Any]] = []
    for index, (row, (lon, lat)) in enumerate(zip(rows, geometry, strict=True), start=1):
        attributes: dict[str, Any] = {}
        for source_name, target_name in numeric_map.items():
            value = row.get(source_name, "")
            number = _number(value)
            attributes[target_name] = number if number is not None else (value or None)
        # 源数据有 2 个纬度明显异常（83.20° / 81.52°，西藏不可能）——同一数据集的
        # 表格里也是这个值（首位数字笔误）。口径：**不静默改源数据，但必须看得见**，
        # 所以打 quality_flags，让下游能筛。
        quality_flags: list[str] = []
        if not (27.0 <= lat <= 37.0):
            quality_flags.append("latitude_out_of_range")
        if not (78.0 <= lon <= 100.0):
            quality_flags.append("longitude_out_of_range")
        geo: dict[str, Any] = {
            "latitude": lat,
            "longitude": lon,
            "country": "China",
            "admin_division": "Tibet Autonomous Region",
        }
        if quality_flags:
            geo["quality_flags"] = quality_flags
        out.append(
            {
                "schema_version": "1.0.0",
                "record_id": f"{DATASET_ID}:{name}:{index:04d}",
                "source": {
                    "dataset": DATASET_ID,
                    "layer": name,
                    "license": "cc-by-nc-sa-4.0",
                },
                "event": {
                    "date": None,
                    "date_start": "2006-01-01",
                    "date_end": "2009-12-31",
                    "title": row.get("名称") or row.get("冰湖名称") or None,
                    "description": None,
                    "location": row.get("所在地区") or None,
                    "location_accuracy": None,
                    "storm_name": None,
                    "notes": None,
                },
                "geo": geo,
                "hazard": {
                    "type": hazard_type,
                    "source_category": name,
                    "scale": row.get("规模") or None,
                    "attributes": attributes,
                },
                "impact": {"fatalities": None, "injuries": None},
            }
        )
    return out


def spec() -> DatasetSpec:
    return DatasetSpec(
        id=DATASET_ID,
        title="喜马拉雅山地区重大地质灾害统计数据集（2006–2009）",
        publisher="国家青藏高原科学数据中心（TPDC）",
        authors=["童立强（自然资源部航空物探遥感中心）等"],
        license_id="cc-by-nc-sa-4.0",
        license_url="https://creativecommons.org/licenses/by-nc-sa/4.0/",
        commercial_use=False,
        license_note=("CC BY-NC-SA 4.0：非商业 + 相同方式共享。派生数据须沿用同一许可；商用发行前必须整目录移除，或另行取得授权。"),
        citation=(
            "童立强等. 喜马拉雅山地区重大地质灾害统计数据集[DS/OL]. "
            f"国家青藏高原科学数据中心, 2020. https://doi.org/{DOI}. CSTR:{CSTR}. CC BY-NC-SA 4.0."
        ),
        landing_url=LANDING_URL,
        doi=DOI,
        cstr=CSTR,
        source_urls={"metadata_api": METADATA_URL, "download_api": DOWNLOAD_URL},
        selection={
            "scope": "三个 shapefile 图层的全部点：泥石流 353 / 滑坡 153 / 溃决隐患冰川终碛湖 19",
            "exclusions": [],
        },
        normalization={
            "raw_encoding": "GB18030（shapefile DBF）",
            "geometry": "ESRI shapefile 点几何（WGS84）",
            "numeric_rule": "DBF 数值字段是科学计数法字符串，float() 直解；空值 → null（源未填）",
            "category_map": LAYER_TYPES,
            "attribute_map": {"泥石流": DEBRIS_FIELDS, "滑坡": LANDSLIDE_FIELDS, "溃决隐患冰川终碛湖": LAKE_FIELDS},
        },
        known_limitations=[
            "数据是 2006–2009 年遥感调查报告的数字化成果，不代表当前状况。",
            "525 个点（353+153+19）与数据集说明的『540 余处』有出入：差额来自表格统计行，本目录只收了有空间坐标的图层点，不补造。",
            "两个点的纬度明显异常（83.20° / 81.52°，西藏不可能）——同一数据集的表格里也是这个值"
            "（首位数字笔误）。我们**不改源数据**，但给这两条打 `geo.quality_flags` 标记。",
            "只有点位与形态学属性，**没有伤亡/经济损失/发生日期**；不能当灾情事件库用。",
        ],
    )


def refresh() -> Path:
    archive_bytes = post_json(DOWNLOAD_URL, {"noToken": True})
    if len(archive_bytes) < 1000:
        raise SystemExit(f"下载到的字节数异常（{len(archive_bytes)}），上游可能改了接口")
    root = dataset_dir(DATASET_ID)
    raw_meta = write_bytes(archive_bytes, root / "raw" / "tpdc_himalaya_geohazard.zip")

    prefix = "喜马拉雅山地区重大地质灾害统计数据集/喜马拉雅山重大地质灾害数据集/"
    records: list[dict[str, Any]] = []
    with zipfile.ZipFile(root / "raw" / "tpdc_himalaya_geohazard.zip") as archive:
        for layer in LAYER_TYPES:
            records.extend(_records_from_layer(layer, archive, prefix))
    normalized = write_jsonl(records, root / "normalized" / "himalaya_geohazard_sites.v1.jsonl")

    metadata_path = root / "raw" / "tpdc_metadata.json"
    try:
        metadata_bytes = post_json(METADATA_URL, {"metadataId": METADATA_ID})
        metadata_path.write_bytes(metadata_bytes)
    except OSError:  # pragma: no cover - 元数据拿不到不该阻断数据入库
        metadata_bytes = b""
    files: dict[str, FileMeta] = {
        "raw/tpdc_himalaya_geohazard.zip": raw_meta,
        "normalized/himalaya_geohazard_sites.v1.jsonl": normalized,
    }
    if metadata_bytes:
        files["raw/tpdc_metadata.json"] = write_bytes(metadata_bytes, metadata_path)
    manifest_path = write_manifest(spec(), files)
    print(f"规范化 {len(records)} 个点位 → {normalized.sha256[:12]}…")
    print(f"manifest: {manifest_path}")
    return manifest_path


def check() -> list[str]:
    from scripts.external_data.core import verify_dataset

    return verify_dataset(DATASET_ID)


if __name__ == "__main__":  # pragma: no cover
    print(json.dumps(check(), ensure_ascii=False))
