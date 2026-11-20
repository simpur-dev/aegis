"""表格读取：xlsx / xls / ods 统一走 python-calamine（Rust 后端）。

**为什么不再手搓 xlsx**：旧 `xlsx.py` 只认"第一张表 + 共享字符串 + 缓存值"这一最小子集，
公式、内联字符串、日期序列号、多表都要调用方自己小心；而下一个要入库的数据集
（GLOF DB V3.1）是 **ods**，手搓那套直接不支持。calamine 一个后端同时覆盖
xlsx/xls/ods，且是 Rust 实现，比纯 Python 解析快一个量级。

**口径**：
- 空单元格在 calamine 里是 `None` → 统一转成 `""`（与旧读取器一致：空就是空，不是字符串 "None"）；
- 数值/布尔原样返回（调用方自己决定怎么格式化）；日期单元格 calamine 返回 `datetime` 对象，
  本模块**不做时区/日期推断**——源表里该是字符串的列由调用方按字符串读；
- 只读指定工作表（默认第一张）；多表用 `sheet_names()` 先核对。

依赖：`python-calamine`（MIT），装在 **`[data]` extra** 里——**不进生产镜像**。
"""

from __future__ import annotations

import io
from typing import Any

from python_calamine import CalamineWorkbook


def _rows(data: bytes, sheet_index: int) -> list[list[Any]]:
    workbook = CalamineWorkbook.from_filelike(io.BytesIO(data))
    return workbook.get_sheet_by_index(sheet_index).to_python()


def sheet_names(data: bytes) -> list[str]:
    """列出所有工作表名（多表文件先核对再读）。"""
    return list(CalamineWorkbook.from_filelike(io.BytesIO(data)).sheet_names)


def read_first_sheet(data: bytes) -> list[dict[str, Any]]:
    """读第一张表：首行当表头，返回每行 `{列名: 值}`；空单元格为 `""`。"""
    return _to_records(_rows(data, 0))


def read_sheet(data: bytes, sheet_index: int) -> list[dict[str, Any]]:
    """按**索引**读指定工作表（0 基）。"""
    return _to_records(_rows(data, sheet_index))


def _to_records(rows: list[list[Any]]) -> list[dict[str, Any]]:
    if not rows:
        return []
    header = ["" if cell is None else str(cell) for cell in rows[0]]
    records: list[dict[str, Any]] = []
    for row in rows[1:]:
        record: dict[str, Any] = {}
        for index, name in enumerate(header):
            value = row[index] if index < len(row) else None
            record[name] = "" if value is None else value
        records.append(record)
    return records


__all__ = ["read_first_sheet", "read_sheet", "sheet_names"]
