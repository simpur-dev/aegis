"""外部逐日降水连接器：平台第一个真实雨量来源。

要钉住的：
- **只产 `rain_cumulative_24h`**（24 小时累计），**不产 `rainfall_mm_h`**——
  日累计与小时雨强是两个口径，平台派生表里对后者有明确的不映射处置；
- **只取当天**的读数：遥测库是运行态事实，倒灌历史日值会污染触发判据；
- **缺测跳过不补**（补出来的"真值"会一路走到预警里）；
- 越界的读数标 `suspect` 而不是静默丢弃；
- 区划码不合约定的行跳过并计数（不是造一个能通过的区划码）。
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest

from aegis.connectors.precip_daily import MAX_DAILY_MM, METRIC, PrecipDailySource
from aegis.errors import SchemaInvalidError


def _csv(tmp_path: Path, rows: list[str]) -> Path:
    path = tmp_path / "station_daily.csv"
    header = "date,station_id,region_code,latitude,longitude,precip_mm"
    path.write_text("\n".join([header, *rows]) + "\n", encoding="utf-8")
    return path


def _today() -> str:
    return date.today().isoformat()


def _yesterday() -> str:
    return (date.today() - timedelta(days=1)).isoformat()


@pytest.mark.asyncio
async def test_今天的读数进平台(tmp_path: Path) -> None:
    path = _csv(tmp_path, [f"{_today()},RG-540121-01,540121,28.1,94.1,12.5"])
    source = PrecipDailySource(path)

    readings = await source.collect()

    assert len(readings) == 1
    reading = readings[0]
    assert reading.metric == METRIC
    assert reading.value == pytest.approx(12.5)
    assert reading.unit == "mm"
    assert reading.source == "precip_daily"
    assert reading.region_code == "540121"
    assert reading.quality_flag == "ok"
    assert source.status()["readings"] == 1


@pytest.mark.asyncio
async def test_历史日期不倒灌(tmp_path: Path) -> None:
    """把三个月前的日值当成今天的读数，会让触发判据按旧数据下结论。"""
    path = _csv(tmp_path, [f"{_yesterday()},RG-540121-01,540121,28.1,94.1,12.5"])
    source = PrecipDailySource(path)

    assert await source.collect() == []
    assert source.status()["skipped"] == 1


@pytest.mark.asyncio
async def test_缺测跳过不补零(tmp_path: Path) -> None:
    path = _csv(tmp_path, [f"{_today()},RG-540121-01,540121,28.1,94.1,"])
    source = PrecipDailySource(path)

    assert await source.collect() == []
    assert source.status()["skipped"] == 1


@pytest.mark.asyncio
async def test_越界的读数标suspect而不是丢掉(tmp_path: Path) -> None:
    path = _csv(tmp_path, [f"{_today()},RG-540121-01,540121,28.1,94.1,{MAX_DAILY_MM + 1}"])
    source = PrecipDailySource(path)

    readings = await source.collect()
    assert len(readings) == 1
    assert readings[0].quality_flag == "suspect"


@pytest.mark.asyncio
async def test_区划码不合约定的行跳过(tmp_path: Path) -> None:
    path = _csv(tmp_path, [f"{_today()},RG-540121-01,5401,28.1,94.1,12.5"])
    source = PrecipDailySource(path)

    assert await source.collect() == []
    assert source.status()["skipped"] == 1


@pytest.mark.asyncio
async def test_文件不存在时这条腿不存在而不是报错(tmp_path: Path) -> None:
    source = PrecipDailySource(tmp_path / "missing.csv")

    assert not source.enabled
    assert await source.collect() == []


@pytest.mark.asyncio
async def test_表头缺列是契约破了要上抛(tmp_path: Path) -> None:
    """上游改了派生格式 → 上抛并计数，不当成"这批没数"。

    吞掉它会让"派生脚本坏了"变成"平台静默少一批雨量"——那正是查不出来的那种故障。
    """
    path = tmp_path / "station_daily.csv"
    path.write_text("date,station_id\n2020-07-01,RG-1\n", encoding="utf-8")
    source = PrecipDailySource(path)

    with pytest.raises(SchemaInvalidError):
        await source.collect()
    assert source.status()["failures"] == 1
    assert source.status()["last_error"]


@pytest.mark.asyncio
async def test_行内缺值是单行不合格_跳过计数(tmp_path: Path) -> None:
    path = _csv(tmp_path, [f"{_today()},RG-540121-01,540121,28.1,94.1,"])
    source = PrecipDailySource(path)

    assert await source.collect() == []
    assert source.status()["skipped"] == 1
    assert source.status()["failures"] == 0
