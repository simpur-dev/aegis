"""抓取并规范化外部公开灾情数据（当前：NASA Global Landslide Catalog）。

为什么要有这个脚本：`data/external/` 里的数据要能**说清来源、能复跑、能验签**，
而不是"某天从网上拖下来的一个 CSV"。脚本负责三件事：

1. 下载权威源文件（或读取 `--input` 指定的本地副本），记录 SHA-256 与行数；
2. 按**写死的选择规则**过滤（中国 / 西藏），原文列一个不改地落 `raw/`；
3. 按项目口径规范化落 `normalized/*.jsonl`（字段映射与空值语义见数据集 README）。

`--check` 只读本地：校验 manifest 里每个文件的 SHA-256、字节数与行数都对得上。
CI/门禁用 `--check`；换数据用 `--refresh`，它会重写 manifest（源文件变了要人看 diff）。

只收 public domain 或明确允许再分发的数据；NC/ND/需登录的一律不收——
判据与"未收清单"见 `data/external/README.md`。
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
import urllib.request
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

SOURCE_URL = "https://data.nasa.gov/docs/legacy/Global_Landslide_Catalog_Export/Global_Landslide_Catalog_Export_rows.csv"
DATASET_ID = "nasa-glc"
DATASET_TITLE = "NASA Global Landslide Catalog (GLC) Export"
PUBLISHER = "NASA Goddard Space Flight Center"
LICENSE_ID = "public-domain"
LICENSE_URL = "https://www.nasa.gov/nasa-brand-center/images-and-media/"
CITATION = (
    "Kirschbaum, D. B., Adler, R., Hong, Y., Hill, S., & Lerner-Lam, A. (2010). "
    "A global landslide catalog for hazard applications: method, results, and limitations. "
    "Natural Hazards, 52(3), 561-575. doi:10.1007/s11069-009-9401-4"
)
SNAPSHOT_NOTE = (
    "data.nasa.gov 的静态导出快照（事件日期止于 2016-06）；上游最新数据走 COOLR / Landslide Viewer。"
    "目录只收**降雨触发**的滑坡事件，且以媒体报道为源，不是完整普查。"
)

#: 西藏子集的**人工排除项**：源数据 admin_division 标成西藏，但地点文本明确在境外。
#: 不静默改正源数据，也不把错标当事实带进 normalized——排除项写在这里，可复现、可复核。
TIBET_EXCLUSIONS: dict[str, str] = {
    "4392": "admin_division 标 Tibet，但 location 为 Dibang, Arunachal Pradesh（印度）",
    "7108": "admin_division 标 Tibet，但 location 为 Tatopani/Araniko Highway（尼泊尔，2015 尼泊尔地震）",
}

#: GLC 的 category → 项目 HazardType（`aegis.domain.enums.HazardType`）。
#: `complex`（复合型滑坡）归 landslide：原文保留在 `hazard.source_category`，可逆。
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

RAW_FIELDS = (
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


def _repo_root() -> Path:
    """backend/scripts/x.py → 仓库根。"""
    return Path(__file__).resolve().parents[2]


def _dataset_dir() -> Path:
    return _repo_root() / "data" / "external" / DATASET_ID


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": "aegis-data-fetch/1.0 (research)"})
    with urllib.request.urlopen(request, timeout=180) as response, dest.open("wb") as out:
        out.write(response.read())


def _read_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        fields = list(reader.fieldnames or [])
        rows = list(reader)
    missing = [name for name in RAW_FIELDS if name not in fields]
    if missing:
        raise SystemExit(f"源文件缺列: {missing}；上游字段变了，先核对再改脚本")
    return fields, rows


def _num(value: str) -> float:
    text = (value or "").strip()
    if not text:
        raise SystemExit("坐标为空：规范化要求每条记录都有经纬度")
    return float(text)


def _count(value: str) -> int | None:
    """空字符串 = 源数据未报，落 null；`0` = 明确报了 0。**两者不能混**。"""
    text = (value or "").strip()
    if not text:
        return None
    return int(text)


def _event_date(row: dict[str, str]) -> date:
    raw_date = (row["event_date"] or "").strip()
    date_part = raw_date.partition(" ")[0]
    try:
        return datetime.strptime(date_part, "%m/%d/%Y").date()
    except ValueError as exc:
        raise SystemExit(f"event_id={row['event_id']} 的 event_date 无法解析: {raw_date!r}") from exc


def _normalize(row: dict[str, str]) -> dict[str, Any]:
    raw_date = (row["event_date"] or "").strip()
    _, _, time_part = raw_date.partition(" ")
    event_date = _event_date(row).isoformat()
    category = (row["landslide_category"] or "").strip()
    return {
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
            "date": event_date,
            "time_raw": time_part or None,
            "title": (row["event_title"] or "").strip() or None,
            "description": (row["event_description"] or "").strip() or None,
            "location": (row["location_description"] or "").strip() or None,
            "location_accuracy": (row["location_accuracy"] or "").strip() or None,
            "storm_name": (row["storm_name"] or "").strip() or None,
            "notes": (row["notes"] or "").strip() or None,
        },
        "geo": {
            "latitude": _num(row["latitude"]),
            "longitude": _num(row["longitude"]),
            "country": (row["country_name"] or "").strip() or None,
            "admin_division": (row["admin_division_name"] or "").strip() or None,
        },
        "hazard": {
            "type": CATEGORY_MAP.get(category, "unknown"),
            "source_category": category or None,
            "trigger": (row["landslide_trigger"] or "").strip() or None,
            "size": (row["landslide_size"] or "").strip() or None,
            "setting": (row["landslide_setting"] or "").strip() or None,
        },
        "impact": {
            "fatalities": _count(row["fatality_count"]),
            "injuries": _count(row["injury_count"]),
        },
    }


def _write_raw(rows: list[dict[str, str]], fields: list[str], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        # `lineterminator="\n"` 是必须的：csv 默认写 `\r\n`，而仓库 `.gitattributes` 是
        # `* text=auto eol=lf`——工作区留 CRLF、入库却变 LF，manifest 的 SHA 会在新克隆里失配。
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({name: row.get(name, "") for name in fields})


def _write_jsonl(records: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        for record in records:
            fh.write(json.dumps(record, ensure_ascii=False, sort_keys=False) + "\n")


def _file_meta(path: Path, *, rows: int | None = None) -> dict[str, Any]:
    meta: dict[str, Any] = {"sha256": _sha256(path), "bytes": path.stat().st_size}
    if rows is not None:
        meta["rows"] = rows
    return meta


def _write_manifest(dataset_dir: Path, *, source_path: Path, source_rows: int, files: dict[str, Any]) -> Path:
    manifest = {
        "schema_version": "1.0.0",
        "generated_at": datetime.now(UTC).isoformat(),
        "datasets": [
            {
                "id": DATASET_ID,
                "title": DATASET_TITLE,
                "publisher": PUBLISHER,
                "license": {"id": LICENSE_ID, "url": LICENSE_URL},
                "citation": CITATION,
                "source": {
                    "url": SOURCE_URL,
                    "sha256": _sha256(source_path),
                    "bytes": source_path.stat().st_size,
                    "rows": source_rows,
                    "snapshot_note": SNAPSHOT_NOTE,
                    "retrieved_at": datetime.now(UTC).isoformat(),
                },
                "selection": {
                    "china": "country_name == 'China'",
                    "tibet": "country_name == 'China' and admin_division_name == 'Tibet Autonomous Region'",
                    "tibet_exclusions": [{"event_id": key, "reason": value} for key, value in TIBET_EXCLUSIONS.items()],
                },
                "files": files,
            }
        ],
    }
    path = dataset_dir / "manifest.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    return path


def _check() -> int:
    dataset_dir = _dataset_dir()
    manifest_path = dataset_dir / "manifest.json"
    if not manifest_path.exists():
        print(f"缺 manifest: {manifest_path}", file=sys.stderr)
        return 1
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    problems: list[str] = []
    for dataset in manifest["datasets"]:
        for rel, expected in dataset["files"].items():
            path = dataset_dir / rel
            if not path.exists():
                problems.append(f"{rel}: 文件不存在")
                continue
            actual_sha = _sha256(path)
            if actual_sha != expected["sha256"]:
                problems.append(f"{rel}: sha256 不符（manifest={expected['sha256'][:12]}… actual={actual_sha[:12]}…）")
            if path.stat().st_size != expected["bytes"]:
                problems.append(f"{rel}: 字节数不符")
            if "rows" in expected:
                with path.open(encoding="utf-8") as fh:
                    lines = [line for line in fh if line.strip()]
                actual = len(lines) - 1 if path.suffix == ".csv" else len(lines)  # CSV 首行是表头
                if actual != expected["rows"]:
                    problems.append(f"{rel}: 行数不符（manifest={expected['rows']} actual={actual}）")
    if problems:
        print("数据目录校验失败：", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        print("改了数据就重跑 `--refresh` 并复核 manifest 的 diff。", file=sys.stderr)
        return 1
    print(f"OK: {manifest_path} 与数据文件逐项一致")
    return 0


def _refresh(source_path: Path) -> int:
    dataset_dir = _dataset_dir()
    fields, rows = _read_rows(source_path)
    china = [row for row in rows if (row["country_name"] or "").strip() == "China"]
    tibet_raw = [row for row in china if (row["admin_division_name"] or "").strip() == "Tibet Autonomous Region"]
    tibet_kept = [row for row in tibet_raw if row["event_id"] not in TIBET_EXCLUSIONS]

    def by_date(row: dict[str, str]) -> tuple[date, int]:
        return (_event_date(row), int(row["event_id"]))

    def normalized(selected: list[dict[str, str]]) -> list[dict[str, Any]]:
        return [_normalize(row) for row in sorted(selected, key=by_date)]

    raw_china = dataset_dir / "raw" / "glc_export_rows.china.csv"
    raw_tibet = dataset_dir / "raw" / "glc_export_rows.tibet_admin_labeled.csv"
    norm_china = dataset_dir / "normalized" / "china_landslide_events.v1.jsonl"
    norm_tibet = dataset_dir / "normalized" / "tibet_landslide_events.v1.jsonl"

    _write_raw(china, fields, raw_china)
    _write_raw(tibet_raw, fields, raw_tibet)
    _write_jsonl(normalized(china), norm_china)
    _write_jsonl(normalized(tibet_kept), norm_tibet)

    files = {
        "raw/glc_export_rows.china.csv": _file_meta(raw_china, rows=len(china)),
        "raw/glc_export_rows.tibet_admin_labeled.csv": _file_meta(raw_tibet, rows=len(tibet_raw)),
        "normalized/china_landslide_events.v1.jsonl": _file_meta(norm_china, rows=len(china)),
        "normalized/tibet_landslide_events.v1.jsonl": _file_meta(norm_tibet, rows=len(tibet_kept)),
    }
    manifest_path = _write_manifest(dataset_dir, source_path=source_path, source_rows=len(rows), files=files)
    print(f"源文件: {source_path}（{len(rows)} 行）")
    print(f"中国: {len(china)} 行；西藏（源标注）: {len(tibet_raw)} 行")
    print(f"西藏（规范化，排除 {len(tibet_raw) - len(tibet_kept)} 条）: {len(tibet_kept)} 行")
    print(f"manifest: {manifest_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="抓取/校验外部公开灾情数据（NASA GLC）")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="只校验本地文件与 manifest 一致（默认）")
    mode.add_argument("--refresh", action="store_true", help="下载（或读 --input）并重写 raw/normalized/manifest")
    parser.add_argument("--input", type=Path, help="--refresh 时读取本地源文件，不联网")
    args = parser.parse_args(argv)

    if args.refresh:
        if args.input is not None:
            return _refresh(args.input)
        with _temp_download() as source:
            return _refresh(source)
    return _check()


class _temp_download:
    """把源文件下到系统临时目录，避免把 8 MB 全量快照落进仓库。"""

    def __enter__(self) -> Path:
        import tempfile

        self._dir = tempfile.TemporaryDirectory(prefix="aegis-glc-")
        path = Path(self._dir.name) / "glc_export_rows.csv"
        print(f"下载 {SOURCE_URL} …")
        _download(SOURCE_URL, path)
        return path

    def __exit__(self, *exc: object) -> None:
        self._dir.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
