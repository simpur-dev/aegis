"""外部公开数据目录的门禁：完整性、形状不漂移、许可可见。

`data/external/` 里的数据是**随仓库发布**的，不是某台机器上的临时文件——所以要有三道闸：

1. **完整性**：manifest 登记的 SHA-256 / 字节数 / 行数与盘上文件逐项一致；
2. **形状**：每条记录过 schema 契约，记录号唯一、按日期有序；
3. **许可**：manifest 与每条记录的许可都在"允许再分发"的白名单里；**非商业许可必须
   显式标 `commercial_use: false` 且许可文件在位**——这类数据进仓库是例外，不是默认。

改数据必须走 `uv run python -m scripts.external_data --refresh`；
手改 CSV/JSONL 会在这里红。
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import zipfile
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from scripts.external_data.cli import MODULES

from aegis.domain.enums import HazardType

DATA_ROOT = Path(__file__).resolve().parents[3] / "data" / "external"

#: 允许随仓库再分发的许可；`nc-*` 是**例外**，必须同时满足 NC 隔离四条。
REDISTRIBUTABLE_LICENSES = {"public-domain", "cc0-1.0", "cc-by-3.0", "cc-by-4.0"}
NON_COMMERCIAL_LICENSES = {"cc-by-nc-4.0", "cc-by-nc-sa-4.0"}

#: 各数据集的实测条数（源数据变动时必须在这里显式改，防止"悄悄换了数据"）。
EXPECTED_ROWS: dict[str, int] = {
    "nasa-glc": 426,
    "pangaea-debris-flow-magnitude": 988,
    "sciencedb-tibet-road-geohazard-2022": 13_768,
    "tpdc-himalaya-geohazard-2006-2009": 525,
    "zenodo-himalaya-landslide-inventories": 831,
    "zenodo-parlung-zangbo-glacial-lakes": 3_224,
}


def _dataset_dir(dataset_id: str) -> Path:
    return DATA_ROOT / dataset_id


def _manifest(dataset_id: str) -> dict[str, Any]:
    return json.loads((_dataset_dir(dataset_id) / "manifest.json").read_text(encoding="utf-8"))["datasets"][0]


def _rows(dataset_id: str, rel: str) -> list[dict[str, str]]:
    path = _dataset_dir(dataset_id) / rel
    if path.suffix == ".csv":
        with path.open(encoding="utf-8", newline="") as fh:
            return list(csv.DictReader(fh))
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ============================================================
# 通用闸（对所有数据集）
# ============================================================


@pytest.mark.parametrize("dataset_id", sorted(MODULES))
def test_manifest校验和与文件逐项一致(dataset_id: str) -> None:
    root = _dataset_dir(dataset_id)
    for rel, expected in _manifest(dataset_id)["files"].items():
        path = root / rel
        assert path.exists(), f"{dataset_id}/{rel} 不在盘上——新克隆缺数据文件"
        data = path.read_bytes()
        assert _sha256(data) == expected["sha256"], f"{dataset_id}/{rel} 被手改过（sha256 不符）"
        assert len(data) == expected["bytes"], f"{dataset_id}/{rel} 字节数不符"
        if "rows" in expected:
            lines = [line for line in data.splitlines() if line.strip()]
            actual = len(lines) - 1 if path.suffix == ".csv" else len(lines)
            assert actual == expected["rows"], f"{dataset_id}/{rel} 行数不符（manifest={expected['rows']} actual={actual}）"


@pytest.mark.parametrize("dataset_id", sorted(MODULES))
def test_许可可见且非商业许可已隔离(dataset_id: str) -> None:
    dataset = _manifest(dataset_id)
    license_info = dataset["license"]
    license_id = license_info["id"]
    if license_id in NON_COMMERCIAL_LICENSES:
        assert license_info["commercial_use"] is False, f"{dataset_id} 是 {license_id}，必须显式标 commercial_use=false"
        assert license_info.get("note"), f"{dataset_id} 必须写明非商业限制与商用前移除要求"
        assert any((_dataset_dir(dataset_id) / name).exists() for name in ("LICENSE-CC-BY-NC-4.0.md", "LICENSE-CC-BY-NC-SA-4.0.md")), (
            f"{dataset_id} 缺少独立许可文件"
        )
    else:
        assert license_id in REDISTRIBUTABLE_LICENSES, f"{dataset_id} 的许可 {license_id} 不在白名单里"


@pytest.mark.parametrize("dataset_id", sorted(MODULES))
def test_灾种取值都在项目枚举内(dataset_id: str) -> None:
    allowed = {hazard.value for hazard in HazardType}
    for rel in _manifest(dataset_id)["files"]:
        if not rel.startswith("normalized/"):
            continue
        for row in _rows(dataset_id, rel):
            value = row["hazard"]["type"]
            assert value in allowed, f"{dataset_id}/{rel} 的灾种 {value!r} 不在项目 HazardType 里"


@pytest.mark.parametrize("dataset_id", sorted(MODULES))
def test_记录号唯一(dataset_id: str) -> None:
    for rel in _manifest(dataset_id)["files"]:
        if not rel.startswith("normalized/"):
            continue
        ids = [row.get("record_id") for row in _rows(dataset_id, rel)]
        assert ids, f"{dataset_id}/{rel} 没有记录"
        assert all(ids), f"{dataset_id}/{rel} 有记录缺 record_id"
        assert len(ids) == len(set(ids)), f"{dataset_id}/{rel} 有重复 record_id"


ENVELOPE_KEYS = {"schema_version", "record_id", "source", "event", "geo", "hazard", "impact"}


@pytest.mark.parametrize("dataset_id", sorted(MODULES))
def test_统一信封(dataset_id: str) -> None:
    """整个目录只有一种 normalized 形状：七个固定信封字段。

    数据源可以有五种，形状不能有两种——不然每个下游都得写两个读取器。
    """
    for rel in _manifest(dataset_id)["files"]:
        if not rel.startswith("normalized/"):
            continue
        assert rel.endswith(".jsonl"), f"{dataset_id}/{rel} 必须是 JSONL（统一信封）"
        for row in _rows(dataset_id, rel):
            assert set(row) == ENVELOPE_KEYS, f"{dataset_id}/{rel} 的信封字段不符: {sorted(set(row) ^ ENVELOPE_KEYS)}"
            assert row["source"]["license"], f"{dataset_id}/{rel} 的记录缺许可"
            assert row["source"]["license"] == _manifest(dataset_id)["license"]["id"]


# ============================================================
# NASA GLC
# ============================================================


def test_nasa_glc_西藏子集是中国子集且排除项生效() -> None:
    china_ids = {row["record_id"] for row in _rows("nasa-glc", "normalized/china_landslide_events.v1.jsonl")}
    tibet = _rows("nasa-glc", "normalized/tibet_landslide_events.v1.jsonl")
    assert {row["record_id"] for row in tibet} <= china_ids
    excluded = {item["event_id"] for item in _manifest("nasa-glc")["selection"]["tibet_exclusions"]}
    assert excluded, "排除项不应为空——空列表会让这条闸空转"
    assert not ({row["source"]["event_id"] for row in tibet} & excluded), "被排除的错标记录混进了西藏子集"
    raw = _dataset_dir("nasa-glc") / "raw" / "glc_export_rows.tibet_admin_labeled.csv"
    with raw.open(encoding="utf-8", newline="") as fh:
        raw_ids = {row["event_id"] for row in csv.DictReader(fh)}
    assert excluded <= raw_ids, "排除项必须仍存在于 raw 投影里——否则排除理由已失效"


def test_nasa_glc_条数与类型分布() -> None:
    china = _rows("nasa-glc", "normalized/china_landslide_events.v1.jsonl")
    assert len(china) == EXPECTED_ROWS["nasa-glc"]
    tibet = _rows("nasa-glc", "normalized/tibet_landslide_events.v1.jsonl")
    assert len(tibet) == 16
    assert Counter(row["hazard"]["type"] for row in china)["landslide"] > 0
    assert all(row["geo"]["longitude"] is not None and row["geo"]["latitude"] is not None for row in china), "NASA 子集每条都有坐标"
    # 空值语义：未报 ≠ 0
    assert any(row["impact"]["fatalities"] is None for row in china), "未报的死亡数应是 null 而不是 0"


# ============================================================
# PANGAEA 全球泥石流规模目录（CC BY 3.0）
# ============================================================


def test_pangaea_规模目录条数与空值语义() -> None:
    rows = _rows("pangaea-debris-flow-magnitude", "normalized/global_debris_flow_magnitude.v1.jsonl")
    assert len(rows) == EXPECTED_ROWS["pangaea-debris-flow-magnitude"]
    # 规模目录没有坐标：全部 null（不臆造）
    assert all(row["geo"]["latitude"] is None and row["geo"]["longitude"] is None for row in rows)
    # 体积全部有值；面积大量缺失（源目录只对部分事件估算）
    attributes = [row["hazard"]["attributes"] for row in rows]
    assert all(item["volume_m3"] is not None for item in attributes)
    assert sum(1 for item in attributes if item["area_m2"] is None) == 860
    # Post-fire 三段值 + 空：264 yes / 663 no / 9 "no evidence…" / 52 空
    post = Counter(item["post_fire"] for item in attributes)
    assert post["yes"] == 264 and post["no"] == 663 and post[None] == 52
    assert sum(1 for item in attributes if (item["post_fire"] or "").startswith("no evidence")) == 9
    # 与 GLC 泥石流事件分表：source_category 固定，不混进事件计数
    assert {row["hazard"]["source_category"] for row in rows} == {"debris_flow_magnitude_catalog"}
    # 主键 Ord No 1..988 唯一
    assert sorted(int(row["source"]["event_id"]) for row in rows) == list(range(1, 989))


# ============================================================
# 公路地灾普查（CC BY-NC 4.0）
# ============================================================


def test_普查_条数分类与隐患点占比() -> None:
    rows = _rows("sciencedb-tibet-road-geohazard-2022", "normalized/tibet_road_geohazard_2022.v1.jsonl")
    assert len(rows) == EXPECTED_ROWS["sciencedb-tibet-road-geohazard-2022"]
    assert Counter(row["hazard"]["source_category"] for row in rows) == Counter(
        {"崩塌": 6_692, "泥石流": 2_909, "滑坡": 1_903, "沉陷塌陷": 1_227, "水毁": 1_037}
    )
    occurred = Counter(row["hazard"]["attributes"]["occurred"] for row in rows)
    assert occurred[False] == 2_070, "隐患点（是否发生灾害=否）必须能数出来"
    assert all(row["geo"]["longitude"] is not None and row["geo"]["latitude"] is not None for row in rows)


def test_普查_raw仍是GB18030且行数对得上() -> None:
    dataset = _manifest("sciencedb-tibet-road-geohazard-2022")
    for rel, expected in dataset["files"].items():
        if not rel.startswith("raw/"):
            continue
        raw = (_dataset_dir("sciencedb-tibet-road-geohazard-2022") / rel).read_bytes()
        text = raw.decode("gb18030")  # 解不开 = 编码被改坏了
        rows = list(csv.DictReader(io.StringIO(text)))
        assert len(rows) == expected["rows"], f"{rel} 按 GB18030 读出的行数不符"
        assert "灾害编号" in rows[0], f"{rel} 缺关键列，源结构变了"


# ============================================================
# TPDC 喜马拉雅（CC BY-NC-SA 4.0）
# ============================================================


def test_tpdc_三类灾害点数与属性齐全() -> None:
    rows = _rows("tpdc-himalaya-geohazard-2006-2009", "normalized/himalaya_geohazard_sites.v1.jsonl")
    assert len(rows) == EXPECTED_ROWS["tpdc-himalaya-geohazard-2006-2009"]
    assert Counter(row["hazard"]["type"] for row in rows) == Counter({"debris_flow": 353, "landslide": 153, "lake_outburst": 19})
    landslide = next(row for row in rows if row["hazard"]["type"] == "landslide")
    assert landslide["hazard"]["attributes"]["slope_angle_deg"] is not None
    assert landslide["hazard"]["attributes"]["stratum"] is not None
    debris = next(row for row in rows if row["hazard"]["type"] == "debris_flow")
    assert debris["hazard"]["attributes"]["channel_gradient"] is not None
    lake = next(row for row in rows if row["hazard"]["type"] == "lake_outburst")
    assert lake["hazard"]["attributes"]["outburst_risk"] is not None


def test_tpdc_坐标越界点被标出来而不是被改掉() -> None:
    """源数据有 2 个纬度明显异常（首位数字笔误，同一数据集的表格里也是这个值）。

    我们的口径是**不静默改源数据**，但必须**看得见**：越界点带 `quality_flags`。
    """
    rows = _rows("tpdc-himalaya-geohazard-2006-2009", "normalized/himalaya_geohazard_sites.v1.jsonl")
    flagged = [row for row in rows if row["geo"].get("quality_flags")]
    assert len(flagged) == 2, "源数据的两个越界点必须被标记"
    for row in flagged:
        assert "latitude_out_of_range" in row["geo"]["quality_flags"]
    inside = [row for row in rows if not row["geo"].get("quality_flags")]
    assert all(27 < row["geo"]["latitude"] < 37 for row in inside)
    assert all(78 < row["geo"]["longitude"] < 100 for row in inside)


def test_tpdc_raw是原始ZIP且可解() -> None:
    raw = (_dataset_dir("tpdc-himalaya-geohazard-2006-2009") / "raw" / "tpdc_himalaya_geohazard.zip").read_bytes()
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        names = archive.namelist()
    assert any(name.endswith(".shp") for name in names), "raw 里应有 shapefile"
    assert any(name.endswith(".xlsx") for name in names), "raw 里应有表格数据"


# ============================================================
# Zenodo 喜马拉雅（CC BY 4.0）
# ============================================================


def test_zenodo_两个集合条数与坐标口径() -> None:
    eastern = _rows("zenodo-himalaya-landslide-inventories", "normalized/eastern_himalaya_large_landslides.v1.jsonl")
    megas = _rows("zenodo-himalaya-landslide-inventories", "normalized/megas_largest_landslides.v1.jsonl")
    assert len(eastern) == 420
    assert len(megas) == 411
    assert all(row["geo"]["longitude"] is not None for row in eastern), "东喜马拉雅清单每条都有坐标"
    assert all(90 < row["geo"]["longitude"] < 98 for row in eastern)
    assert all(27 < row["geo"]["latitude"] < 31 for row in eastern)
    # MEGAS 源数据只有地名没有坐标——坐标列留空，不臆造
    assert all(row["geo"]["longitude"] is None and row["geo"]["latitude"] is None for row in megas)
    assert any(row["geo"]["country"] == "China" for row in megas)
    # 源 CSV 有 14 行缺 ID；按行序兜底后不能把它们丢掉
    assert sum(1 for row in megas if row["source"]["event_id"] is None) == 14


# ============================================================
# Zenodo 帕隆藏布冰湖（CC BY 4.0）
# ============================================================


def test_冰湖两期条数与坐标在藏东南() -> None:
    rows = _rows("zenodo-parlung-zangbo-glacial-lakes", "normalized/parlung_zangbo_glacial_lakes.v1.jsonl")
    assert len(rows) == EXPECTED_ROWS["zenodo-parlung-zangbo-glacial-lakes"]
    years = Counter(row["source"]["inventory_year"] for row in rows)
    assert years[2020] == 1_618 and years[2025] == 1_606
    assert all(92 < row["geo"]["longitude"] < 98 for row in rows), "冰湖坐标应落在藏东南"
    assert all(28 < row["geo"]["latitude"] < 32 for row in rows)
    assert all(row["hazard"]["type"] == "lake_outburst" for row in rows)


# ============================================================
# 目录规范
# ============================================================


def test_每个数据集都有README与manifest() -> None:
    for dataset_id in MODULES:
        root = _dataset_dir(dataset_id)
        assert (root / "README.md").exists(), f"{dataset_id} 缺 README"
        assert (root / "manifest.json").exists(), f"{dataset_id} 缺 manifest"


def test_数据集索引与实际目录一致() -> None:
    """`scripts/external_data/cli.py` 的模块表是数据集索引的唯一真源——目录多一个就红。"""
    on_disk = {path.name for path in DATA_ROOT.iterdir() if path.is_dir()}
    assert on_disk == set(MODULES), f"目录与索引不一致：目录有 {on_disk - set(MODULES)}，索引有 {set(MODULES) - on_disk}"


def test_jsonl记录过schema契约() -> None:
    """有 schema.json 的数据集按 schema 校验；没有的走字段级断言（上面各条）。"""
    for dataset_id in MODULES:
        schema_path = _dataset_dir(dataset_id) / "schema.json"
        if not schema_path.exists():
            continue
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        if "columns" in schema:  # CSV 列契约
            continue
        validator = Draft202012Validator(schema)
        for rel in _manifest(dataset_id)["files"]:
            if not rel.startswith("normalized/"):
                continue
            for record in _rows(dataset_id, rel):
                errors = sorted(validator.iter_errors(record), key=lambda e: list(e.path))
                assert errors == [], f"{dataset_id}/{rel} 的 {record.get('record_id')} 不合 schema"
                if record.get("event", {}).get("date"):
                    date.fromisoformat(record["event"]["date"])
