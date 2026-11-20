"""外部降水派生的**离线**用例：用合成的 NetCDF 验派生逻辑。

真数据取件（46.8 GB 的 CHM_PRE V2）是运维动作，不进门禁；进门禁的是**派生逻辑**——
"按物理量纲找降水变量""按最近格点取站值""缺测不补值"这几件事必须对，
否则线上派生出来的表会安静地错。

`h5py` 装在 `[precip]` extra 里（CI 装它；没装时本文件整体跳过）。
"""

from __future__ import annotations

import csv
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

h5py = pytest.importorskip("h5py")

from scripts.derive_precip_station_daily import (  # noqa: E402
    Station,
    derive_from_file,
    load_stations,
    write_csv,
)


def _make_netcdf(path: Path, *, days: int = 3, lats: int = 5, lons: int = 5) -> None:
    """造一个结构与源文件同形的 NetCDF：`prec(time, latY, lonX)`，单位 mm/day。"""
    base = datetime(2020, 7, 1, tzinfo=UTC)
    with h5py.File(path, "w") as handle:
        handle.create_dataset("time", data=[(base + timedelta(days=i)).timestamp() / 86400 for i in range(days)])
        handle.create_dataset("latY", data=[28.0 + 0.5 * i for i in range(lats)])
        handle.create_dataset("lonX", data=[94.0 + 0.5 * i for i in range(lons)])
        prec = handle.create_dataset(
            "prec",
            data=[[[1.0 * (j + 1) for _ in range(lons)] for j in range(lats)] for _ in range(days)],
        )
        prec.attrs["units"] = "mm/day"
        prec.attrs["long_name"] = "Daily precipitation"


def _stations_csv(path: Path) -> Path:
    path.write_text(
        "station_id,region_code,latitude,longitude\nRG-540121-01,540121,28.1,94.1\nRG-540121-02,540121,28.9,94.9\n",
        encoding="utf-8",
    )
    return path


def test_按最近格点取站值(tmp_path: Path) -> None:
    nc = tmp_path / "CHM_PRE_V2_daily_2020.nc"
    _make_netcdf(nc)
    stations = [
        Station(station_id="RG-540121-01", region_code="540121", latitude=28.1, longitude=94.1),
        Station(station_id="RG-540121-02", region_code="540121", latitude=28.9, longitude=94.9),
    ]

    rows: list[list[str]] = []
    derive_from_file(nc, stations, rows)

    assert len(rows) == 3 * 2
    # 站点 1 最近格点：lat 28.0（index 0）、lon 94.0（index 0）→ 值 1.0
    first = next(row for row in rows if row[1] == "RG-540121-01")
    assert first[0] == "2020-07-01"
    assert first[2] == "540121"
    assert float(first[5]) == pytest.approx(1.0)
    # 站点 2 在 lat 28.9，最近格点是 29.0（index 2）→ 值 1.0×3 = 3.0
    second = next(row for row in rows if row[1] == "RG-540121-02")
    assert float(second[5]) == pytest.approx(3.0)


def test_缺测写成空值而不是零(tmp_path: Path) -> None:
    """NaN 是"没测到"，不是"测到是 0"——补出来的 0 会一路走到预警里。"""
    nc = tmp_path / "CHM_PRE_V2_daily_2020.nc"
    _make_netcdf(nc, days=1)
    with h5py.File(nc, "r+") as handle:
        handle["prec"][0, 0, 0] = float("nan")

    rows: list[list[str]] = []
    derive_from_file(nc, [Station("S1", "540121", 28.0, 94.0)], rows)
    assert rows and rows[0][5] == ""


def test_找不到降水变量就报错而不是猜(tmp_path: Path) -> None:
    nc = tmp_path / "wrong.nc"
    with h5py.File(nc, "w") as handle:
        handle.create_dataset("temperature", data=[[1.0]], dtype="f8")
        handle["temperature"].attrs["units"] = "degC"
    with pytest.raises(SystemExit, match="找不到降水变量"):
        derive_from_file(nc, [Station("S1", "540121", 28.0, 94.0)], [])


def test_站点清单读出来带区划码(tmp_path: Path) -> None:
    stations = load_stations(_stations_csv(tmp_path / "stations.csv"))
    assert [station.station_id for station in stations] == ["RG-540121-01", "RG-540121-02"]
    assert all(station.region_code == "540121" for station in stations)


def test_输出表头与列数一致(tmp_path: Path) -> None:
    out = tmp_path / "station_daily.csv"
    write_csv([["2020-07-01", "S1", "540121", "28.100000", "94.100000", "1.00"]], out)
    with out.open(encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        header = next(reader)
        row = next(reader)
    assert header == ["date", "station_id", "region_code", "latitude", "longitude", "precip_mm"]
    assert len(row) == len(header)
