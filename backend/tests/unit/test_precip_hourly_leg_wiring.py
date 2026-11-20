"""外部逐小时降水腿的装配面：默认关、填了文件才在、状态面读得出。

要钉住的：
- **默认关**：没有取数文件就不该有这条腿；
- 填了路径但文件不在 → 如实说"这条腿不存在"，不静默当成没数据；
- 文件在位 → 这条腿进摄取源清单，状态面报出文件与 metric；
- 容器装配面同样认这条腿（状态面多一行 `precip_hourly`）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aegis.config import Settings
from aegis.container import create_container
from aegis.integrations.precip_hourly import build_precip_hourly


def _settings(tmp_path: Path, *, enabled: bool = True, file: str = "") -> Settings:
    return Settings(
        env="test",
        bus_backend="memory",
        store_backend="memory",
        simulator_enabled=False,
        precip_hourly_enabled=enabled,
        precip_hourly_file=file,
    )


def _hourly_csv(path: Path) -> Path:
    hour = (datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    path.write_text(f"datetime_utc,station_id,region_code,precip_mm_h\n{hour},RG-540121-01,540121,2.5\n", encoding="utf-8")
    return path


def test_默认关_这条腿不存在(tmp_path: Path) -> None:
    source, state = build_precip_hourly(_settings(tmp_path))
    assert source is None
    assert (state.enabled, state.driver) == (False, "off")


def test_填了路径但文件不在_如实说不存在(tmp_path: Path) -> None:
    source, state = build_precip_hourly(_settings(tmp_path, file=str(tmp_path / "nope.csv")))
    assert source is None
    assert (state.enabled, state.driver) == (False, "off")
    assert "不存在" in str(state.detail.get("error", ""))


def test_文件在位_这条腿就在(tmp_path: Path) -> None:
    path = _hourly_csv(tmp_path / "openmeteo_hourly.csv")
    source, state = build_precip_hourly(_settings(tmp_path, file=str(path)))
    assert source is not None
    assert (state.enabled, state.driver) == (True, "csv")
    assert state.detail["file"] == "openmeteo_hourly.csv"
    assert state.detail["metric"] == "precip_mm_h"


def test_容器装配面认这条腿(tmp_path: Path) -> None:
    path = _hourly_csv(tmp_path / "openmeteo_hourly.csv")
    settings = _settings(tmp_path, file=str(path))
    ctn = create_container(settings, with_simulator=False)

    status = {state.name: state for state in ctn.integration_status()}
    assert "precip_hourly" in status
    assert (status["precip_hourly"].enabled, status["precip_hourly"].driver) == (True, "csv")
    assert any(source.name == "precip_hourly" for source in ctn.ingest.sources)


@pytest.mark.asyncio
async def test_这条腿产的是小时口径_经派生表换算成因子(tmp_path: Path) -> None:
    """连接器只供原始量 `precip_mm_h`；因子名走派生表 scale=1.0——口径换算不许绕开表。"""
    from aegis.pipeline.assess_input import DERIVATIONS

    path = _hourly_csv(tmp_path / "openmeteo_hourly.csv")
    source, _ = build_precip_hourly(_settings(tmp_path, file=str(path)))
    assert source is not None

    readings = await source.collect()
    assert [reading.metric for reading in readings] == ["precip_mm_h"]
    derivation = DERIVATIONS["precip_mm_h"]
    assert derivation.factor == "rainfall_mm_h"
    assert derivation.scale == 1.0
