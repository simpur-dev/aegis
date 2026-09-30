"""检索模型取件脚本：把 bge-m3 / bge-reranker-v2-m3 的 ONNX int8 权重与分词器拉到本地目录。

为什么需要它：高原边缘站点常常两件事同时成立——没有 GPU、也没有稳定的公网。权重必须
**提前**放进镜像卷，运行期绝不做隐式下载（否则检索回路的尾延迟里会混进一次 HTTPS 抖动）。

三条硬约束：
1. 幂等：已存在且 sha256 与清单一致的文件直接跳过；`--verify-only` 提供纯离线的自检入口。
2. 完整性：每个文件都边下边算 sha256，落地后写入 `<dest>/sha256sums.json`；
   `PINNED_SHA256` 里已钉住的条目按"不匹配即失败"处理（清单自身由可信渠道随镜像分发）。
3. 失败要说人话：网络不可达与 404/403 是两类完全不同的运维动作（换镜像/离线预置 vs 改路径），
   因此错误消息里带主机、原因与下一步建议。

用法：
    python scripts/fetch_retrieval_models.py                        # 默认 backend/data/models/
    python scripts/fetch_retrieval_models.py --only embedder        # 只取嵌入模型
    python scripts/fetch_retrieval_models.py --verify-only          # 离线校验，不联网
    python scripts/fetch_retrieval_models.py --dest D:\\models       # 指定卷

    # 内网镜像（模板必须含 {repo} 与 {path}）：
    set AEGIS_RETRIEVAL_MIRROR=http://nexus.internal/artifactory/hf/{repo}/resolve/raw/{path}

许可证（模型本体，非本脚本的授权）：bge-m3 MIT、bge-reranker-v2-m3 Apache-2.0；
int8 权重取自 onnx-community 的 optimum 量化导出，沿用原模型许可证。

本脚本不参与测试：pytest 全程不联网、不需要权重（见 tests/unit/test_retrieval_*.py）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DEST = REPO_ROOT / "backend" / "data" / "models"
CHECKSUM_FILE = "sha256sums.json"
USER_AGENT = "aegis-retrieval-fetch/1.0"
CHUNK = 1 << 20

# 候选源按顺序尝试。revision 名各家不同：HF 系用 main，ModelScope 用 master。
MIRRORS: tuple[tuple[str, str], ...] = (
    ("huggingface", "https://huggingface.co/{repo}/resolve/main/{path}"),
    ("hf-mirror", "https://hf-mirror.com/{repo}/resolve/main/{path}"),
    ("modelscope", "https://www.modelscope.cn/models/{repo}/resolve/master/{path}"),
)


@dataclass(frozen=True, slots=True)
class Artifact:
    """一个待取文件：repo 内路径 -> 落到本地模型目录下的相对路径。"""

    repo: str
    src_path: str
    dest_rel: str


@dataclass(frozen=True, slots=True)
class ModelSpec:
    key: str
    title: str
    dir_name: str
    artifacts: tuple[Artifact, ...]
    license_note: str


def _bundle(spec_dir: str, repo: str, *, onnx: str, root_files: tuple[str, ...]) -> tuple[Artifact, ...]:
    """一个模型 = int8 权重 + tokenizers 免组词所需的三个 json（tokenizer 是单文件快路）。"""
    items = [Artifact(repo, onnx, f"{spec_dir}/{Path(onnx).name}")]
    items.extend(Artifact(repo, name, f"{spec_dir}/{name}") for name in root_files)
    return tuple(items)


EMBEDDER = ModelSpec(
    key="embedder",
    title="bge-m3 密集嵌入（1024 维，int8 动态量化）",
    dir_name="bge-m3-int8",
    license_note="MIT (BAAI/bge-m3) via onnx-community/bge-m3-ONNX",
    artifacts=_bundle(
        "bge-m3-int8",
        "onnx-community/bge-m3-ONNX",
        onnx="onnx/model_int8.onnx",
        root_files=("tokenizer.json", "tokenizer_config.json", "special_tokens_map.json", "config.json"),
    ),
)

RERANKER = ModelSpec(
    key="reranker",
    title="bge-reranker-v2-m3 交叉编码器（int8 动态量化）",
    dir_name="bge-reranker-v2-m3-int8",
    license_note="Apache-2.0 (BAAI/bge-reranker-v2-m3) via onnx-community/bge-reranker-v2-m3-ONNX",
    artifacts=_bundle(
        "bge-reranker-v2-m3-int8",
        "onnx-community/bge-reranker-v2-m3-ONNX",
        onnx="onnx/model_int8.onnx",
        root_files=("tokenizer.json", "tokenizer_config.json", "special_tokens_map.json", "config.json"),
    ),
)

SPECS: tuple[ModelSpec, ...] = (EMBEDDER, RERANKER)

# 已核验的权重指纹：2026-09-30 于本机经 hf-mirror 实际取件后 `--record` 得到（REPORT 里记录了来源）。
# 非空条目在运行期强制校验：不匹配即删除文件并失败，绝不部署对不上号的权重。
PINNED_SHA256: dict[str, str] = {
    "bge-m3-int8/model_int8.onnx": "2237f770aad5c71bbc1fc2d361a57f9a37400574cc9eff32626f0cdb49234730",
    "bge-m3-int8/tokenizer.json": "249df0778f236f6ece390de0de746838ef25b9d6954b68c2ee71249e0a9d8fd4",
    "bge-m3-int8/tokenizer_config.json": "b87c8703482b0300d3da30e201519aa641f6a450f5eb5bf1e624afbf70c74d80",
    "bge-m3-int8/special_tokens_map.json": "8c785abebea9ae3257b61681b4e6fd8365ceafde980c21970d001e834cf10835",
    "bge-m3-int8/config.json": "70dae5884ced999af00244f776ac9eaa71538d68497d3d6a6091e0318cd32905",
    "bge-reranker-v2-m3-int8/model_int8.onnx": "912fc1215c2dbff6499700534bd8d31253af01573861abbfc43afd1fab6cce5d",
    "bge-reranker-v2-m3-int8/tokenizer.json": "8bf8afbfd11306bd872018c53bfdf2e160a56f8edbcf49933324404791c148d3",
    "bge-reranker-v2-m3-int8/tokenizer_config.json": "b87c8703482b0300d3da30e201519aa641f6a450f5eb5bf1e624afbf70c74d80",
    "bge-reranker-v2-m3-int8/special_tokens_map.json": "8c785abebea9ae3257b61681b4e6fd8365ceafde980c21970d001e834cf10835",
    "bge-reranker-v2-m3-int8/config.json": "122e922dcfed6503c8721e6fe1daf090340c3d95ca7f3aa3a72730b321a51cfd",
}


class FetchError(RuntimeError):
    """取件失败：消息已面向运维，包含主机与下一步动作。"""


def sha256_of(path: Path, *, progress: Callable[[int], None] | None = None) -> str:
    digest = hashlib.sha256()
    read = 0
    with path.open("rb") as fh:
        while chunk := fh.read(CHUNK):
            digest.update(chunk)
            read += len(chunk)
            if progress is not None:
                progress(read)
    return digest.hexdigest()


def checksum_manifest_path(dest: Path) -> Path:
    return dest / CHECKSUM_FILE


def load_manifest(dest: Path) -> dict[str, str]:
    path = checksum_manifest_path(dest)
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise FetchError(f"完整性清单不可读：{path}（{exc}）。确认卷未被截断后删除它重跑。") from exc
    entries = raw.get("files") if isinstance(raw, dict) else None
    if not isinstance(entries, dict):
        return {}
    return {str(k): str(v) for k, v in entries.items()}


def save_manifest(dest: Path, files: dict[str, str]) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": "aegis.retrieval.models/v1",
        "algorithm": "sha256",
        "note": "本机落地指纹；PINNED_SHA256 非空的条目在运行期强制校验",
        "files": dict(sorted(files.items())),
    }
    path = checksum_manifest_path(dest)
    tmp = path.with_suffix(".json.part")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def mirror_candidates() -> list[tuple[str, str]]:
    """镜像列表：环境变量优先（内网源），其余按内置顺序，最后去重。"""
    custom = os.environ.get("AEGIS_RETRIEVAL_MIRROR", "").strip()
    chain: list[tuple[str, str]] = []
    if custom:
        if "{repo}" not in custom or "{path}" not in custom:
            raise FetchError("AEGIS_RETRIEVAL_MIRROR 模板必须同时包含 {repo} 与 {path} 占位符")
        chain.append(("mirror-env", custom))
    chain.extend(MIRRORS)
    seen: set[str] = set()
    return [entry for entry in chain if not (entry[1] in seen or seen.add(entry[1]))]


def _describe_http_error(exc: urllib.error.HTTPError, url: str) -> str:
    host = urllib.parse.urlsplit(url).netloc
    if exc.code in (401, 403):
        return f"{host} 拒绝访问（HTTP {exc.code}）：该源可能需要登录/gated 授权，或被网关策略拦截"
    if exc.code == 404:
        return f"{host} 上找不到该路径（HTTP 404）：仓库/文件名或 revision 与脚本预期不符"
    return f"{host} 返回 HTTP {exc.code}"


def _describe_url_error(exc: urllib.error.URLError, url: str) -> str:
    host = urllib.parse.urlsplit(url).netloc
    reason = exc.reason
    if isinstance(reason, socket.timeout):
        return f"连接 {host} 超时：本网络到该源不可达（高原站点按离线处理）"
    if isinstance(reason, OSError):
        return f"无法访问 {host}：{reason}"
    return f"无法访问 {host}：{reason}"


def download(artifact: Artifact, target: Path, *, timeout: float, log: Callable[[str], None]) -> tuple[str, int]:
    """尝试所有镜像；返回 (sha256, bytes)。写入走 .part + rename，避免半截文件被当成可用权重。"""
    errors: list[str] = []
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(target.name + ".part")
    for label, template in mirror_candidates():
        url = template.format(repo=artifact.repo, path=artifact.src_path)
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                total = int(response.headers.get("Content-Length") or 0)
                digest = hashlib.sha256()
                read = 0
                next_mark = 64 << 20
                with part.open("wb") as fh:
                    while chunk := response.read(CHUNK):
                        fh.write(chunk)
                        digest.update(chunk)
                        read += len(chunk)
                        if read >= next_mark:
                            suffix = f"/{total >> 20}MB" if total else ""
                            log(f"    {label}: {read >> 20}MB{suffix}")
                            next_mark += 128 << 20
                if total and read != total:
                    raise FetchError(f"{label}: 传输截断（收到 {read}，声明 {total}）")
            os.replace(part, target)
            log(f"  [{label}] {artifact.dest_rel} <- {read} bytes")
            return digest.hexdigest(), read
        except urllib.error.HTTPError as exc:
            errors.append(f"{url}\n      {_describe_http_error(exc, url)}")
        except urllib.error.URLError as exc:
            errors.append(f"{url}\n      {_describe_url_error(exc, url)}")
        except (OSError, FetchError) as exc:
            errors.append(f"{url}\n      {exc}")
        finally:
            part.unlink(missing_ok=True)
    raise FetchError(
        f"全部镜像都取不到 {artifact.repo}/{artifact.src_path}：\n  - "
        + "\n  - ".join(errors)
        + "\n下一步：换源（设 AEGIS_RETRIEVAL_MIRROR 指向内网镜像模板），"
        "或在有网机器上把文件按同名路径放入模型卷，再跑 --record 生成完整性清单。"
    )


def expected_digest(rel: str, manifest: dict[str, str]) -> str | None:
    pinned = PINNED_SHA256.get(rel, "")
    if pinned:
        return pinned
    return manifest.get(rel) or None


def resolve_target(dest: Path, artifact: Artifact) -> Path:
    return dest / artifact.dest_rel


def ensure(
    spec: ModelSpec,
    dest: Path,
    *,
    force: bool,
    timeout: float,
    log: Callable[[str], None],
) -> dict[str, str]:
    """取齐一个模型的全部文件；返回 {相对路径: sha256}。"""
    manifest = load_manifest(dest)
    produced: dict[str, str] = {}
    log(f"[{spec.key}] {spec.title}  ({spec.license_note})")
    for artifact in spec.artifacts:
        target = resolve_target(dest, artifact)
        pinned = expected_digest(artifact.dest_rel, manifest)
        if target.is_file() and not force:
            actual = sha256_of(target)
            if pinned and actual == pinned:
                log(f"  跳过（sha256 命中清单）{artifact.dest_rel}")
                produced[artifact.dest_rel] = actual
                continue
            if pinned:
                log(f"  重取（sha256 与清单不符）{artifact.dest_rel}")
            else:
                log(f"  记录（首次见此文件，无钉住指纹）{artifact.dest_rel}")
                produced[artifact.dest_rel] = actual
                continue
        actual, _size = download(artifact, target, timeout=timeout, log=log)
        if pinned and actual != pinned:
            target.unlink(missing_ok=True)
            raise FetchError(f"{artifact.dest_rel} 校验失败：期望 {pinned}，实际 {actual}（文件已删除，勿部署）")
        produced[artifact.dest_rel] = actual
    return produced


def verify(spec: ModelSpec, dest: Path, manifest: dict[str, str], *, log: Callable[[str], None]) -> list[str]:
    """离线自检：文件在不在、指纹对不对。返回问题列表（空即健康）。"""
    problems: list[str] = []
    for artifact in spec.artifacts:
        target = resolve_target(dest, artifact)
        if not target.is_file():
            problems.append(f"缺失文件：{target}")
            continue
        pinned = PINNED_SHA256.get(artifact.dest_rel) or manifest.get(artifact.dest_rel)
        actual = sha256_of(target)
        if pinned and actual != pinned:
            problems.append(f"指纹不符：{artifact.dest_rel} 期望 {pinned} 实际 {actual}")
        elif pinned is None:
            problems.append(f"无指纹可比：{artifact.dest_rel}（清单缺该条目；跑一次 --record）")
    for line in [f"[{spec.key}] 校验通过"] if not problems else [f"[{spec.key}] 校验失败", *problems]:
        log("  " + line)
    return problems


def parse_specs(raw: str) -> tuple[ModelSpec, ...]:
    if raw == "all":
        return SPECS
    chosen = {part.strip() for part in raw.split(",") if part.strip()}
    unknown = chosen - {spec.key for spec in SPECS}
    if unknown:
        raise argparse.ArgumentTypeError(f"未知模型键：{sorted(unknown)}，可选 embedder/reranker/all")
    return tuple(spec for spec in SPECS if spec.key in chosen)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="拉取 AEGIS 检索模型的 ONNX int8 权重（幂等、可离线校验）")
    parser.add_argument("--dest", type=Path, default=DEFAULT_DEST, help=f"模型卷目录（默认 {DEFAULT_DEST}）")
    parser.add_argument("--only", default="all", help="embedder / reranker / all，或逗号分隔组合")
    parser.add_argument("--timeout", type=float, default=30.0, help="单次 HTTP 读超时秒数")
    parser.add_argument("--force", action="store_true", help="忽略已存在文件，强制重取")
    parser.add_argument("--verify-only", action="store_true", help="只做离线校验，绝不联网")
    parser.add_argument("--record", action="store_true", help="把当前落地文件的 sha256 写进清单后退出（离线）")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    dest: Path = args.dest.expanduser().resolve()
    log = lambda msg: print(msg, flush=True)  # noqa: E731 - 单文件脚本内的局部小助手
    try:
        specs = parse_specs(args.only)
    except argparse.ArgumentTypeError as exc:
        log(f"参数错误：{exc}")
        return 2

    manifest = load_manifest(dest)
    if args.record:
        found: dict[str, str] = {}
        for spec in specs:
            for artifact in spec.artifacts:
                target = resolve_target(dest, artifact)
                if target.is_file():
                    found[artifact.dest_rel] = sha256_of(target)
        if not found:
            log(f"目录 {dest} 下没有可记录的文件；先不带 --record 完成下载")
            return 1
        merged = {**manifest, **found}
        save_manifest(dest, merged)
        for rel, digest in sorted(found.items()):
            log(f"  {digest}  {rel}")
        log(f"清单已写入 {checksum_manifest_path(dest)}（把其中的权重条目抄进 PINNED_SHA256 即可强制校验）")
        return 0

    if args.verify_only:
        problems = [p for spec in specs for p in verify(spec, dest, manifest, log=log)]
        log(f"模型卷：{dest}")
        if problems:
            log(f"共 {len(problems)} 项问题——检索服务会以降级模式启动（HashingEmbedder + 无重排）")
            return 1
        log("两个模型齐备且指纹一致，可启用 bge-m3 检索路径")
        return 0

    collected: dict[str, str] = dict(manifest)
    try:
        for spec in specs:
            collected.update(
                ensure(
                    spec,
                    dest,
                    force=args.force,
                    timeout=args.timeout,
                    log=log,
                )
            )
    except FetchError as exc:
        log(f"\n取件失败：{exc}")
        return 1

    save_manifest(dest, collected)
    log(f"\n完整性清单：{checksum_manifest_path(dest)}")
    log(f"模型卷：{dest}")
    for rel in sorted(collected):
        log(f"  {collected[rel][:16]}…  {rel}")
    log("\n下一步：设 AEGIS_RETRIEVAL_MODEL_DIR 指向上面的两个子目录，或直接把该卷打进边缘镜像。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
