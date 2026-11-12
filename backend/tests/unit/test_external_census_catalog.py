"""西藏公路地质灾害普查数据（CC BY-NC 4.0）的门禁：可核验、形状不漂移、商用限制可见。

这份数据与 NASA GLC 不同：它是 **CC BY-NC 4.0（非商业）**，必须隔离放置并明确标注。
这条门禁除了完整性/形状，还钉住三件事：

1. manifest 的 `license.commercial_use` 必须是 `false`，且许可说明非空；
2. `LICENSE-CC-BY-NC-4.0.md` 必须在位且写明许可；
3. `raw/` 是原始字节（GB18030 + CRLF），不许被文本编辑器"顺手修正"——SHA-256 一改就红。
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
from collections import Counter
from pathlib import Path
from typing import Any

from aegis.domain.enums import HazardType

DATA_ROOT = Path(__file__).resolve().parents[3] / "data" / "external"
DATASET_DIR = DATA_ROOT / "sciencedb-tibet-road-geohazard-2022"
NORMALIZED = DATASET_DIR / "normalized" / "tibet_road_geohazard_2022.v1.csv"

#: 本副本的实测条数（源数据变动时必须在这里显式改，防止"悄悄换了数据"）。
EXPECTED_ROWS = 13_768
EXPECTED_BY_CATEGORY = {"崩塌": 6_692, "泥石流": 2_909, "滑坡": 1_903, "沉陷塌陷": 1_227, "水毁": 1_037}
EXPECTED_OCCURRED_FALSE = 2_070


def _manifest() -> dict[str, Any]:
    return json.loads((DATASET_DIR / "manifest.json").read_text(encoding="utf-8"))["datasets"][0]


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _schema() -> dict[str, Any]:
    return json.loads((DATASET_DIR / "schema.json").read_text(encoding="utf-8"))


def _rows() -> list[dict[str, str]]:
    with NORMALIZED.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def test_manifest校验和与文件逐项一致() -> None:
    dataset = _manifest()
    for rel, expected in dataset["files"].items():
        path = DATASET_DIR / rel
        assert path.exists(), f"{rel} 不在盘上——新克隆缺数据文件"
        data = path.read_bytes()
        assert _sha256(data) == expected["sha256"], f"{rel} 被改过（sha256 不符）"
        assert len(data) == expected["bytes"], f"{rel} 字节数不符"
        if "rows" in expected:
            lines = [line for line in data.splitlines() if line.strip()]
            actual = len(lines) - 1 if path.suffix == ".csv" else len(lines)
            assert actual == expected["rows"], f"{rel} 行数不符（manifest={expected['rows']} actual={actual}）"


def test_raw仍是GB18030且行数与manifest一致() -> None:
    """raw 是证据：能按 GB18030 解出来、能被 csv 读、行数对得上。"""
    dataset = _manifest()
    for rel, expected in dataset["files"].items():
        if not rel.startswith("raw/"):
            continue
        raw = (DATASET_DIR / rel).read_bytes()
        text = raw.decode("gb18030")  # 解不开 = 编码被改坏了
        rows = list(csv.DictReader(io.StringIO(text)))
        assert len(rows) == expected["rows"], f"{rel} 按 GB18030 读出的行数不符"
        assert "灾害编号" in rows[0], f"{rel} 缺关键列，源结构变了"


def test_规范化列与schema一致且主键唯一() -> None:
    schema = _schema()
    expected = [column["name"] for column in schema["columns"]]
    rows = _rows()
    assert list(rows[0]) == expected, "normalized CSV 的列与 schema.json 漂移了"
    assert len(rows) == EXPECTED_ROWS
    ids = [row["record_id"] for row in rows]
    assert len(ids) == len(set(ids)), "record_id 不唯一"
    assert all(row["record_id"].startswith("sciencedb-tibet-road-geohazard-2022:") for row in rows)


def test_灾种映射与项目枚举一致且条数对得上() -> None:
    rows = _rows()
    assert Counter(row["source_category"] for row in rows) == Counter(EXPECTED_BY_CATEGORY)
    hazard_values = {hazard.value for hazard in HazardType}
    for row in rows:
        assert row["hazard_type"] in hazard_values, f"{row['record_id']} 的 hazard_type 不在项目枚举里"
        assert row["source_category"] in EXPECTED_BY_CATEGORY, f"{row['record_id']} 的 source_category 未知"
    assert Counter(row["occurred"] for row in rows)["false"] == EXPECTED_OCCURRED_FALSE


def test_日期与坐标可解析且落在西藏范围() -> None:
    schema = _schema()
    limits = {column["name"]: column for column in schema["columns"]}
    for row in _rows():
        if row["last_event_date"]:
            year, month, day = (int(part) for part in row["last_event_date"].split("-"))
            assert 2000 <= year <= 2030 and 1 <= month <= 12 and 1 <= day <= 31, row["record_id"]
        lon = float(row["longitude"])
        lat = float(row["latitude"])
        assert limits["longitude"]["minimum"] <= lon <= limits["longitude"]["maximum"], row["record_id"]
        assert limits["latitude"]["minimum"] <= lat <= limits["latitude"]["maximum"], row["record_id"]
        assert int(row["geometry_points"]) >= 1
        assert not any("\ufffd" in value for value in row.values()), f"{row['record_id']} 有替换字符（编码损坏）"


def test_许可为非商业且注明商用限制() -> None:
    dataset = _manifest()
    license_info = dataset["license"]
    assert license_info["id"] == "cc-by-nc-4.0"
    assert license_info["commercial_use"] is False, "CC BY-NC 数据必须显式标 commercial_use=false"
    assert license_info["note"], "必须写明非商业限制与商用前移除要求"
    license_file = DATASET_DIR / "LICENSE-CC-BY-NC-4.0.md"
    assert license_file.exists()
    text = license_file.read_text(encoding="utf-8")
    assert "CC BY-NC 4.0" in text and "商业" in text
