"""降水派生脚本：从 CHM_PRE V2 的 NetCDF 原件派生**站点逐日降水量**。

原件由 `scripts/fetch_precip_chmpre.py` 取到运行态目录（`backend/data/precip/chm_pre_v2/daily/`），
本脚本把它派生成平台能直接吃的一张小表：`station_daily.csv`
（`date, station_id, region_code, latitude, longitude, precip_mm`）。

**为什么按站点取而不是按区域取**：平台认的是"站 + 指标"（`TelemetryReading`），
区域级聚合需要行政区几何——仓里没有。与其造一个行政区面平均（几何是假的），
不如取**站点所在格点**的格点值（最近格点法），这是气象站转网格的通行做法，
误差来源写在输出与 README 里。

**口径**：
- 取的是**逐日降水量（mm/day）**，对应平台的 `rain_cumulative_24h`；
  它**不折算小时雨强**（`rainfall_mm_h`）——日累计与小时雨强口径不同，
  平台的派生表里对此有明确的不映射处置（`assess_input.UNMAPPED_REASONS`）。
- 逐日值**不插值**：站点落在哪个格点就取哪个格点，边界上不做平滑。
- 缺测（源文件里就是缺测）如实写 `null`，不拿邻格补——补出来的"真值"会一路走到预警里。

依赖：`xarray` + `h5netcdf`（读 NetCDF-4/HDF5 与 **CF 时间约定**），装在 **`[data]` extra** 里——
**不进生产镜像**（生产镜像不需要读原件，平台只读派生出来的 CSV）。没装时报错要说清怎么装。

**为什么从裸 h5py 换成 xarray**：裸 h5py 只能按"自 1970-01-01 起的天数"猜时间轴，
而 NetCDF 的时间约定由 `units` 属性决定（CHM_PRE 是 `days since 1960-01-01`）——
猜错就是整表日期偏移十年。xarray 的 CF 解码按 `units` 属性解，`calendar` 也一并处理；
最近格点改 `numpy.argmin`（与手写扫描等价，但更快、更不易写错）。
对**没有 `units` 属性**的文件保留旧行为（按 1970 纪元天数），保证历史产物可复现。

用法：
    python scripts/derive_precip_station_daily.py --source backend/data/precip/chm_pre_v2/daily
    python scripts/derive_precip_station_daily.py --source ... --stations stations.csv --out station_daily.csv
"""

from __future__ import annotations

import argparse
import csv
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

#: 缺测在输出里写这个（不是 0——"没测到"与"测到是 0"是两件事）。
MISSING = ""

#: 经纬度坐标轴的候选名（源文件结构随版本变过，按名字候选找，找不到就报错）。
_LAT_NAMES = ("latY", "lat", "latitude", "y")
_LON_NAMES = ("lonX", "lon", "longitude", "x")


@dataclass(frozen=True, slots=True)
class Station:
    """一个站点：取它所在格点的格点值。"""

    station_id: str
    region_code: str
    latitude: float
    longitude: float


def _require_xarray():
    """导入 xarray；没装时给一句能照做的提示。"""
    try:
        import xarray as xr
    except ImportError as exc:  # pragma: no cover - 环境缺依赖时的提示路径
        raise SystemExit(
            "读 NetCDF 需要 xarray + h5netcdf。请装可选 extra：\n"
            "    cd backend && uv sync --extra dev --extra data\n"
            "（xarray/h5netcdf 只用于离线派生，不进生产镜像）"
        ) from exc
    return xr


def _require_numpy():
    try:
        import numpy as np
    except ImportError as exc:  # pragma: no cover - 环境缺依赖时的提示路径
        raise SystemExit("派生需要 numpy。请装可选 extra：uv sync --extra dev --extra data") from exc
    return np


def load_stations(path: Path) -> list[Station]:
    """读站点清单 CSV（`station_id, region_code, latitude, longitude`）。"""
    stations: list[Station] = []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            station_id = (row.get("station_id") or "").strip()
            if not station_id:
                continue
            region_code = (row.get("region_code") or "").strip().upper()
            try:
                latitude = float(row["latitude"])
                longitude = float(row["longitude"])
            except (KeyError, TypeError, ValueError) as exc:
                raise SystemExit(f"站点 {station_id!r} 的经纬度读不出来：{row}") from exc
            stations.append(
                Station(
                    station_id=station_id,
                    region_code=region_code,
                    latitude=latitude,
                    longitude=longitude,
                )
            )
    if not stations:
        raise SystemExit(f"{path} 里一个站点都没有——站点清单要先有真实经纬度")
    return stations


