"""抓取并规范化 Science Data Bank《2022 年度西藏地质灾害普查数据》（CC BY-NC 4.0）。

**许可提醒**：这份数据是 **CC BY-NC 4.0（非商业）**，与仓库的 AGPL-3.0 不是同一套许可。
它被隔离在 `data/external/sciencedb-tibet-road-geohazard-2022/`，manifest 标
`commercial_use: false`；**商用发行前必须整目录移除或另行取得授权**。详见该目录 README。

数据内容：西藏公路沿线地质灾害普查（崩塌 / 水毁 / 沉陷塌陷 / 泥石流 / 滑坡 五个 CSV），
字段是道路与隐患点口径（线路、桩号、断通状况、灾害等级等），**不是**完整灾情事件库——
其中 2,070 条 `是否发生灾害=否` 是隐患点，不是已发生事件。

规范化的口径：
- 原始 CSV 是 GB18030 + CRLF，`raw/` **按字节原样保留**（manifest 记录 SHA-256）；
- `normalized/` 转 UTF-8 + LF，只挑跨类型通用的列，`hazard.type` 映射到项目 HazardType；
- 日期统一 ISO（解析不了保留原文列），坐标取 `经纬度CGCS2K` 的**第一个点**（原串是折线）。

用法（在 backend/ 下）：
    uv run python -m scripts.fetch_tibet_road_geohazard --check
    uv run python -m scripts.fetch_tibet_road_geohazard --refresh
    uv run python -m scripts.fetch_tibet_road_geohazard --refresh --source-dir D:/downloads/census
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import sys
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DATASET_ID = "sciencedb-tibet-road-geohazard-2022"
DATASET_TITLE = "2022 年度西藏地质灾害普查数据（公路沿线）"
DOI = "10.57760/sciencedb.27813"
CSTR = "31253.11.sciencedb.27813"
LANDING_URL = f"https://www.scidb.cn/doi/{DOI}"
DATA_SET_ID = "9b7541457595412c91304d3193198cc5"
FILETREE_URL = f"https://www.scidb.cn/api/gin-sdb-filetree/getAllUrl?dataSetId={DATA_SET_ID}&type=personal&version=V1&global=Asia/Shanghai"
ATTRIBUTION = "田建（Tian Jian），中国交通科学研究院（China Academy of Transportation Sciences）"
LICENSE_ID = "cc-by-nc-4.0"
LICENSE_URL = "https://creativecommons.org/licenses/by-nc/4.0/"
LICENSE_NOTE = (
    "CC BY-NC 4.0：允许署名、非商业性再分发；禁止商业性使用。"
    "本目录与 AGPL-3.0 主仓库隔离，manifest 标 commercial_use=false；"
    "商用发行前必须移除本目录或另行取得授权。"
)

#: 源文件名 → 项目 HazardType。沉陷塌陷/水毁在项目枚举里没有对应类，落 unknown（原文保留）。
CATEGORY_MAP: dict[str, str] = {
    "崩塌": "rockfall",
    "滑坡": "landslide",
    "泥石流": "debris_flow",
    "沉陷塌陷": "unknown",
    "水毁": "unknown",
}

#: 归一化 CSV 的列（也是 schema.json 的 columns）。
NORMALIZED_COLUMNS: tuple[str, ...] = (
    "record_id",
    "source_category",
    "hazard_type",
    "disaster_code",
    "name",
    "road_class",
    "road_name",
    "road_code",
    "admin_code",
    "maintainer",
    "occurred",
    "last_event_date",
    "last_event_date_raw",
    "blocked_status",
    "severity",
    "damage_level",
    "development_degree",
    "tech_level",
    "occurrence_window_years",
    "occurrence_count",
    "treatment",
    "check_status",
    "start_stake",
    "end_stake",
    "longitude",
    "latitude",
    "geometry_points",
)

_DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S")


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _dataset_dir() -> Path:
    return _repo_root() / "data" / "external" / DATASET_ID


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _download(url: str, timeout: int = 240) -> bytes:
    # 源 URL 的 path 里有空格与中文，fileName 已百分号编码；safe 里保留 % 避免二次编码。
    safe = urllib.parse.quote(url, safe=":/?&=%")
    request = urllib.request.Request(safe, headers={"User-Agent": "aegis-data-fetch/1.0 (research)"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def _source_files(source_dir: Path | None) -> list[tuple[str, bytes]]:
    """返回 [(文件名, 原始字节)]。给了 --source-dir 就读本地，否则走 ScienceDB 下载。"""
    if source_dir is not None:
        files = sorted(source_dir.glob("*.csv"))
        if len(files) != 5:
            raise SystemExit(f"--source-dir 里应有 5 个 CSV，实际 {len(files)} 个: {[p.name for p in files]}")
        return [(path.name, path.read_bytes()) for path in files]

    listing = _download(FILETREE_URL, timeout=60).decode("utf-8")
    urls = [line.strip() for line in listing.splitlines() if line.strip()]
    if len(urls) != 5:
        raise SystemExit(f"ScienceDB 文件树应返回 5 个文件，实际 {len(urls)} 个；上游结构变了，先核对")
    out: list[tuple[str, bytes]] = []
    for url in urls:
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        name = query.get("fileName", [""])[0]
        if not name:
            raise SystemExit(f"文件 URL 缺 fileName: {url[:120]}")
        print(f"下载 {name} …")
        out.append((name, _download(url)))
    return out


def _read_rows(name: str, raw: bytes) -> list[dict[str, str]]:
    try:
        text = raw.decode("gb18030")
    except UnicodeDecodeError as exc:
        raise SystemExit(f"{name} 不是 GB18030 文本: {exc}") from exc
    return list(csv.DictReader(io.StringIO(text)))


def _clean(value: str | None) -> str:
    return (value or "").strip()


def _parse_date(raw: str) -> str:
    text = raw.strip()
    if not text:
        return ""
    for fmt in _DATE_FORMATS:
        try:
            parsed = datetime.strptime(text, fmt).date()
        except ValueError:
            continue
        return parsed.isoformat()
    return ""


def _parse_coord(raw: str) -> tuple[float, float, int]:
    """`90.95,29.97;90.95,29.97;` → 第一个点 + 点数。"""
    points = [part for part in raw.split(";") if part.strip()]
    if not points:
        raise SystemExit("经纬度为空")
    lon_text, lat_text = points[0].split(",")[:2]
    return float(lon_text), float(lat_text), len(points)


def _normalize(name: str, rows: list[dict[str, str]]) -> list[dict[str, str]]:
    category = name.removesuffix(".csv")
    hazard_type = CATEGORY_MAP.get(category)
    if hazard_type is None:
        raise SystemExit(f"未知灾害类型文件: {name}")
    out: list[dict[str, str]] = []
    for row in rows:
        code = _clean(row.get("灾害编号"))
        if not code:
            raise SystemExit(f"{name} 有记录缺灾害编号")
        lon, lat, points = _parse_coord(_clean(row.get("经纬度CGCS2K")))
        raw_date = _clean(row.get("最近发生时间"))
        window = "10" if "近十年发生次数" in row else "5"
        treatment = _clean(row.get("灾害处治情况")) or _clean(row.get("灾害修复情况"))
        out.append(
            {
                "record_id": f"{DATASET_ID}:{code}",
                "source_category": category,
                "hazard_type": hazard_type,
                "disaster_code": code,
                "name": _clean(row.get("灾害名称")),
                "road_class": _clean(row.get("道路类别")),
                "road_name": _clean(row.get("线路名称")),
                "road_code": _clean(row.get("线路编号")),
                "admin_code": _clean(row.get("区划代码")),
                "maintainer": _clean(row.get("管养单位")),
                "occurred": {"是": "true", "否": "false"}.get(_clean(row.get("是否发生灾害")), ""),
                "last_event_date": _parse_date(raw_date),
                "last_event_date_raw": raw_date,
                "blocked_status": _clean(row.get("断通状况")),
                "severity": _clean(row.get("危害程度")),
                "damage_level": _clean(row.get("灾害等级")),
                "development_degree": _clean(row.get("灾害发育程度")),
                "tech_level": _clean(row.get("技术等级")),
                "occurrence_window_years": window,
                "occurrence_count": _clean(row.get(f"近{'十' if window == '10' else '五'}年发生次数")),
                "treatment": treatment,
                "check_status": _clean(row.get("核查状态")),
                "start_stake": _clean(row.get("起点桩号")),
                "end_stake": _clean(row.get("止点桩号")),
                "longitude": f"{lon:.6f}",
                "latitude": f"{lat:.6f}",
                "geometry_points": str(points),
            }
        )
    return out


def _write_csv(rows: list[dict[str, str]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=NORMALIZED_COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _write_manifest(dataset_dir: Path, raw_meta: dict[str, Any], normalized: Path, rows: int) -> Path:
    manifest = {
        "schema_version": "1.0.0",
        "generated_at": datetime.now(UTC).isoformat(),
        "datasets": [
            {
                "id": DATASET_ID,
                "title": DATASET_TITLE,
                "publisher": "Science Data Bank",
                "authors": [ATTRIBUTION],
                "doi": DOI,
                "cstr": CSTR,
                "license": {
                    "id": LICENSE_ID,
                    "url": LICENSE_URL,
                    "commercial_use": False,
                    "note": LICENSE_NOTE,
                },
                "citation": (f"{ATTRIBUTION}. {DATASET_TITLE}[DS/OL]. V1. Science Data Bank, 2025. https://doi.org/{DOI}. CSTR:{CSTR}."),
                "source": {
                    "landing_url": LANDING_URL,
                    "filetree_api": FILETREE_URL,
                    "retrieved_at": datetime.now(UTC).isoformat(),
                },
                "selection": {
                    "scope": "五个 CSV 的全部记录（崩塌/水毁/沉陷塌陷/泥石流/滑坡）",
                    "exclusions": [],
                },
                "normalization": {
                    "raw_encoding": "gb18030",
                    "raw_line_endings": "CRLF（raw 按字节原样保留）",
                    "normalized_encoding": "utf-8",
                    "normalized_line_endings": "LF",
                    "date_rule": "最近发生时间 → ISO；解析不了留空，原文在 last_event_date_raw",
                    "coord_rule": "经纬度CGCS2K 取第一个点；原串是 'lon,lat;lon,lat;' 折线",
                    "category_map": CATEGORY_MAP,
                    "note": "沉陷塌陷/水毁在项目 HazardType 里没有对应类，落 unknown，原文保留在 source_category",
                },
                "known_limitations": [
                    "公路沿线普查口径，不是行政区域全量灾情；非公路地质灾害点不在内。",
                    "2,070 条 是否发生灾害=否 是隐患点，不是已发生事件（normalized.occurred=false）。",
                    "最近发生时间 2,056 条为空；有值的范围 2012-07-10 至 2022-09-04。",
                    "经纬度是灾害体折线（首点入 normalized），不是单点坐标。",
                ],
                "files": {
                    **raw_meta,
                    "normalized/tibet_road_geohazard_2022.v1.csv": {
                        "sha256": _file_sha256(normalized),
                        "bytes": normalized.stat().st_size,
                        "rows": rows,
                    },
                },
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
            if _file_sha256(path) != expected["sha256"]:
                problems.append(f"{rel}: sha256 不符")
            if path.stat().st_size != expected["bytes"]:
                problems.append(f"{rel}: 字节数不符")
            if "rows" in expected:
                # 原始 CSV 是 GB18030，按字节切行最稳（不猜编码）；首行是表头。
                lines = [line for line in path.read_bytes().splitlines() if line.strip()]
                actual = len(lines) - 1 if path.suffix == ".csv" else len(lines)
                if actual != expected["rows"]:
                    problems.append(f"{rel}: 行数不符（manifest={expected['rows']} actual={actual}）")
    if problems:
        print("数据目录校验失败：", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print(f"OK: {manifest_path} 与数据文件逐项一致")
    return 0


def _refresh(source_dir: Path | None) -> int:
    dataset_dir = _dataset_dir()
    raw_dir = dataset_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    raw_meta: dict[str, Any] = {}
    normalized_rows: list[dict[str, str]] = []
    for name, raw in _source_files(source_dir):
        rows = _read_rows(name, raw)
        target = raw_dir / name
        target.write_bytes(raw)  # 原样保留：编码/换行/SHA 都是证据
        raw_meta[f"raw/{name}"] = {"sha256": _sha256(raw), "bytes": len(raw), "rows": len(rows)}
        normalized_rows.extend(_normalize(name, rows))
    normalized = dataset_dir / "normalized" / "tibet_road_geohazard_2022.v1.csv"
    _write_csv(normalized_rows, normalized)
    manifest_path = _write_manifest(dataset_dir, raw_meta, normalized, len(normalized_rows))
    print(f"原始文件 {len(raw_meta)} 个；规范化 {len(normalized_rows)} 行")
    print(f"manifest: {manifest_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="抓取/校验西藏公路地质灾害普查数据（CC BY-NC 4.0）")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="只校验本地文件与 manifest 一致（默认）")
    mode.add_argument("--refresh", action="store_true", help="下载（或读 --source-dir）并重写 raw/normalized/manifest")
    parser.add_argument("--source-dir", type=Path, help="--refresh 时读取本地 5 个 CSV，不联网")
    args = parser.parse_args(argv)
    if args.refresh:
        return _refresh(args.source_dir)
    return _check()


if __name__ == "__main__":
    raise SystemExit(main())
