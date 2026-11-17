"""外部公开数据目录的公共内核：清单、校验和、规范化写盘、下载。

`data/external/` 里的每个数据集都遵守同一套规矩，所以规矩本身只写一份：

- 一个 `manifest.json` 记来源/许可/抓取时间/逐文件 SHA-256/条数/选择与规范化口径；
- `raw/` 是证据（原始字节或原始列），`normalized/` 是可用数据；
- `--check` 逐文件比对 SHA-256 / 字节数 / 行数，**任何手改都红**。

**`normalized/` 只有一种格式**：JSONL + 统一信封
（`schema_version` / `record_id` / `source` / `event` / `geo` / `hazard` / `impact`，
数据集特有的列进 `hazard.attributes` 或 `extra`）。这样整个目录**只有一个读取器**——
数据源可以有五种，形状不能有两种。

数据集各自的口径（怎么筛、怎么映射、有什么偏差）写在各自的模块与 README 里，
内核不猜、不补、不清洗——**空值语义由数据集自己定，内核只负责核账**。
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

USER_AGENT = "aegis-data-fetch/1.0 (research)"


def repo_root() -> Path:
    """backend/scripts/external_data/core.py → 仓库根。"""
    return Path(__file__).resolve().parents[3]


def data_root() -> Path:
    return repo_root() / "data" / "external"


def dataset_dir(dataset_id: str) -> Path:
    return data_root() / dataset_id


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(slots=True)
class FileMeta:
    """一个落盘文件的账：校验和 + 字节数 + 行数。"""

    sha256: str
    bytes: int
    rows: int | None = None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"sha256": self.sha256, "bytes": self.bytes}
        if self.rows is not None:
            out["rows"] = self.rows
        return out


def file_meta(path: Path, *, rows: int | None = None) -> FileMeta:
    return FileMeta(sha256=sha256_file(path), bytes=path.stat().st_size, rows=rows)


@dataclass(slots=True)
class DatasetSpec:
    """一个数据集的"户口本"：它是什么、从哪来、什么许可、怎么选、怎么规范化。"""

    id: str
    title: str
    publisher: str
    license_id: str
    license_url: str
    citation: str
    landing_url: str
    authors: list[str] = field(default_factory=list)
    doi: str | None = None
    cstr: str | None = None
    commercial_use: bool = True
    license_note: str = ""
    source_urls: dict[str, str] = field(default_factory=dict)
    selection: dict[str, Any] = field(default_factory=dict)
    normalization: dict[str, Any] = field(default_factory=dict)
    known_limitations: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "title": self.title,
            "publisher": self.publisher,
            "authors": self.authors,
            "license": {
                "id": self.license_id,
                "url": self.license_url,
                "commercial_use": self.commercial_use,
            },
            "citation": self.citation,
            "source": {"landing_url": self.landing_url, **self.source_urls},
        }
        if self.doi:
            out["doi"] = self.doi
        if self.cstr:
            out["cstr"] = self.cstr
        if self.license_note:
            out["license"]["note"] = self.license_note
        if self.selection:
            out["selection"] = self.selection
        if self.normalization:
            out["normalization"] = self.normalization
        if self.known_limitations:
            out["known_limitations"] = self.known_limitations
        out.update(self.extra)
        return out


def write_manifest(spec: DatasetSpec, files: dict[str, FileMeta], *, retrieved_at: str | None = None) -> Path:
    """写 `manifest.json`。`generated_at` 与 `retrieved_at` 每次刷新都会变——**diff 里看到它们变了是正常的**。"""
    manifest = {
        "schema_version": "1.0.0",
        "generated_at": datetime.now(UTC).isoformat(),
        "datasets": [
            {
                **spec.as_dict(),
                "retrieved_at": retrieved_at or datetime.now(UTC).isoformat(),
                "files": {rel: meta.as_dict() for rel, meta in files.items()},
            }
        ],
    }
    path = dataset_dir(spec.id) / "manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    return path


def read_manifest(dataset_id: str) -> dict[str, Any]:
    path = dataset_dir(dataset_id) / "manifest.json"
    return json.loads(path.read_text(encoding="utf-8"))


def count_rows(path: Path) -> int:
    """数**记录**数（不含表头）。

    CSV 不能靠数行：源数据的描述字段里有内嵌换行（NASA GLC 就有），
    所以走真正的 csv 解析；编码按 utf-8-sig → gb18030 依次试。
    """
    if path.suffix == ".csv":
        data = path.read_bytes()
        text: str | None = None
        for encoding in ("utf-8-sig", "gb18030"):
            try:
                text = data.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        if text is None:
            raise SystemExit(f"{path} 既不是 utf-8 也不是 gb18030，无法计数")
        return max(0, sum(1 for _ in csv.reader(io.StringIO(text))) - 1)
    return sum(1 for line in path.read_bytes().splitlines() if line.strip())


def verify_dataset(dataset_id: str) -> list[str]:
    """核对 manifest 与盘上文件；返回问题清单（空 = 一致）。"""
    problems: list[str] = []
    root = dataset_dir(dataset_id)
    manifest_path = root / "manifest.json"
    if not manifest_path.exists():
        return [f"{dataset_id}: 缺 manifest.json"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for dataset in manifest.get("datasets", []):
        for rel, expected in dataset.get("files", {}).items():
            path = root / rel
            if not path.exists():
                problems.append(f"{dataset_id}/{rel}: 文件不存在")
                continue
            if sha256_file(path) != expected["sha256"]:
                problems.append(f"{dataset_id}/{rel}: sha256 不符（数据被手改过？）")
            if path.stat().st_size != expected["bytes"]:
                problems.append(f"{dataset_id}/{rel}: 字节数不符")
            if "rows" in expected and count_rows(path) != expected["rows"]:
                problems.append(f"{dataset_id}/{rel}: 行数不符（manifest={expected['rows']} actual={count_rows(path)}）")
    return problems


def verify_all(dataset_ids: Iterable[str]) -> list[str]:
    problems: list[str] = []
    for dataset_id in dataset_ids:
        problems.extend(verify_dataset(dataset_id))
    return problems


def write_envelope(
    records: Sequence[dict[str, Any]],
    path: Path,
    *,
    extra_keys: Sequence[str] = (),
) -> FileMeta:
    """写统一信封的 JSONL。

    信封字段固定七个：`schema_version` / `record_id` / `source` / `event` / `geo` /
    `hazard` / `impact`；数据集特有的列进 `extra`（键名在 `extra_keys` 里登记，
    门禁会检查"登记了但没出现"或"出现了但没登记"）。
    """
    required = ("schema_version", "record_id", "source", "event", "geo", "hazard", "impact")
    for index, record in enumerate(records):
        missing = [key for key in required if key not in record]
        if missing:
            raise SystemExit(f"记录 {record.get('record_id', index)} 缺信封字段: {missing}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        for record in records:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    return file_meta(path, rows=len(records))


def write_jsonl(records: Sequence[dict[str, Any]], path: Path) -> FileMeta:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        for record in records:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    return file_meta(path, rows=len(records))


def write_csv(rows: Sequence[dict[str, Any]], columns: Sequence[str], path: Path) -> FileMeta:
    """写 CSV，**LF 换行**（仓库 `.gitattributes` 是 `eol=lf`；工作区留 CRLF 会让 SHA 在新克隆里失配）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(columns), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    return file_meta(path, rows=len(rows))


