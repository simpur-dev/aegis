"""统一信封（`EnvelopeRecord`）的写入时校验用例。

这条门禁的意义：形状/类型/值域错了要在**写入时**失败（`--refresh` 当场退出），
而不是先落盘、再等目录门禁的 jsonschema 发现。用例覆盖三类：
① 合法记录（含数据集特有的扩展键）能过；
② 缺块/越界/未知灾种被拒；
③ 0 与 null 的空值语义不被混淆。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from scripts.external_data.core import EnvelopeRecord, write_envelope


def _record(**overrides) -> dict:
    base = {
        "schema_version": "1.0.0",
        "record_id": "test:1",
        "source": {"dataset": "test", "license": "public-domain"},
        "event": {"date": "2020-01-01", "title": "样例"},
        "geo": {"latitude": 29.65, "longitude": 91.1, "country": "China", "admin_division": "西藏"},
        "hazard": {"type": "landslide", "source_category": "landslide", "attributes": {}},
        "impact": {"fatalities": None, "injuries": None},
    }
    base.update(overrides)
    return base


def test_合法记录通过且能落盘(tmp_path: Path) -> None:
    out = tmp_path / "out.jsonl"
    meta = write_envelope([_record()], out)
    assert meta.rows == 1
    assert not out.read_bytes().replace(b"\n", b"").count(b"\r"), "必须是 LF"
    assert json.loads(out.read_text(encoding="utf-8"))["record_id"] == "test:1"


def test_数据集特有的扩展键被允许() -> None:
    """source.layer / event.date_start / hazard.scale / geo.quality_flags 都属允许扩展。"""
    record = _record(
        source={"dataset": "test", "license": "public-domain", "layer": "滑坡"},
        event={"date_start": "2006-01-01", "date_end": "2009-12-31"},
        geo={"latitude": 29.0, "longitude": 91.0, "quality_flags": ["latitude_out_of_range"]},
        hazard={"type": "landslide", "scale": "巨型", "attributes": {"slope_angle_deg": 30.0}},
    )
    EnvelopeRecord.model_validate(record)


@pytest.mark.parametrize(
    ("patch", "needle"),
    [
        ({"impact": None}, "impact"),
        ({"hazard": {"type": "tsunami"}}, "未知灾种"),
        ({"geo": {"latitude": 95.0, "longitude": 91.0}}, "latitude 越界"),
        ({"geo": {"latitude": 29.0, "longitude": 200.0}}, "longitude 越界"),
        ({"impact": {"fatalities": -1, "injuries": None}}, "greater than or equal to 0"),
    ],
)
def test_坏记录被拒并指明原因(tmp_path: Path, patch: dict, needle: str) -> None:
    record = _record(**patch)
    with pytest.raises(SystemExit, match=needle):
        write_envelope([record], tmp_path / "out.jsonl")


def test_零与null不混() -> None:
    """0 是"测到是 0"，None 是"没测到"——两者都必须能过模型，且保持各自的值。"""
    zero = _record(impact={"fatalities": 0, "injuries": 0})
    missing = _record(impact={"fatalities": None, "injuries": None})
    assert EnvelopeRecord.model_validate(zero).impact.fatalities == 0
    assert EnvelopeRecord.model_validate(missing).impact.fatalities is None
