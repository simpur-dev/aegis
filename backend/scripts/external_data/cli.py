"""外部公开数据目录的统一入口。

    # 校验：manifest 与盘上文件逐项比对（SHA-256 / 字节数 / 行数）
    uv run python -m scripts.external_data --check

    # 重抓：联网下载并重写 raw / normalized / manifest
    uv run python -m scripts.external_data --refresh

    # 只动一个数据集
    uv run python -m scripts.external_data --refresh --dataset tpdc-himalaya-geohazard-2006-2009

**每个数据集一个模块**（`nasa_glc` / `scidb_census` / `tpdc_himalaya` / `zenodo_himalaya` /
`zenodo_lakes`），规矩只有一份（`core.py`）。改数据必须走 `--refresh` 并复核 manifest 的 diff；
手改数据会被 `--check` 与门禁测试钉成红。
"""

from __future__ import annotations

import argparse
import importlib
import inspect
import sys
from pathlib import Path
from typing import Any

#: 数据集 id → 模块名。顺序即目录索引顺序。
MODULES: dict[str, str] = {
    "nasa-glc": "nasa_glc",
    "pangaea-debris-flow-magnitude": "pangaea_debris_flow",
    "sciencedb-tibet-road-geohazard-2022": "scidb_census",
    "tpdc-himalaya-geohazard-2006-2009": "tpdc_himalaya",
    "zenodo-himalaya-landslide-inventories": "zenodo_himalaya",
    "zenodo-parlung-zangbo-glacial-lakes": "zenodo_lakes",
}


def _load(dataset_id: str) -> Any:
    module_name = MODULES.get(dataset_id)
    if module_name is None:
        raise SystemExit(f"未知数据集 {dataset_id!r}；可选：{', '.join(MODULES)}")
    return importlib.import_module(f"scripts.external_data.{module_name}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="外部公开数据目录：抓取/校验")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="只校验本地文件与 manifest 一致（默认）")
    mode.add_argument("--refresh", action="store_true", help="下载并重写 raw / normalized / manifest")
    parser.add_argument("--dataset", help=f"只处理这一个数据集（可选：{', '.join(MODULES)}）")
    parser.add_argument("--source", type=Path, help="离线复跑：从本地副本读（NASA 用源 CSV，普查用含 5 个 CSV 的目录）")
    args = parser.parse_args(argv)

    targets = [args.dataset] if args.dataset else list(MODULES)
    if args.refresh:
        for dataset_id in targets:
            module = _load(dataset_id)
            print(f"=== {dataset_id}")
            # 只有支持离线复跑的模块接 `source` 参数——用签名判断，不靠 try/except
            # （except TypeError 会把模块内部的 TypeError 一起吞掉）。
            supports_source = "source" in inspect.signature(module.refresh).parameters
            module.refresh(args.source) if supports_source else module.refresh()
        return 0

    problems: list[str] = []
    for dataset_id in targets:
        module = _load(dataset_id)
        problems.extend(module.check())
    if problems:
        print("数据目录校验失败：", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        print("改了数据就重跑 `--refresh` 并复核 manifest 的 diff。", file=sys.stderr)
        return 1
    print(f"OK: {len(targets)} 个数据集的 manifest 与数据文件逐项一致")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
