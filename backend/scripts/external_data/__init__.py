"""外部公开数据目录：公共内核 + 各数据集的抓取/规范化模块。

模块：
- `core`：清单、校验和、规范化写盘、下载（规矩只有一份）
- `nasa_glc`：NASA 全球滑坡目录（public domain）
- `scidb_census`：西藏公路地质灾害普查（CC BY-NC 4.0）
- `tpdc_himalaya`：喜马拉雅重大地质灾害统计（TPDC，CC BY-NC-SA 4.0）
- `zenodo_himalaya`：喜马拉雅东部大型滑坡清单 + 全球巨型滑坡 MEGAS（CC BY 4.0）

入口：`python -m scripts.external_data --check` / `--refresh [--dataset <id>]`。
"""

from __future__ import annotations

from scripts.external_data.core import DatasetSpec, FileMeta, verify_all

#: 目录里登记的数据集（`--check` 全查，`--refresh` 可单选）。
DATASET_IDS: tuple[str, ...] = (
    "nasa-glc",
    "sciencedb-tibet-road-geohazard-2022",
    "tpdc-himalaya-geohazard-2006-2009",
    "zenodo-himalaya-landslide-inventories",
)

__all__ = ["DATASET_IDS", "DatasetSpec", "FileMeta", "verify_all"]
