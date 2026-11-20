"""外部降水取件脚本：把 CHM_PRE V2（中国逐日 0.1° 格点降水）拉到运行态目录。

**为什么需要它**：平台要接真实雨量，仓库里却没有雨量源——外部公开数据目录里五套都是
灾情/普查/点位，唯独**触发因子（降雨）**缺。这个脚本把那一块补上。

**为什么落运行态目录而不是进 git**：CHM_PRE V2 全量 46.8 GB（逐日 66 个文件 × 673 MB），
按仓库既有的纪律（"大二进制不进 git"），原件落 `backend/data/precip/chm_pre_v2/`，
仓库只留脚本、派生摘要与校验和清单。

三条硬约束（与 `scripts/fetch_retrieval_models.py` 同构）：
1. **幂等**：已存在且 sha256 与清单一致的文件直接跳过；`--verify-only` 提供纯离线的自检入口。
2. **完整性**：边下边算 sha256，落地后写 `<dest>/sha256sums.json`；`--verify-only` 只认清单。
3. **失败要说人话**：TPDC 的 FTP 并发上限满（421）与 Zenodo 的限流是两回事，
   错误消息要分开说，并给出下一步。

两条取数通道（同一份数据，不同托管）：
- **TPDC FTP**（主）：匿名临时账号 + `ftp2/ftp3.tpdc.ac.cn:6201`，支持 `REST` 断点续传；
- **Zenodo 镜像**（备）：官方镜像记录 `10.5281/zenodo.20378336`（CC BY 4.0、access=open、
  每文件带 MD5），走 HTTPS + Range。

许可（数据本体，非本脚本的授权）：CHM_PRE V2 为 **CC BY 4.0**，允许再分发（含商用）。
引用格式见 `data/external/README.md` 与各数据集 README 的"引用"段。

用法：
    python scripts/fetch_precip_chmpre.py --files annual          # 只取年降水（122 MB）
    python scripts/fetch_precip_chmpre.py --files daily --years 1991-2020
    python scripts/fetch_precip_chmpre.py --verify-only            # 离线校验，不联网
    python scripts/fetch_precip_chmpre.py --dest D:\\precip --channel zenodo

本脚本不参与 pytest 全量门禁（不联网）；它的离线部分有对应用例
（`tests/unit/test_fetch_precip_chmpre.py`），用假通道替身跑。
"""

from __future__ import annotations

import argparse
import contextlib
import ftplib
import hashlib
import json
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DEST = REPO_ROOT / "backend" / "data" / "precip" / "chm_pre_v2"
CHECKSUM_FILE = "sha256sums.json"
USER_AGENT = "aegis-precip-fetch/1.0"
CHUNK = 1 << 20

#: TPDC 数据集元数据 id（CHM_PRE V2：中国逐日 0.1° 格点降水数据集 V2）。
TPDC_METADATA_ID = "e5c335d9-cbb9-48a6-ba35-d67dd614bb8c"
TPDC_FTP_PORT = 6201
#: 服务端并发上限满时返回 421；重试即可（换账号重连，不要改路径）。
FTP_MAX_ATTEMPTS = 40

#: Zenodo 官方镜像记录（CC BY 4.0、access=open、每文件带 MD5、支持 Range）。
ZENODO_RECORD = "20378336"
ZENODO_FILE_URL = "https://zenodo.org/api/records/{record}/files/{name}/content"

#: 取哪些文件。`annual`/`monthly` 各一个文件；`daily` 按年切分（每年一个文件）。
FILE_GROUPS = ("annual", "monthly", "daily")


@dataclass(frozen=True, slots=True)
class Artifact:
    """一个待取文件：源文件名 → 落到本地目录下的相对路径。"""

    name: str
    dest_rel: str


def _daily_artifacts(years: Iterable[int]) -> list[Artifact]:
    return [Artifact(f"CHM_PRE_V2_daily_{year}.nc", f"daily/CHM_PRE_V2_daily_{year}.nc") for year in years]


def build_artifacts(files: Iterable[str], years: Iterable[int]) -> list[Artifact]:
    """按 `--files` 与 `--years` 生成待取清单。"""
    wanted = set(files)
    unknown = wanted - set(FILE_GROUPS)
    if unknown:
        raise SystemExit(f"未知的文件分组：{sorted(unknown)}；可选：{list(FILE_GROUPS)}")
    items: list[Artifact] = []
    if "annual" in wanted:
        items.append(Artifact("CHM_PRE_V2_annual.nc", "annual/CHM_PRE_V2_annual.nc"))
    if "monthly" in wanted:
        items.append(Artifact("CHM_PRE_V2_monthly.nc", "monthly/CHM_PRE_V2_monthly.nc"))
    if "daily" in wanted:
        if not years:
            # daily 是按年切分的：不给 --years 会生成空清单，报"没有要取的文件"
            # 会把人引去查 --files，而真正缺的是年份。
            raise SystemExit("daily 分组必须给 --years（如 --years 2020-2025）；整包 46.8 GB，子集取年即可")
        items.extend(_daily_artifacts(years))
    if not items:
        raise SystemExit(f"没有要取的文件；--files 可选：{list(FILE_GROUPS)}")
    return items


