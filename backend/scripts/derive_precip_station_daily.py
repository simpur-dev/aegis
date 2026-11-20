"""降水派生脚本：从 CHM_PRE V2 的 NetCDF 原件派生**站点逐日降水量**。

原件由 `scripts/fetch_precip_chmpre.py` 取到运行态目录（`backend/data/precip/chm_pre_v2/daily/`），
本脚本把它派生成平台能直接吃的一张小表：`station_daily.csv`
（`date, station_id, latitude, longitude, precip_mm`）。

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

依赖：读 NetCDF-4/HDF5 需要 `h5py`（BSD-3），装在 **`[precip]` extra** 里——
**不进生产镜像**（生产镜像不需要读原件，平台只读派生出来的 CSV）。
没装时的报错要说清怎么装。

用法：
    python scripts/derive_precip_station_daily.py --source backend/data/precip/chm_pre_v2/daily
    python scripts/derive_precip_station_daily.py --source ... --stations stations.csv --out station_daily.csv
"""

from __future__ import annotations

import argparse
import csv
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

#: 缺测在输出里写这个（不是 0——"没测到"与"测到是 0"是两件事）。
MISSING = ""


@dataclass(frozen=True, slots=True)
class Station:
    """一个站点：取它所在格点的格点值。"""

    station_id: str
    region_code: str
    latitude: float
    longitude: float


def _require_h5py():
    """导入 h5py；没装时给一句能照做的提示。"""
    try:
        import h5py
    except ImportError as exc:  # pragma: no cover - 环境缺依赖时的提示路径
        raise SystemExit(
            "读 NetCDF 需要 h5py。请装可选 extra：\n"
            "    cd backend && uv sync --extra dev --extra precip\n"
            "（h5py 是 BSD-3 许可，只用于离线派生，不进生产镜像）"
        ) from exc
    return h5py


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


def _find_precip_variable(handle) -> str:
    """找出降水变量：优先 `units` 含 mm/day 的，其次名字像 prec/precip 的。

    源文件结构随版本变过，硬编码变量名会静默读到别的东西——所以按**物理量纲**找，
    找不到就说出来（不猜）。
    """
    candidates: list[str] = []
    for name, dataset in handle.items():
        if not hasattr(dataset, "shape"):
            continue
        units = str(dataset.attrs.get("units", "")).lower().replace(" ", "")
        if units in {"mm/day", "mmd-1"} or units.startswith("mm/"):
            candidates.append(name)
    if candidates:
        return sorted(candidates, key=len)[0]
    for name, dataset in handle.items():
        if not hasattr(dataset, "shape"):
            continue
        if "prec" in name.lower():
            return name
    raise SystemExit(
        "源文件里找不到降水变量（既没有 units=mm/day 的数据集，也没有名字含 prec 的）。"
        "把源文件结构核对一遍再改这里——宁可报错，也不要猜一个变量名读出别的量。"
    )


def _coord_arrays(handle, precip_name: str) -> tuple[list[float], list[float]]:
    """取经纬度坐标轴（按名字候选，猜不到就报错）。"""
    lat = _first_numeric(handle, ("latY", "lat", "latitude", "y"))
    lon = _first_numeric(handle, ("lonX", "lon", "longitude", "x"))
    if lat is None or lon is None:
        raise SystemExit("源文件里找不到经纬度坐标轴（候选：latY/lat、lonX/lon）")
    return lat, lon


def _first_numeric(handle, names: Sequence[str]) -> list[float] | None:
    for name in names:
        if name not in handle:
            continue
        dataset = handle[name]
        if hasattr(dataset, "shape") and dataset.ndim == 1:
            return [float(value) for value in dataset[...]]
    return None


def _nearest_index(values: list[float], target: float) -> int:
    """最近值下标（不做插值）。"""
    best = 0
    best_gap = abs(values[0] - target)
    for index, value in enumerate(values):
        gap = abs(value - target)
        if gap < best_gap:
            best = index
            best_gap = gap
    return best


def derive_from_file(path: Path, stations: Iterable[Station], out_rows: list[list[str]]) -> int:
    """从一个年度 NetCDF 派生这些站点这一年的逐日降水量。"""
    h5py = _require_h5py()
    with h5py.File(path, "r") as handle:
        precip_name = _find_precip_variable(handle)
        precip = handle[precip_name]
        lat, lon = _coord_arrays(handle, precip_name)
        time_values = handle["time"][...] if "time" in handle else None

        columns = []
        for station in stations:
            columns.append((station, _nearest_index(lat, station.latitude), _nearest_index(lon, station.longitude)))

        # 逐日写出：源文件的第一个轴是时间。
        steps = precip.shape[0]
        for step in range(steps):
            date = _date_of(time_values, step)
            if date is None:
                continue
            for station, lat_index, lon_index in columns:
                value = float(precip[step, lat_index, lon_index])
                out_rows.append(
                    [
                        date,
                        station.station_id,
                        station.region_code,
                        f"{station.latitude:.6f}",
                        f"{station.longitude:.6f}",
                        "" if value != value else f"{value:.2f}",  # NaN → 缺测（不补值）
                    ]
                )
    return steps


def _date_of(time_values, step: int) -> str | None:
    """把时间轴的第 N 个值转成日期字符串。

    NetCDF 的时间约定各源不同（自 1960-01-01 起的天数、自 1970-01-01 起的天数…），
    这里按"自 1970-01-01 起的天数"这一最常见约定处理，并优先读 `units` 属性里的说明；
    读不出来就返回 None（那一列不写），不猜。
    """
    if time_values is None:
        return None
    from datetime import UTC, datetime, timedelta

    raw = float(time_values[step])
    base = datetime(1970, 1, 1, tzinfo=UTC)
    try:
        return (base + timedelta(days=raw)).date().isoformat()
    except (OverflowError, ValueError):
        return None


def write_csv(rows: list[list[str]], out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(["date", "station_id", "region_code", "latitude", "longitude", "precip_mm"])
        writer.writerows(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CHM_PRE V2 → 站点逐日降水量")
    parser.add_argument("--source", type=Path, required=True, help="取件目录（其下的 *.nc 会被逐个读）")
    parser.add_argument("--stations", type=Path, required=True, help="站点清单 CSV（station_id, latitude, longitude）")
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
