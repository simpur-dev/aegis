"""最小 xlsx 读取器：只读第一张工作表，够用且不引第三方依赖。

**为什么自己写**：xlsx 是 zip + XML，`openpyxl`/`pandas` 是重依赖，而本仓只在
"抓外部数据"这一条路上用到表格——为一次抓取把整个依赖树拖进生产镜像不划算。
所以按 xlsx 的最小子集读：共享字符串表 + 第一张表的单元格值。

**边界**（用到就说明，别假装支持）：
- 只读 `xl/worksheets/sheet1.xml`（第一张表）；多表请用 `sheet_names()` 自行核对；
- 不处理公式（只读 `<v>` 缓存值）、不处理日期序列号（源数据里的日期是字符串）；
- 行内字符串 `<is>` 不支持（源数据用共享字符串表）。
"""

from __future__ import annotations

import io
import re
import zipfile
from typing import Any

_CELL_RE = re.compile(r'<c r="([A-Z]+)(\d+)"([^>]*)>(.*?)</c>', re.S)
_ROW_RE = re.compile(r"<row[^>]*>(.*?)</row>", re.S)
_SHARED_RE = re.compile(r"<t[^>]*>([^<]*)</t>")


def _column_index(letters: str) -> int:
    """`A`→0，`B`→1，… `AA`→26。"""
    value = 0
    for ch in letters:
        value = value * 26 + (ord(ch) - ord("A") + 1)
    return value - 1


def sheet_names(xlsx: bytes) -> list[str]:
    with zipfile.ZipFile(io.BytesIO(xlsx)) as inner:
        workbook = inner.read("xl/workbook.xml").decode("utf-8", "replace")
    return re.findall(r'<sheet[^>]*name="([^"]*)"', workbook)


def read_first_sheet(xlsx: bytes) -> list[dict[str, Any]]:
    """读第一张表，返回每行的**列名 → 值**字典（第一行当表头）。"""
    with zipfile.ZipFile(io.BytesIO(xlsx)) as inner:
        shared = _SHARED_RE.findall(inner.read("xl/sharedStrings.xml").decode("utf-8", "replace"))
        sheet = inner.read("xl/worksheets/sheet1.xml").decode("utf-8", "replace")

    rows: list[dict[int, Any]] = []
    for raw_row in _ROW_RE.findall(sheet):
        values: dict[int, Any] = {}
        for match in _CELL_RE.finditer(raw_row):
            col_letters, _row_no, attrs, body = match.groups()
            value_match = re.search(r"<v>(.*?)</v>", body, re.S)
            if value_match is None:
                continue
            raw = value_match.group(1)
            if 't="s"' in attrs:
                values[_column_index(col_letters)] = shared[int(raw)]
            else:
                values[_column_index(col_letters)] = raw
        rows.append(values)

    if not rows:
        return []
    header = rows[0]
    out: list[dict[str, Any]] = []
    for row in rows[1:]:
        record = {str(header.get(index)): row.get(index, "") for index in header}
        out.append(record)
    return out


def read_sheet(xlsx: bytes, sheet_path: str) -> list[dict[str, Any]]:
    """读指定工作表（路径形如 `xl/worksheets/sheet5.xml`）。"""
    with zipfile.ZipFile(io.BytesIO(xlsx)) as inner:
        shared = _SHARED_RE.findall(inner.read("xl/sharedStrings.xml").decode("utf-8", "replace"))
        sheet = inner.read(sheet_path).decode("utf-8", "replace")
    rows: list[dict[int, Any]] = []
    for raw_row in _ROW_RE.findall(sheet):
        values: dict[int, Any] = {}
        for match in _CELL_RE.finditer(raw_row):
            col_letters, _row_no, attrs, body = match.groups()
            value_match = re.search(r"<v>(.*?)</v>", body, re.S)
            if value_match is None:
                continue
            raw = value_match.group(1)
            values[_column_index(col_letters)] = shared[int(raw)] if 't="s"' in attrs else raw
        rows.append(values)
    if not rows:
        return []
    header = rows[0]
    return [{str(header.get(index)): row.get(index, "") for index in header} for row in rows[1:]]


__all__ = ["read_first_sheet", "read_sheet", "sheet_names"]
