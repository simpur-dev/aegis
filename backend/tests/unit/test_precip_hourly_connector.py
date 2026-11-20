"""外部逐小时降水连接器：因子引擎的 `rainfall_mm_h` 第一次有了真实源。

要钉住的：
- **只产 `precip_mm_h`**（遥测原始量）——因子名 `rainfall_mm_h` 是它经派生表
  恒等换算（scale=1.0）后的去处，连接器不绕开口径直供因子；
- **只取文件里最新一小时**的读数，更早的小时不倒灌；
- **源停更（>6h）整轮不产**：陈年数据冒充当前雨情比没数据更危险；
- **缺测跳过不补**；越物理线的标 `suspect` 而不是静默丢弃；
- 区划码不合约定的行跳过并计数；
- CSV 缺列是契约破了（上抛 SchemaInvalidError），不是"这批没数"。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aegis.connectors.precip_hourly import MAX_LAG_HOURS, METRIC, PrecipHourlySource
from aegis.errors import SchemaInvalidError


def _csv(tmp_path: Path, rows: list[str], name: str = "openmeteo_hourly.csv") -> Path:
    path = tmp_path / name
    header = "datetime_utc,station_id,region_code,precip_mm_h"
    path.write_text("\n".join([header, *rows]) + "\n", encoding="utf-8")
    return path


def _hour(offset: int = 0) -> str:
    """相对当前的某个整点（默认上一个完整小时），Open-Meteo 落盘形态：`…T14:00Z`。"""
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    return (now - timedelta(hours=offset)).isoformat().replace("+00:00", "Z")


@pytest.mark.asyncio
async def test_最新整点的读数进平台(tmp_path: Path) -> None:
    path = _csv(tmp_path, [f"{_hour(1)},RG-540121-01,540121,4.2", f"{_hour(2)},RG-540121-01,540121,1.0"])
    source = PrecipHourlySource(path)

    readings = await source.collect()

    assert len(readings) == 1, "更早的小时不得倒灌"
    reading = readings[0]
    assert reading.metric == METRIC
    assert reading.value == pytest.approx(4.2)
    assert reading.unit == "mm/h"
    assert reading.source == "precip_hourly"
    assert reading.region_code == "540121"
    assert reading.quality_flag == "ok"
    assert reading.observed_at == _hour(1)


@pytest.mark.asyncio
async def test_最新整点可以是naive小时戳(tmp_path: Path) -> None:
    """Open-Meteo 原始形态是不带时区的 `2026-11-20T14:00`——落盘脚本已归一成 Z，但连接器两种都认。"""
    naive = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    path = _csv(tmp_path, [f"{naive:%Y-%m-%dT%H:%M},RG-540121-01,540121,0.5"])
    source = PrecipHourlySource(path)

    readings = await source.collect()

    assert len(readings) == 1
    assert readings[0].observed_at.endswith("Z")


@pytest.mark.asyncio
async def test_源停更超过上限整轮不产(tmp_path: Path) -> None:
    """把 7 小时前的雨当现在的实况，触发判据会拿旧数据下结论——整轮不产比产错值好。"""
    stale = _hour(MAX_LAG_HOURS + 1)
    path = _csv(tmp_path, [f"{stale},RG-540121-01,540121,4.2"])
    source = PrecipHourlySource(path)

    readings = await source.collect()

    assert readings == []
    status = source.status()
    assert status["stale_rounds"] == 1
    assert status["readings"] == 0


@pytest.mark.asyncio
async def test_缺测与坏行跳过并计数(tmp_path: Path) -> None:
    hour = _hour(1)
    path = _csv(
        tmp_path,
        [
            f"{hour},RG-540121-01,540121,",  # 缺测：空值
            f"{hour},RG-540121-02,540121,abc",  # 值不是数字
            f"{hour},,540121,1.0",  # 缺 station_id
            f"{hour},RG-540121-03,54,1.0",  # 区划码过短
        ],
    )
    source = PrecipHourlySource(path)

    readings = await source.collect()

    assert readings == []
    assert source.status()["skipped"] == 4


@pytest.mark.asyncio
async def test_越物理线的值标suspect不丢(tmp_path: Path) -> None:
    path = _csv(tmp_path, [f"{_hour(1)},RG-540121-01,540121,350.0"])
    source = PrecipHourlySource(path)

    readings = await source.collect()

    assert len(readings) == 1
    assert readings[0].quality_flag == "suspect"


@pytest.mark.asyncio
async def test_多站同小时各产一条(tmp_path: Path) -> None:
    hour = _hour(1)
    path = _csv(tmp_path, [f"{hour},RG-540121-01,540121,3.0", f"{hour},RG-540122-01,540122,0.0"])
    source = PrecipHourlySource(path)

    readings = await source.collect()

    assert len(readings) == 2
    assert {reading.station_id for reading in readings} == {"RG-540121-01", "RG-540122-01"}


@pytest.mark.asyncio
async def test_csv缺列上抛契约错(tmp_path: Path) -> None:
    path = _csv(tmp_path, [f"{_hour(1)},RG-540121-01,540121,4.2"])
    path.write_text("hour,station,region,value\n", encoding="utf-8")
    source = PrecipHourlySource(path)

    with pytest.raises(SchemaInvalidError):
        await source.collect()
    assert source.status()["failures"] == 1


def test_文件不在腿就是关的(tmp_path: Path) -> None:
    source = PrecipHourlySource(tmp_path / "missing.csv")
    assert source.enabled is False
