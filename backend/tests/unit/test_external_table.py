"""表格读取（`scripts/external_data/table.py`）的离线用例。

用**已提交的 raw xlsx** 验读取器：不联网、结果稳定，而且正好钉住"读出来的条数"
与数据集 manifest 登记的一致（3,224 条冰湖由两张表各 1,618 / 1,606 行构成）。

`python-calamine` 装在 `[data]` extra 里（CI 装它；没装时本文件整体跳过）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("python_calamine")

from scripts.external_data.table import read_first_sheet, sheet_names

RAW = Path(__file__).resolve().parents[3] / "data" / "external" / "zenodo-parlung-zangbo-glacial-lakes" / "raw"


def test_读第一张表且条数与manifest一致() -> None:
    rows = read_first_sheet((RAW / "parlung_glacial_lakes_2020.xlsx").read_bytes())
    assert len(rows) == 1_618, "2020 期应是 1,618 条数据行（不含表头）"
    assert rows[0]["FID"] == 0, "首行 FID 应是数值 0（不是字符串）"
    assert "X coordinate" in rows[0] and "Y coordinate" in rows[0]


def test_空单元格为空串而不是字符串None() -> None:
    """calamine 的空单元格是 None——必须规范成 ""（与旧读取器口径一致）。"""
    from scripts.external_data.table import _to_records

    records = _to_records([["a", "b", "c"], [1, None, "x"], [None, None, None]])
    assert records == [
        {"a": 1, "b": "", "c": "x"},
        {"a": "", "b": "", "c": ""},
    ]


def test_能列出工作表名() -> None:
    names = sheet_names((RAW / "parlung_glacial_lakes_2020.xlsx").read_bytes())
    assert names, "至少应有一张表"
    assert isinstance(names[0], str)
