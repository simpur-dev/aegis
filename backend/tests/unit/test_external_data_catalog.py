"""外部公开灾情数据目录的门禁：来源可核验、形状不漂移、许可可再分发。

`data/external/` 里的数据是**随仓库发布**的，不是某台机器上的临时文件——所以要有三道闸：

1. **完整性**：manifest 登记的 SHA-256 / 字节数 / 行数与盘上文件逐项一致；
2. **形状**：每条记录通过 `schema.json`，记录号唯一、按日期有序，西藏是中国子集；
3. **许可**：manifest 与每条记录的 license 都在"允许再分发"的白名单里，NC/ND 混不进来。

改数据必须走 `uv run python -m scripts.fetch_external_disaster_data --refresh`；
手改 CSV/JSONL 会在这里红。
"""

from __future__ import annotations

import csv
import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from aegis.domain.enums import HazardType

DATA_ROOT = Path(__file__).resolve().parents[3] / "data" / "external"
DATASET_DIR = DATA_ROOT / "nasa-glc"

#: 允许随仓库再分发的许可白名单；NC/ND/需授权的一律不收（判据见 data/external/README.md）。
REDISTRIBUTABLE_LICENSES = {"public-domain", "cc0-1.0", "cc-by-4.0"}


def _manifest() -> dict[str, Any]:
    return json.loads((DATASET_DIR / "manifest.json").read_text(encoding="utf-8"))


def _dataset() -> dict[str, Any]:
    return _manifest()["datasets"][0]


def _records(rel: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in (DATASET_DIR / rel).read_text(encoding="utf-8").splitlines() if line.strip()]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_manifest校验和与文件逐项一致() -> None:
    for rel, expected in _dataset()["files"].items():
        path = DATASET_DIR / rel
        assert path.exists(), f"{rel} 不在盘上——新克隆缺数据文件"
        assert _sha256(path) == expected["sha256"], f"{rel} 被手改过（sha256 不符）"
        assert path.stat().st_size == expected["bytes"], f"{rel} 字节数不符"
        if "rows" in expected:
            lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
            actual = len(lines) - 1 if path.suffix == ".csv" else len(lines)  # CSV 首行是表头
            assert actual == expected["rows"], f"{rel} 行数不符（manifest={expected['rows']} actual={actual}）"


def test_每条记录通过schema且日期可解析() -> None:
    schema = json.loads((DATASET_DIR / "schema.json").read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)
    for rel in ("normalized/china_landslide_events.v1.jsonl", "normalized/tibet_landslide_events.v1.jsonl"):
        for record in _records(rel):
            errors = sorted(validator.iter_errors(record), key=lambda e: list(e.path))
            assert errors == [], f"{rel} 的 {record.get('record_id')} 不合 schema: {[e.message for e in errors]}"
            date.fromisoformat(record["event"]["date"])


def test_记录号唯一且按日期有序() -> None:
    for rel in ("normalized/china_landslide_events.v1.jsonl", "normalized/tibet_landslide_events.v1.jsonl"):
        records = _records(rel)
        ids = [record["record_id"] for record in records]
        assert len(ids) == len(set(ids)), f"{rel} 有重复 record_id"
        keys = [(record["event"]["date"], int(record["source"]["event_id"])) for record in records]
        assert keys == sorted(keys), f"{rel} 未按 (日期, 事件号) 排序"


def test_西藏是中国子集且排除项生效() -> None:
    china_ids = {record["record_id"] for record in _records("normalized/china_landslide_events.v1.jsonl")}
    tibet = _records("normalized/tibet_landslide_events.v1.jsonl")
    assert {record["record_id"] for record in tibet} <= china_ids, "西藏记录必须也出现在中国子集里"
    excluded = {item["event_id"] for item in _dataset()["selection"]["tibet_exclusions"]}
    assert excluded, "排除项不应为空——空列表会让这条闸空转"
    assert not ({record["source"]["event_id"] for record in tibet} & excluded), "被排除的错标记录混进了西藏子集"

    raw_tibet = DATASET_DIR / "raw" / "glc_export_rows.tibet_admin_labeled.csv"
    with raw_tibet.open(encoding="utf-8", newline="") as fh:
        raw_ids = {row["event_id"] for row in csv.DictReader(fh)}
    assert excluded <= raw_ids, "排除项必须仍存在于 raw 投影里——否则排除理由已失效，该重新核对"


def test_许可允许再分发且记录许可与manifest一致() -> None:
    dataset = _dataset()
    assert dataset["license"]["id"] in REDISTRIBUTABLE_LICENSES
    for rel in ("normalized/china_landslide_events.v1.jsonl", "normalized/tibet_landslide_events.v1.jsonl"):
        for record in _records(rel):
            assert record["source"]["license"] == dataset["license"]["id"]


def test_schema里的灾种枚举与项目HazardType一致() -> None:
    schema = json.loads((DATASET_DIR / "schema.json").read_text(encoding="utf-8"))
    enum = set(schema["properties"]["hazard"]["properties"]["type"]["enum"])
    assert enum == {hazard.value for hazard in HazardType}, "外部数据的灾种枚举与项目 HazardType 漂移了"