def write_bytes(data: bytes, path: Path) -> FileMeta:
    """按字节原样落盘（原始证据：编码、换行都不动）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return file_meta(path)


def download(url: str, *, timeout: int = 240, retries: int = 3) -> bytes:
    """匿名下载。源 URL 的 path 里可能有空格与中文（已百分号编码）：safe 保留 `%` 避免二次编码。

    重试：这些公开门户偶尔在 TLS 握手阶段断（`UNEXPECTED_EOF_WHILE_READING`）——
    那是网络抖动不是"没有这份数据"，所以退避重试；重试耗尽才报错。
    """
    safe = urllib.parse.quote(url, safe=":/?&=%")
    last_error: Exception | None = None
    for attempt in range(retries):
        request = urllib.request.Request(safe, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt + 1 < retries:
                time.sleep(2 * (attempt + 1))
    raise SystemExit(f"下载失败（重试 {retries} 次）：{url} — {last_error}")


def post_json(url: str, payload: dict[str, Any], *, timeout: int = 120) -> bytes:
    """POST JSON（ScienceDB / TPDC 的公开接口用这个形式）。"""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"User-Agent": USER_AGENT, "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def read_csv_rows(data: bytes, *, encoding: str = "utf-8-sig") -> list[dict[str, str]]:
    """按指定编码读 CSV（源数据有 GB18030 的）。"""
    text = data.decode(encoding)
    return list(csv.DictReader(io.StringIO(text)))


def parse_dms(raw: str) -> float | None:
    """度分秒 → 十进制度。`096° 57' 8.81" E` → 96.952469。写错符号会静默出错，所以按 E/W/N/S 判符号。"""
    text = (raw or "").strip()
    if not text:
        return None
    try:
        degrees = float(text.split("°")[0])
        minutes = float(text.split("'")[0].split("°")[-1])
        seconds = float(text.split('"')[0].split("'")[-1])
    except (IndexError, ValueError):
        return None
    decimal = degrees + minutes / 60.0 + seconds / 3600.0
    if text.endswith(("W", "S")):
        decimal = -decimal
    return decimal


__all__ = [
    "DatasetSpec",
    "FileMeta",
    "count_rows",
    "data_root",
    "dataset_dir",
    "download",
    "file_meta",
    "parse_dms",
    "post_json",
    "read_csv_rows",
    "read_manifest",
    "repo_root",
    "sha256_bytes",
    "sha256_file",
    "verify_all",
    "verify_dataset",
    "write_bytes",
    "write_csv",
    "write_envelope",
    "write_jsonl",
    "write_manifest",
]
