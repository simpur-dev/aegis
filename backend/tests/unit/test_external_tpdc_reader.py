"""TPDC 图层读取（pyshp）的离线用例：用已提交的 raw zip 验 SHP/DBF 直读。

钉三件事：三个图层的行数与 manifest 一致、几何只接受单点、字符字段去尾随空白
（源 DBF 里确有 `P2k、P3b、T3n\\n` 这种带换行的值）。

`pyshp` 装在 `[data]` extra 里（CI 装它；没装时本文件整体跳过）。
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

pytest.importorskip("shapefile")

from scripts.external_data.tpdc_himalaya import _layer_table

RAW = (
    Path(__file__).resolve().parents[3] / "data" / "external" / "tpdc-himalaya-geohazard-2006-2009" / "raw" / "tpdc_himalaya_geohazard.zip"
)
PREFIX = "喜马拉雅山地区重大地质灾害统计数据集/喜马拉雅山重大地质灾害数据集/"


@pytest.mark.parametrize(("layer", "expected"), [("泥石流", 353), ("滑坡", 153), ("溃决隐患冰川终碛湖", 19)])
def test_图层行数与几何数一致(layer: str, expected: int) -> None:
    with zipfile.ZipFile(RAW) as archive:
        rows, geometry = _layer_table(layer, archive, PREFIX)
    assert len(rows) == expected
    assert len(geometry) == expected
    assert all(isinstance(lon, float) and isinstance(lat, float) for lon, lat in geometry)


def test_字符字段去尾随空白() -> None:
    with zipfile.ZipFile(RAW) as archive:
        rows, _ = _layer_table("滑坡", archive, PREFIX)
    stratum = [row.get("地层") for row in rows if isinstance(row.get("地层"), str)]
    assert stratum, "滑坡图层应有地层字段"
    assert all(value == value.strip() for value in stratum), "字符字段必须去尾随空白（旧读器口径）"


def test_数值字段是float而不是字符串() -> None:
    with zipfile.ZipFile(RAW) as archive:
        rows, _ = _layer_table("泥石流", archive, PREFIX)
    assert isinstance(rows[0]["沟口_高程_"], float), "DBF 数值字段应由 pyshp 解析成 float"