# --------------------------------------------------------------------- 通道


class Channel:
    """一条取数通道：能把一个文件拉到本地路径。"""

    name = "base"

    def fetch(self, artifact: Artifact, dest: Path, *, on_progress: Callable[[int], None] | None = None) -> None:
        raise NotImplementedError


class ZenodoChannel(Channel):
    """Zenodo 镜像通道：HTTPS + Range（断点续传）。"""

    name = "zenodo"

    def __init__(self, record: str = ZENODO_RECORD) -> None:
        self.record = record

    def _url(self, artifact: Artifact) -> str:
        return ZENODO_FILE_URL.format(record=self.record, name=artifact.name)

    def fetch(self, artifact: Artifact, dest: Path, *, on_progress: Callable[[int], None] | None = None) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        existing = dest.stat().st_size if dest.exists() else 0
        request = urllib.request.Request(self._url(artifact), headers={"User-Agent": USER_AGENT})
        if existing:
            request.add_header("Range", f"bytes={existing}-")
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                mode = "ab" if existing and response.status == 206 else "wb"
                if mode == "wb":
                    existing = 0
                with dest.open(mode) as handle:
                    while True:
                        chunk = response.read(CHUNK)
                        if not chunk:
                            break
                        handle.write(chunk)
                        existing += len(chunk)
                        if on_progress is not None:
                            on_progress(len(chunk))
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"Zenodo 取 {artifact.name} 失败：HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Zenodo 取 {artifact.name} 失败：{exc.reason}（网络不可达）") from exc


