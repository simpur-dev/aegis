"""PANGAEA《Debris flow magnitudes: a global catalog》（CC BY 3.0）。

**这是什么**：Riley, Bendick & Hyde (2012) 汇编的全球泥石流**规模目录**——
988 条记录，每条给体积（m³）、堆积面积（m²，部分缺失）、是否火后、成因机制、
文献出处与地点名。**不是事件表**：没有坐标（只有地名）、没有伤亡、采样日期只填了
一部分（约一半为 null）。所以它进 `data/external/` 时与 NASA GLC 的泥石流事件
**分目录、分 `source_category`**——把"规模目录条目数"混进"事件数"会让下游统计失真。

**许可**：CC BY 3.0（实测 `doi.pangaea.de` 页面 `conditionsOfAccess=unrestricted`），
可再分发、可商用，须署名。这是本目录里继 NASA GLC 之后第二条无 NC 限制的数据。

**读取栈**：polars 严格读 TSV（先按字符串读入，再显式 cast——源文件的数值列有空值，
自动推断会把整列判成字符串或报错），pandera（polars 后端）校验列/类型/值域；
校验不过就报错，不静默放行。
"""

from __future__ import annotations

import io
import re
from datetime import datetime
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

DATASET_ID = "pangaea-debris-flow-magnitude"
PANGAEA_DOI = "10.1594/PANGAEA.783654"
LANDING_URL = f"https://doi.pangaea.de/{PANGAEA_DOI}"
DOWNLOAD_URL = f"{LANDING_URL}?format=textfile"
LICENSE_ID = "cc-by-3.0"
LICENSE_URL = "https://creativecommons.org/licenses/by/3.0/"

#: 列名（源文件表头；`Area` 出现两次：前者是面积，后者是地点名——按位置区分）。
COL_ORDINAL = "Ord No"
COL_VOLUME = "Vol [m**3]"
COL_AREA = "Area [m**2]"
COL_POST_FIRE = "Post-fire"
COL_ORIGIN = "Origin"
COL_FLOW_ID = "Debris flow ID"
COL_METHOD = "Method comm"
COL_REFERENCE = "Reference"
COL_CODE = "Code"
COL_PLACE = "Area"
COL_SAMPLING_DATE = "Sampling date"
COL_FIRE_EVENT = "Fire event"
COL_COMMENT = "Comment"

#: 泥石流规模目录的 schema 门禁：列、类型、值域、主键唯一。
#: 源文件有两列同名（`Area [m**2]` 与 `Area`），按名字选会撞——所以按**列位置**重命名后再校验。
RAW_SCHEMA = pa.DataFrameSchema(
    columns={
        "ordinal": pa.Column(pl.Int64, pa.Check.ge(1), unique=True),
        "volume_m3": pa.Column(pl.Float64, pa.Check.ge(0)),
        "area_m2": pa.Column(pl.Float64, pa.Check.ge(0), nullable=True),
        "post_fire": pa.Column(pl.String, nullable=True),
        "origin": pa.Column(pl.String, nullable=True),
        "flow_id": pa.Column(pl.String, nullable=True),
        "method": pa.Column(pl.String, nullable=True),
        "reference": pa.Column(pl.String),
        "code": pa.Column(pl.String, nullable=True),
        "place": pa.Column(pl.String),
        "sampling_date": pa.Column(pl.String, nullable=True),
        "fire_event": pa.Column(pl.String, nullable=True),
        "comment": pa.Column(pl.String, nullable=True),
    },
    strict=True,
)

#: 只有年份的采样日期**不臆造月日**（%"Y" 会在 strptime 里变成 1 月 1 日的假精确日期，
#: 实测 33 条；口径与 glof_db._iso_date 同一）：年-only 返回 None，原文留在 date_raw。
_DATE_FORMATS = ("%B %d, %Y", "%b %d, %Y", "%Y-%m-%d")
_YEAR_ONLY = re.compile(r"^(18|19|20)\d{2}$")

#: `Post-fire` 的三段值：yes / no / "no evidence for fire (charcoal not found…)"。
_POST_FIRE_FLAG = {"yes": True, "no": False}


