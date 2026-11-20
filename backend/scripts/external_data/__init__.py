"""外部公开数据目录：公共内核 + 各数据集的抓取/规范化模块。

模块：
- `core`：清单、校验和、规范化写盘、下载（规矩只有一份）
- `table`：表格读取（xlsx/xls/ods，python-calamine）
- `glof_db`：全球冰湖溃决事件库（Zenodo，CC BY 4.0）
- `nasa_glc`：NASA 全球滑坡目录（public domain）
- `pangaea_debris_flow`：全球泥石流规模目录（PANGAEA，CC BY 3.0）
- `scidb_census`：西藏公路地质灾害普查（CC BY-NC 4.0）
- `tpdc_himalaya`：喜马拉雅重大地质灾害统计（TPDC，CC BY-NC-SA 4.0）
- `zenodo_himalaya`：喜马拉雅东部大型滑坡清单 + 全球巨型滑坡 MEGAS（CC BY 4.0）
- `zenodo_lakes`：帕隆藏布冰湖清单 2020/2025（CC BY 4.0）

入口：`python -m scripts.external_data --check` / `--refresh [--dataset <id>]`。
数据集的唯一登记处是 `cli.MODULES`（目录与索引一致性由门禁钉住）——这里不再维护第二份清单。
"""

from __future__ import annotations

from scripts.external_data.core import DatasetSpec, FileMeta, verify_all

__all__ = ["DatasetSpec", "FileMeta", "verify_all"]
