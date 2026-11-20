"""外部降水派生的**离线**用例：用合成的 NetCDF 验派生逻辑。

真数据取件（46.8 GB 的 CHM_PRE V2）是运维动作，不进门禁；进门禁的是**派生逻辑**——
"按物理量纲找降水变量""按最近格点取站值""缺测不补值""时间轴按 CF 单位解码"这几件事
必须对，否则线上派生出来的表会安静地错。

合成文件用 xarray 自己写（而不是裸 h5py）：xarray 写出来的是合法 NetCDF-4
（带维度刻度），与真实源文件同一种读法；裸 h5py 手搭的 HDF5 缺维度刻度，
xarray/h5netcdf 读起来要靠猜，测试就测不到真实路径。

`xarray` + `h5netcdf` 装在 `[data]` extra 里（CI 装它；没装时本文件整体跳过）。
"""

from __future__ import annotations

import csv
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

xr = pytest.importorskip("xarray")
pytest.importorskip("h5netcdf")

from scripts.derive_precip_station_daily import (  # noqa: E402
    Station,
    derive_from_file,
    load_stations,
    write_csv,
)


def _make_netcdf(
    path: Path,
    *,
    days: int = 3,
    lats: int = 5,
    lons: int = 5,
    time_units: str | None = "days since 1970-01-01",
) -> None:
    """造一个结构与源文件同形的 NetCDF：`prec(time, latY, lonX)`，单位 mm/day。

    `time_units=None` 时故意**不写** `units` 属性——覆盖"没声明单位"的旧文件
    （xarray 不会 CF 解码，派生脚本按 1970 纪元天数兜底）。
    """
    base = datetime(2020, 7, 1, tzinfo=UTC)
    raw_time = [(base + timedelta(days=i)).timestamp() / 86400 for i in range(days)]
    data = {
        "latY": ("latY", [28.0 + 0.5 * i for i in range(lats)]),
        "lonX": ("lonX", [94.0 + 0.5 * i for i in range(lons)]),
        "prec": (
            ("time", "latY", "lonX"),
            [[[1.0 * (j + 1) for _ in range(lons)] for j in range(lats)] for _ in range(days)],
            {"units": "mm/day", "long_name": "Daily precipitation"},
        ),
    }
    if time_units is None:
        data["time"] = ("time", raw_time)
    else:
        data["time"] = ("time", raw_time, {"units": time_units})
    with xr.Dataset(data) as ds:
        ds.to_netcdf(path, engine="h5netcdf")


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
    # 先读进内存再关闭，然后新建一个数据集写回（同一个文件边读边写会被 h5py 锁住）。
    with xr.open_dataset(nc) as ds:
        prec = ds["prec"].values.copy()
        attrs = dict(ds["prec"].attrs)
        payload = {name: ds[name].values for name in ("latY", "lonX", "time")}
        time_attrs = dict(ds["time"].attrs)
    prec[0, 0, 0] = float("nan")
    with xr.Dataset(
        {
            "prec": (("time", "latY", "lonX"), prec, attrs),
            "latY": ("latY", payload["latY"]),
            "lonX": ("lonX", payload["lonX"]),
            "time": ("time", payload["time"], time_attrs),
        }
    ) as ds:
        ds.to_netcdf(nc, engine="h5netcdf")

    rows: list[list[str]] = []
    derive_from_file(nc, [Station("S1", "540121", 28.0, 94.0)], rows)
    assert rows and rows[0][5] == ""


def test_找不到降水变量就报错而不是猜(tmp_path: Path) -> None:
    nc = tmp_path / "wrong.nc"
    with xr.Dataset(
        {
            "temperature": (
                ("latY", "lonX"),
                [[1.0]],
                {"units": "degC"},
            ),
            "latY": ("latY", [28.0]),
            "lonX": ("lonX", [94.0]),
        }
    ) as ds:
        ds.to_netcdf(nc, engine="h5netcdf")
    with pytest.raises(SystemExit, match="找不到降水变量"):
        derive_from_file(nc, [Station("S1", "540121", 28.0, 94.0)], [])


def test_时间轴按CF单位解码而不是按1970猜(tmp_path: Path) -> None:
    """★ 修复的 bug：CHM_PRE 用 `days since 1960-01-01`，按 1970 猜会整表偏十年。

    合成文件写 1960 纪元 + 22000 天：CF 解码后应得 1960+22000 天；
    若脚本仍按 1970 纪元算，会得到 1970+22000 天（差 3653 天）——断言同时排除后者。
    """
    nc = tmp_path / "CHM_PRE_V2_daily_2020.nc"
    offset_days = 22_000
    with xr.Dataset(
        {
            "prec": (
                ("time", "latY", "lonX"),
                [
                    [[1.0]],
                ],
                {"units": "mm/day"},
            ),
            "latY": ("latY", [28.0]),
            "lonX": ("lonX", [94.0]),
            "time": ("time", [float(offset_days)], {"units": "days since 1960-01-01"}),
        }
    ) as ds:
        ds.to_netcdf(nc, engine="h5netcdf")

    rows: list[list[str]] = []
    derive_from_file(nc, [Station("S1", "540121", 28.0, 94.0)], rows)
    assert len(rows) == 1
    expected = (datetime(1960, 1, 1) + timedelta(days=offset_days)).date().isoformat()
    wrong = (datetime(1970, 1, 1) + timedelta(days=offset_days)).date().isoformat()
    assert rows[0][0] == expected
    assert rows[0][0] != wrong, "仍按 1970 纪元猜——CF 单位没被读"


def test_没声明单位时保留1970纪元旧行为(tmp_path: Path) -> None:
    """没有 `units` 属性的文件 CF 解不了——按旧行为兜底（原始值即自 1970 起的天数）。"""
    nc = tmp_path / "legacy.nc"
    _make_netcdf(nc, days=2, time_units=None)
    rows: list[list[str]] = []
    derive_from_file(nc, [Station("S1", "540121", 28.0, 94.0)], rows)
    # 合成文件里的原始值就是 2020-07-01 起的天数（自 1970 纪元），兜底路径应解回这两天。
    assert [row[0] for row in rows] == ["2020-07-01", "2020-07-02"]


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