class TpdcFtpChannel(Channel):
    """TPDC FTP 通道：匿名临时账号 + `REST` 断点续传。

    服务端有并发上限（满时 421），因此每次取件都可能要重新申请账号并重连——
    这不是错误，是这个源的常态，错误消息里说清楚。
    """

    name = "tpdc"

    def __init__(self, metadata_id: str = TPDC_METADATA_ID) -> None:
        self.metadata_id = metadata_id

    # ---- 账号 ----
    def _fresh_account(self) -> tuple[str, int, str, str]:
        request = urllib.request.Request(
            f"https://data.tpdc.ac.cn/file/ftp/createNoLoginDownloadFtpUser?metadataId={self.metadata_id}",
            data=b"{}",
            headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.load(response)
        except urllib.error.URLError as exc:
            raise RuntimeError(f"TPDC 申请匿名 FTP 账号失败：{exc.reason}（网络不可达）") from exc
        data = payload.get("data") or {}
        hosts = [host.strip() for host in str(data.get("ftpUrl", "")).split(",") if host.strip()]
        if not hosts:
            raise RuntimeError(f"TPDC 返回的 FTP 地址不可用：{payload}")
        return hosts[0], int(data.get("ftpPort", TPDC_FTP_PORT)), str(data["username"]), str(data["password"])

    def _connect(self) -> ftplib.FTP:
        last: Exception | None = None
        for attempt in range(1, FTP_MAX_ATTEMPTS + 1):
            try:
                host, port, user, password = self._fresh_account()
                ftp = ftplib.FTP()
                ftp.connect(host, port, timeout=60)
                ftp.login(user, password)
                return ftp
            except Exception as exc:
                last = exc
                # 421 = 并发上限满（这个源的常态，退避重试）；其它异常同样重试但退避更久。
                time.sleep(min(3 + attempt * 2, 30))
        raise RuntimeError(f"TPDC FTP 连不上（试了 {FTP_MAX_ATTEMPTS} 次）：{last}")

    def fetch(self, artifact: Artifact, dest: Path, *, on_progress: Callable[[int], None] | None = None) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        ftp = self._connect()
        try:
            directory = str(Path(artifact.dest_rel).parent).replace("\\", "/")
            ftp.cwd(f"/{directory.strip('/')}")
            existing = dest.stat().st_size if dest.exists() else 0
            ftp.sendcmd("TYPE I")  # 二进制模式；ASCII 模式下 REST 会被拒（503 ASCII resume is unsafe）
            if existing:
                ftp.sendcmd(f"REST {existing}")
            conn = ftp.transfercmd(f"RETR {artifact.name}")
            with dest.open("ab" if existing else "wb") as handle:
                while True:
                    chunk = conn.recv(CHUNK)
                    if not chunk:
                        break
                    handle.write(chunk)
                    existing += len(chunk)
                    if on_progress is not None:
                        on_progress(len(chunk))
            conn.close()
        finally:
            with contextlib.suppress(Exception):
                ftp.quit()


# --------------------------------------------------------------------- 校验与清单


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def read_manifest(dest: Path) -> dict[str, str]:
    path = dest / CHECKSUM_FILE
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def write_manifest(dest: Path, digests: dict[str, str]) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    payload = dict(sorted(digests.items()))
    (dest / CHECKSUM_FILE).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")


def verify(dest: Path, artifacts: Iterable[Artifact]) -> list[str]:
    """离线校验：清单里登记的文件是否在盘、sha256 是否一致。返回问题清单。"""
    digests = read_manifest(dest)
    problems: list[str] = []
    for artifact in artifacts:
        path = dest / artifact.dest_rel
        if not path.exists():
            problems.append(f"{artifact.dest_rel} 缺失")
            continue
        expected = digests.get(artifact.dest_rel)
        if expected is None:
            problems.append(f"{artifact.dest_rel} 不在 {CHECKSUM_FILE} 里（清单缺这条）")
            continue
        actual = sha256_file(path)
        if actual != expected:
            problems.append(f"{artifact.dest_rel} sha256 不一致（清单 {expected[:12]}… 实际 {actual[:12]}…）")
    return problems


# --------------------------------------------------------------------- 主流程


def _human(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}GB"


def fetch_all(
    dest: Path,
    artifacts: Iterable[Artifact],
    channels: Iterable[Channel],
    *,
    skip_existing: bool = True,
    on_progress: Callable[[str, int], None] | None = None,
) -> dict[str, str]:
    """逐个取件；一条通道失败自动换下一条。返回 dest_rel → sha256。"""
    digests = read_manifest(dest)
    channel_list = list(channels)
    for artifact in artifacts:
        path = dest / artifact.dest_rel
        if skip_existing and path.exists():
            digests[artifact.dest_rel] = sha256_file(path)
            print(f"[skip] {artifact.dest_rel}（已在盘，{_human(path.stat().st_size)}）", flush=True)
            continue
        last_error: Exception | None = None
        for channel in channel_list:
            progress: Callable[[int], None] | None = None
            if on_progress is not None:
                name = artifact.dest_rel

                def report(size: int, _name: str = name) -> None:
                    on_progress(_name, size)

                progress = report
            try:
                print(f"[get ] {artifact.dest_rel} ← {channel.name}", flush=True)
                channel.fetch(artifact, path, on_progress=progress)
                digests[artifact.dest_rel] = sha256_file(path)
                print(f"[ok  ] {artifact.dest_rel}（{_human(path.stat().st_size)}）", flush=True)
                last_error = None
                break
            except Exception as exc:
                last_error = exc
                print(f"[warn] {channel.name} 取 {artifact.dest_rel} 失败：{exc}", flush=True)
        if last_error is not None:
            raise RuntimeError(f"{artifact.dest_rel} 所有通道都失败，最后一个错误：{last_error}")
    write_manifest(dest, digests)
    return digests


def _parse_years(text: str) -> list[int]:
    """`1991-2020` 或 `1997,2001` → 年份列表。"""
    years: list[int] = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start, _, end = part.partition("-")
            years.extend(range(int(start), int(end) + 1))
        else:
            years.append(int(part))
    if not years:
        raise SystemExit("--years 没解析出年份")
    return years


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="外部降水取件（CHM_PRE V2）")
    parser.add_argument("--dest", type=Path, default=DEFAULT_DEST, help="落盘目录（默认 backend/data/precip/chm_pre_v2）")
    parser.add_argument("--files", default="annual", help=f"要取的文件分组，逗号分隔：{','.join(FILE_GROUPS)}")
    parser.add_argument("--years", default="", help="daily 分组要取的年份，如 1991-2020")
    parser.add_argument("--channel", default="tpdc", choices=["tpdc", "zenodo", "both"], help="取数通道")
    parser.add_argument("--verify-only", action="store_true", help="只做离线校验，不联网")
    parser.add_argument("--force", action="store_true", help="已存在的文件也重取")
    args = parser.parse_args(argv)

    files = [part.strip() for part in args.files.split(",") if part.strip()]
    years = _parse_years(args.years) if args.years else []
    artifacts = build_artifacts(files, years)

    if args.verify_only:
        problems = verify(args.dest, artifacts)
        if problems:
            print("校验未通过：", file=sys.stderr)
            for problem in problems:
                print(f"  - {problem}", file=sys.stderr)
            return 1
        print(f"OK: {len(artifacts)} 个文件与 {CHECKSUM_FILE} 逐项一致")
        return 0

    if args.channel == "tpdc":
        channels: list[Channel] = [TpdcFtpChannel()]
    elif args.channel == "zenodo":
        channels = [ZenodoChannel()]
    else:
        channels = [TpdcFtpChannel(), ZenodoChannel()]

    try:
        fetch_all(args.dest, artifacts, channels, skip_existing=not args.force)
    except RuntimeError as exc:
        print(f"取件失败：{exc}", file=sys.stderr)
        print("下一步：换通道（--channel zenodo）、换时段重试（FTP 421 是并发上限满），或离线预置后跑 --verify-only。", file=sys.stderr)
        return 1
    print(f"完成：{len(artifacts)} 个文件在 {args.dest}，校验和见 {args.dest / CHECKSUM_FILE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
