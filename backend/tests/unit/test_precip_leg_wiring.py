"""外部逐日降水腿的装配面：默认关、填了文件才在、状态面读得出。

要钉住的：
- **默认关**：没有派生表就不该有这条腿（否则状态面多一行"永远 0 条"的腿）；
- 填了路径但文件不在 → 如实说"这条腿不存在"，不静默当成没数据；
- 文件在位 → 这条腿进摄取源清单，状态面报出文件与 metric；
- 容器装配面同样认这条腿（状态面多一行 `precip`）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aegis.config import Settings
from aegis.container import create_container
from aegis.integrations.precip import build_precip


def _settings(tmp_path: Path, *, enabled: bool = True, file: str = "") -> Settings:
    return Settings(
        env="test",
        bus_backend="memory",
        store_backend="memory",
        simulator_enabled=False,
        precip_enabled=enabled,
        precip_daily_file=file,
    )


def _derived_csv(path: Path) -> Path:
    path.write_text(
        "date,station_id,region_code,latitude,longitude,precip_mm\n2020-07-01,RG-540121-01,540121,28.1,94.1,12.5\n",
        encoding="utf-8",
    )
    return path


def test_默认关_这条腿不存在(tmp_path: Path) -> None:
    source, state = build_precip(_settings(tmp_path))
    assert source is None
    assert (state.enabled, state.driver) == (False, "off")


def test_开了开关但没填文件_仍不存在(tmp_path: Path) -> None:
    source, state = build_precip(_settings(tmp_path, enabled=True))
    assert source is None
    assert (state.enabled, state.driver) == (False, "off")


def test_填了路径但文件不在_如实说不存在(tmp_path: Path) -> None:
    missing = tmp_path / "nope.csv"
    source, state = build_precip(_settings(tmp_path, enabled=True, file=str(missing)))
    assert source is None
    assert (state.enabled, state.driver) == (False, "off")
    assert "不存在" in str(state.detail.get("error", ""))


def test_文件在位_这条腿就在(tmp_path: Path) -> None:
    path = _derived_csv(tmp_path / "station_daily.csv")
    source, state = build_precip(_settings(tmp_path, enabled=True, file=str(path)))
    assert source is not None
    assert (state.enabled, state.driver) == (True, "csv")
    assert state.detail["file"] == "station_daily.csv"
    assert state.detail["metric"] == "rain_cumulative_24h"


def test_容器装配面认这条腿(tmp_path: Path) -> None:
    path = _derived_csv(tmp_path / "station_daily.csv")
    settings = _settings(tmp_path, enabled=True, file=str(path))
    ctn = create_container(settings, with_simulator=False)

    status = {state.name: state for state in ctn.integration_status()}
    assert "precip" in status
    assert (status["precip"].enabled, status["precip"].driver) == (True, "csv")
    assert any(source.name == "precip_daily" for source in ctn.ingest.sources)


@pytest.mark.asyncio
async def test_这条腿产的是二十四小时累计不是小时雨强(tmp_path: Path) -> None:
    """日累计与小时雨强是两个口径——这条腿只给前者，不冒充后者。"""
    from datetime import date

    path = tmp_path / "station_daily.csv"
    path.write_text(
        f"date,station_id,region_code,latitude,longitude,precip_mm\n{date.today().isoformat()},RG-540121-01,540121,28.1,94.1,12.5\n",
        encoding="utf-8",
    )
    source, _ = build_precip(_settings(tmp_path, enabled=True, file=str(path)))
    assert source is not None

    readings = await source.collect()
    assert [reading.metric for reading in readings] == ["rain_cumulative_24h"]