def _read_text(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:  # pragma: no cover - 上游只有 UTF-8
        raise SystemExit(f"PANGAEA 文本不是 UTF-8：{exc}") from exc


def _split_header(text: str) -> tuple[list[str], str]:
    """跳过 PANGAEA 的 `/* DATA DESCRIPTION: … */` 注释块，返回 (列头, 数据正文)。

    列头行以 `Ord No` + 制表符开头；找不到就报错——不猜。
    """
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.startswith(COL_ORDINAL + "\t"):
            header = line.split("\t")
            return header, "\n".join(lines[index:])
    raise SystemExit("PANGAEA 文件里找不到以 'Ord No' 开头的列头行；上游格式变了，先核对")


def read_catalog(raw: bytes) -> pl.DataFrame:
    """TSV → 校验过的 DataFrame（列名统一成英文短名，空串规范成 null）。"""
    header, body = _split_header(_read_text(raw))
    if len(header) != 13:
        raise SystemExit(f"PANGAEA 列数应为 13，实际 {len(header)}：{header}")
    frame = pl.read_csv(
        io.BytesIO(body.encode("utf-8")),
        separator="\t",
        infer_schema_length=0,  # 全部按字符串读入，类型由下面显式 cast（不让推断猜）
    )
    if frame.columns != header:
        raise SystemExit(f"polars 读出的列名与表头不符：{frame.columns} != {header}")
    # 同名列（`Area` 出现两次）不能按名选：按位置改名为固定短名。
    renamed = frame.select(
        [
            pl.col(header[0]).alias("ordinal"),
            pl.col(header[1]).alias("volume_m3"),
            pl.col(header[2]).alias("area_m2"),
            pl.col(header[3]).alias("post_fire"),
            pl.col(header[4]).alias("origin"),
            pl.col(header[5]).alias("flow_id"),
            pl.col(header[6]).alias("method"),
            pl.col(header[7]).alias("reference"),
            pl.col(header[8]).alias("code"),
            pl.col(header[9]).alias("place"),
            pl.col(header[10]).alias("sampling_date"),
            pl.col(header[11]).alias("fire_event"),
            pl.col(header[12]).alias("comment"),
        ]
    ).with_columns(
        [
            pl.col("ordinal").cast(pl.Int64),
            pl.col("volume_m3").cast(pl.Float64, strict=False),
            pl.col("area_m2").cast(pl.Float64, strict=False),
            pl.col("post_fire").fill_null("").str.strip_chars().replace("", None),
            pl.col("place").fill_null("").str.strip_chars(),
        ]
    )
    validated = RAW_SCHEMA.validate(renamed)
    # "地点"不能为空：源目录这一列 988 条全有值；变空说明上游结构改了，直接拦。
    empty_places = validated.filter(pl.col("place").str.strip_chars() == "").height
    if empty_places:
        raise SystemExit(f"PANGAEA 有 {empty_places} 条地点为空；上游结构变了，先核对")
    return validated


def _parse_date(text: str | None) -> str | None:
    value = (text or "").strip()
    if not value:
        return None
    if _YEAR_ONLY.match(value):
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _records(frame: pl.DataFrame) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for row in frame.iter_rows(named=True):
        ordinal = int(row["ordinal"])
        place = (row["place"] or "").strip() or None
        post_fire_raw = row["post_fire"]
        records.append(
            {
                "schema_version": "1.0.0",
                "record_id": f"{DATASET_ID}:{ordinal}",
                "source": {
                    "dataset": DATASET_ID,
                    "event_id": str(ordinal),
                    "source_name": "PANGAEA 783654 (Riley et al. 2012)",
                    "source_url": LANDING_URL,
                    "license": LICENSE_ID,
                },
                "event": {
                    "date": _parse_date(row["sampling_date"]),
                    "date_raw": (row["sampling_date"] or "").strip() or None,
                    "title": place,
                    "description": (row["comment"] or "").strip() or None,
                    "location": place,
                    "location_accuracy": None,
                    "storm_name": None,
                    "notes": (row["fire_event"] or "").strip() or None,
                },
                "geo": {
                    # 源目录只有地名、没有坐标——留 null，不臆造。
                    "latitude": None,
                    "longitude": None,
                    "country": None,
                    "admin_division": None,
                },
                "hazard": {
                    "type": "debris_flow",
                    "source_category": "debris_flow_magnitude_catalog",
                    "attributes": {
                        "volume_m3": row["volume_m3"],
                        "area_m2": row["area_m2"],
                        "post_fire": post_fire_raw,
                        "post_fire_flag": _POST_FIRE_FLAG.get((post_fire_raw or "").strip()),
                        "origin": (row["origin"] or "").strip() or None,
                        "method": (row["method"] or "").strip() or None,
                        "reference": (row["reference"] or "").strip() or None,
                        "code": (row["code"] or "").strip() or None,
                        "debris_flow_id": (row["flow_id"] or "").strip() or None,
                    },
                },
                "impact": {"fatalities": None, "injuries": None},
            }
        )
    return records


def spec() -> DatasetSpec:
    return DatasetSpec(
        id=DATASET_ID,
        title="Debris flow magnitudes: a global catalog（全球泥石流规模目录）",
        publisher="PANGAEA",
        authors=["Riley, Karin L", "Bendick, Rebecca", "Hyde, Kevin D"],
        license_id=LICENSE_ID,
        license_url=LICENSE_URL,
        commercial_use=True,
        license_note="CC BY 3.0：允许署名后的再分发与商业使用（须保留署名与许可链接）。",
        citation=(
            "Riley, Karin L; Bendick, Rebecca; Hyde, Kevin D (2012): Debris flow magnitudes: a global catalog. "
            f"PANGAEA. https://doi.org/{PANGAEA_DOI}. CC BY 3.0."
        ),
        landing_url=LANDING_URL,
        doi=PANGAEA_DOI,
        source_urls={"textfile": DOWNLOAD_URL},
        selection={
            "scope": "目录全部 988 条记录（无筛选）",
            "exclusions": [],
        },
        normalization={
            "read_stack": "polars（全列字符串读入 → 显式 cast）+ pandera（polars 后端）列/类型/值域校验",
            "date_rule": "Sampling date 尝试 %B %d, %Y 等格式；只有年份不留假日期（不臆造 01-01），原文在 event.date_raw",
            "geo_rule": "源目录只有地名（Area 列）没有坐标——lat/lon 留 null，不臆造",
            "category_rule": "与 NASA GLC 的泥石流**事件**分目录：本目录是规模编目，不是事件表",
        },
        known_limitations=[
            "**规模目录，不是事件表**：没有坐标、没有伤亡、没有逐条发生时间；不能当灾情事件库用。",
            "采样日期约一半为空（517/988）；`Post-fire` 有 52 条为空、9 条是"
            "『no evidence for fire (charcoal not found…)』——按原值保留，`post_fire_flag` 只对 yes/no 给布尔。",
            "面积缺失 860/988（源目录只对部分事件估算了堆积面积）。",
            "与 GLC 的泥石流事件可能有重叠，但两者口径不同（GLC 是媒体报道事件，本目录是文献规模汇编），本目录不做去重合并。",
        ],
    )


def refresh(source: Path | None = None) -> Path:
    """重抓并重写 raw / normalized / manifest；`source` 给定时离线复跑。"""
    raw = source.read_bytes() if source is not None else download(DOWNLOAD_URL)
    frame = read_catalog(raw)
    records = _records(frame)
    root = dataset_dir(DATASET_ID)
    files: dict[str, FileMeta] = {
        "raw/pangaea_783654_debris_flow_magnitude.tsv": write_bytes(raw, root / "raw" / "pangaea_783654_debris_flow_magnitude.tsv"),
        "normalized/global_debris_flow_magnitude.v1.jsonl": write_envelope(
            records, root / "normalized" / "global_debris_flow_magnitude.v1.jsonl"
        ),
    }
    manifest_path = write_manifest(spec(), files)
    print(f"规范化 {len(records)} 条泥石流规模记录")
    print(f"manifest: {manifest_path}")
    return manifest_path


def check() -> list[str]:
    from scripts.external_data.core import verify_dataset

    return verify_dataset(DATASET_ID)


if __name__ == "__main__":  # pragma: no cover
    import json

    print(json.dumps(check(), ensure_ascii=False))