def _find_precip_variable(ds) -> str:
    """找出降水变量：优先 `units` 含 mm/day 的，其次名字像 prec/precip 的。

    源文件结构随版本变过，硬编码变量名会静默读到别的东西——所以按**物理量纲**找，
    找不到就说出来（不猜）。只认 ndim>=2 的变量（1 维的坐标轴不会被当成降水场）。
    """
    candidates: list[str] = []
    for name, array in ds.data_vars.items():
        if getattr(array, "ndim", 0) < 2:
            continue
        units = str(array.attrs.get("units", "")).lower().replace(" ", "")
        if units in {"mm/day", "mmd-1"} or units.startswith("mm/"):
            candidates.append(name)
    if candidates:
        return sorted(candidates, key=len)[0]
    for name, array in ds.data_vars.items():
        if getattr(array, "ndim", 0) < 2:
            continue
        if "prec" in name.lower():
            return name
    raise SystemExit(
        "源文件里找不到降水变量（既没有 units=mm/day 的变量，也没有名字含 prec 的）。"
        "把源文件结构核对一遍再改这里——宁可报错，也不要猜一个变量名读出别的量。"
    )


def _coord_name(ds, names: Sequence[str]) -> str | None:
    """按候选名找 1 维坐标轴；找不到返回 None。"""
    for name in names:
        if name in ds.variables and getattr(ds[name], "ndim", 0) == 1:
            return name
    return None


def _nearest_index(values, target: float) -> int:
    """最近值下标（不做插值）。"""
    np = _require_numpy()
    return int(np.abs(values - target).argmin())


def _date_decoder(ds):
    """构造 `step -> 日期字符串` 的解码器。

    - `time` 已被 CF 解码（dtype 是 datetime64）：直接取日期部分；
    - `time` 没有 `units` 属性（CF 解不了）：**保留旧行为**，按自 1970-01-01 起的天数算——
      历史产物可复现，且不会把"没声明单位"的文件解成错日期；
    - 没有 `time` 变量：返回 None（该列不写）。
    """
    np = _require_numpy()
    if "time" not in ds.variables:
        return lambda _step: None
    time = ds["time"]
    if np.issubdtype(time.dtype, np.datetime64):
        values = time.values

        def decoded(step: int) -> str | None:
            return str(values[step])[:10] if step < len(values) else None

        return decoded

    raw_values = np.asarray(time.values, dtype=float)
    base = datetime(1970, 1, 1, tzinfo=UTC)

    def legacy(step: int) -> str | None:
        if step >= len(raw_values):
            return None
        try:
            return (base + timedelta(days=float(raw_values[step]))).date().isoformat()
        except (OverflowError, ValueError):
            return None

    return legacy


def derive_from_file(path: Path, stations: Iterable[Station], out_rows: list[list[str]]) -> int:
    """从一个年度 NetCDF 派生这些站点这一年的逐日降水量。"""
    xr = _require_xarray()
    np = _require_numpy()
    with xr.open_dataset(path) as ds:
        precip_name = _find_precip_variable(ds)
        precip = ds[precip_name]
        lat_name = _coord_name(ds, _LAT_NAMES)
        lon_name = _coord_name(ds, _LON_NAMES)
        if lat_name is None or lon_name is None:
            raise SystemExit("源文件里找不到经纬度坐标轴（候选：latY/lat、lonX/lon）")
        lat_values = np.asarray(ds[lat_name].values, dtype=float)
        lon_values = np.asarray(ds[lon_name].values, dtype=float)

        columns = []
        for station in stations:
            columns.append((station, _nearest_index(lat_values, station.latitude), _nearest_index(lon_values, station.longitude)))

        # 逐日写出：源文件的第一个轴是时间。
        steps = int(precip.shape[0])
        # 一次性把场读进内存再按位置索引：逐点 `isel` 会每次走 xarray 的索引开销，
        # 而年度文件（365×lat×lon f32）本来就在内存量级。
        field = np.asarray(precip.values)
        decode_date = _date_decoder(ds)
        for step in range(steps):
            date = decode_date(step)
            if date is None:
                continue
            for station, lat_index, lon_index in columns:
                value = float(field[step, lat_index, lon_index])
                out_rows.append(
                    [
                        date,
                        station.station_id,
                        station.region_code,
                        f"{station.latitude:.6f}",
                        f"{station.longitude:.6f}",
                        MISSING if value != value else f"{value:.2f}",  # NaN → 缺测（不补值）
                    ]
                )
    return steps


def write_csv(rows: list[list[str]], out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(["date", "station_id", "region_code", "latitude", "longitude", "precip_mm"])
        writer.writerows(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CHM_PRE V2 → 站点逐日降水量")
    parser.add_argument("--source", type=Path, required=True, help="取件目录（其下的 *.nc 会被逐个读）")
    parser.add_argument("--stations", type=Path, required=True, help="站点清单 CSV（station_id, region_code, latitude, longitude）")
    parser.add_argument("--out", type=Path, required=True, help="输出 CSV")
    args = parser.parse_args(argv)

    stations = load_stations(args.stations)
    files = sorted(args.source.glob("*.nc"))
    if not files:
        raise SystemExit(f"{args.source} 里没有 .nc——先跑 scripts/fetch_precip_chmpre.py 取件")

    rows: list[list[str]] = []
    for path in files:
        steps = derive_from_file(path, stations, rows)
        print(f"{path.name}: {steps} 天 × {len(stations)} 站", flush=True)
    write_csv(rows, args.out)
    print(f"写出 {len(rows)} 行 → {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
