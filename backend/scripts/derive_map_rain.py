"""Extract native CHM_PRE cells around Gyirong for the map's daily replay."""

from __future__ import annotations

import argparse
import json
import math
from itertools import pairwise
from pathlib import Path

from derive_precip_station_daily import (
    _LAT_NAMES,
    _LON_NAMES,
    _coord_name,
    _date_decoder,
    _find_precip_variable,
    _require_xarray,
)

DEFAULT_DATA = Path(__file__).resolve().parents[1] / "data" / "precip"


def extract(path: Path) -> dict:
    xr = _require_xarray()
    with xr.open_dataset(path) as ds:
        lat_name = _coord_name(ds, _LAT_NAMES)
        lon_name = _coord_name(ds, _LON_NAMES)
        if lat_name is None or lon_name is None:
            raise ValueError("源文件缺少经纬度轴")
        field = ds[_find_precip_variable(ds)].sortby([lat_name, lon_name])
        field = field.sel({lat_name: slice(27.9, 28.7), lon_name: slice(84.9, 85.9)})
        field = field.transpose("time", lat_name, lon_name)
        lats = [round(float(value), 4) for value in field[lat_name].values]
        lons = [round(float(value), 4) for value in field[lon_name].values]
        if len(lats) < 2 or len(lons) < 2:
            raise ValueError("源文件未覆盖吉隆窗口")
        step_lat = round(lats[1] - lats[0], 4)
        step_lon = round(lons[1] - lons[0], 4)
        for axis, spacing in ((lats, step_lat), (lons, step_lon)):
            if spacing <= 0 or any(abs(b - a - spacing) > 0.0002 for a, b in pairwise(axis)):
                raise ValueError("源文件网格轴间隔不一致")

        decode = _date_decoder(ds)
        frames = []
        for index, array in enumerate(field.values):
            end = decode(index)
            if end is None:
                raise ValueError("源文件缺少有效时间轴")
            values = [round(val, 2) if math.isfinite(val) and val >= 0 else None for val in array.flatten().tolist()]
            peak = max((val for val in values if val is not None), default=None)
            frames.append({"end": end, "values": values, "peak": peak})

    if not any(frame["peak"] is not None for frame in frames):
        raise ValueError("吉隆窗口内没有有效降水格点")
    return {
        "source": "CHM_PRE V2",
        "file": path.name,
        "grid": {"lats": lats, "lons": lons, "step_lat": step_lat, "step_lon": step_lon},
        "frames": frames,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_DATA / "chm_pre_v2" / "daily")
    parser.add_argument("--out", type=Path, default=DEFAULT_DATA / "map_rain")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    written = 0
    for path in sorted(args.source.glob("CHM_PRE_V2_daily_*.nc")):
        try:
            archive = extract(path)
        except (OSError, ValueError) as exc:
            print(f"跳过 {path.name}：{exc}")
            continue
        target = args.out / (path.stem.rsplit("_", 1)[1] + ".json")
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(archive, separators=(",", ":"), allow_nan=False), encoding="utf-8")
        temporary.replace(target)
        written += 1
        print(f"{target.name}: {len(archive['frames'])} 个日雨量帧")
    if not written:
        parser.exit(1, "没有可用的吉隆降水网格。\n")


if __name__ == "__main__":
    main()
