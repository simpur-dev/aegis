"""Read-only weather layers. Model and archive data never enter telemetry ingestion."""

from __future__ import annotations

import asyncio
import csv
import math
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from time import monotonic
from typing import Any, Literal

import httpx
from pydantic import BaseModel

from aegis.config import Settings
from aegis.connectors.precip_hourly import MAX_HOURLY_MM, _parse_utc

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
GYIRONG_POINTS = (
    ("history-gyirong-port", 85.3778, 28.2797),
    ("history-gyirong-upstream", 85.515, 28.271),
)


class ForecastHour(BaseModel):
    end: datetime
    rain_mm: float | None


class WeatherPoint(BaseModel):
    id: str
    valid_at: datetime
    interval_minutes: int
    temperature_c: float | None
    wind_kmh: float | None
    rain_mm: float | None
    weather_code: int | None
    is_day: bool | None
    forecast: list[ForecastHour]


class CurrentMapWeather(BaseModel):
    status: Literal["available", "stale", "unavailable", "disabled"]
    fetched_at: datetime | None = None
    points: list[WeatherPoint] = []


class RainPoint(BaseModel):
    id: str
    name: str
    lon: float
    lat: float


class RainGrid(BaseModel):
    lats: list[float]
    lons: list[float]
    step_lat: float
    step_lon: float


class RainFrame(BaseModel):
    end: str
    values: list[float | None]
    peak: float | None


class RainArchive(BaseModel):
    source: str
    file: str
    grid: RainGrid | None = None
    points: list[RainPoint] = []
    frames: list[RainFrame]


class RainCatalog(BaseModel):
    daily_years: list[int]
    hourly_available: bool


def _number(raw: Any, *, minimum: float = 0, maximum: float = math.inf) -> float | None:
    if isinstance(raw, bool):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and minimum <= value <= maximum else None


def _rain(rain: Any, showers: Any) -> float | None:
    first, second = _number(rain), _number(showers)
    return round(first + second, 2) if first is not None and second is not None else None


def parse_weather_point(point_id: str, payload: dict, now: datetime) -> WeatherPoint | None:
    current = payload.get("current") or {}
    valid_at = _parse_utc(str(current.get("time", "")))
    interval = _number(current.get("interval"), minimum=60, maximum=3600)
    if valid_at is None or interval is None or not now - timedelta(minutes=90) <= valid_at <= now + timedelta(minutes=15):
        return None
    code = _number(current.get("weather_code"), maximum=99)
    daylight = _number(current.get("is_day"), maximum=1)
    hourly = payload.get("hourly") or {}
    forecast = []
    for time, rain, showers in zip(hourly.get("time") or [], hourly.get("rain") or [], hourly.get("showers") or [], strict=False):
        end = _parse_utc(str(time))
        if end is not None and valid_at < end <= valid_at + timedelta(hours=6):
            forecast.append(ForecastHour(end=end, rain_mm=_rain(rain, showers)))
    return WeatherPoint(
        id=point_id,
        valid_at=valid_at,
        interval_minutes=int(interval / 60),
        temperature_c=_number(current.get("temperature_2m"), minimum=-100, maximum=65),
        wind_kmh=_number(current.get("wind_speed_10m"), maximum=400),
        rain_mm=_rain(current.get("rain"), current.get("showers")),
        weather_code=int(code) if code is not None else None,
        is_day=bool(daylight) if daylight is not None else None,
        forecast=sorted(forecast, key=lambda hour: hour.end),
    )


class MapWeather:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._expires = 0.0
        self._current = CurrentMapWeather(status="unavailable")

    async def current(self, *, enabled: bool) -> CurrentMapWeather:
        if not enabled:
            return CurrentMapWeather(status="disabled")
        async with self._lock:
            if monotonic() < self._expires:
                return self._current
            try:
                points = await self._fetch()
                if not points:
                    raise ValueError("no current weather time in the response")
            except (httpx.HTTPError, ValueError, TypeError, AttributeError):
                # A stale value keeps its original time and is never plotted as current.
                status = "stale" if self._current.points else "unavailable"
                self._current = self._current.model_copy(update={"status": status})
                self._expires = monotonic() + 60
            else:
                self._current = CurrentMapWeather(status="available", fetched_at=datetime.now(UTC), points=points)
                self._expires = monotonic() + 300
            return self._current

    async def _fetch(self) -> list[WeatherPoint]:
        async with httpx.AsyncClient(timeout=6) as client:
            response = await client.get(FORECAST_URL, params={
                "latitude": ",".join(str(lat) for _, _, lat in GYIRONG_POINTS),
                "longitude": ",".join(str(lon) for _, lon, _ in GYIRONG_POINTS),
                "current": "temperature_2m,weather_code,is_day,rain,showers,wind_speed_10m",
                "hourly": "rain,showers",
                "forecast_hours": "7",
                "cell_selection": "nearest",
                "timezone": "GMT",
            })
            response.raise_for_status()
            payload = response.json()
        if not isinstance(payload, list) or len(payload) != len(GYIRONG_POINTS):
            return []
        now = datetime.now(UTC)
        return [point for (point_id, _, _), row in zip(GYIRONG_POINTS, payload, strict=True)
                if isinstance(row, dict) and (point := parse_weather_point(point_id, row, now)) is not None]


def _data_root(settings: Settings) -> Path:
    if settings.weather_map_data_dir:
        return Path(settings.weather_map_data_dir)
    local = Path(__file__).resolve().parents[3] / "data" / "precip"
    return local if local.is_dir() else Path("data/precip")


def _hourly_paths(settings: Settings) -> tuple[Path, Path]:
    root = _data_root(settings)
    hourly = Path(settings.precip_hourly_file) if settings.precip_hourly_file else root / "openmeteo/openmeteo_hourly.csv"
    return root / "stations.csv", hourly


def rain_catalog(settings: Settings) -> RainCatalog:
    years = [int(p.stem) for p in (_data_root(settings) / "map_rain").glob("[0-9][0-9][0-9][0-9].json") if p.is_file()]
    return RainCatalog(daily_years=sorted(years, reverse=True), hourly_available=all(p.is_file() for p in _hourly_paths(settings)))


@lru_cache(maxsize=3)
def _read_daily(path: Path, mtime: int) -> RainArchive:
    return RainArchive.model_validate_json(path.read_bytes())


def rain_daily(settings: Settings, year: int) -> RainArchive:
    path = _data_root(settings) / "map_rain" / f"{year}.json"
    return _read_daily(path, path.stat().st_mtime_ns)


@lru_cache(maxsize=2)
def _read_hourly(stations: Path, hourly: Path, stations_mtime: int, hourly_mtime: int) -> RainArchive:
    points = {}
    with stations.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            lon = _number(row.get("longitude"), minimum=-180, maximum=180)
            lat = _number(row.get("latitude"), minimum=-90, maximum=90)
            key = row.get("station_id")
            if key and lon is not None and lat is not None:
                points[key] = RainPoint(id=key, name=(row.get("name_zh") or key).replace("县域降水代理点", ""), lon=lon, lat=lat)

    with hourly.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not {"datetime_utc", "station_id", "precip_mm_h"}.issubset(reader.fieldnames or []):
            raise ValueError("小时降水文件缺少必要列")
        series: dict[datetime, dict[str, float | None]] = {}
        for row in reader:
            end = _parse_utc(row.get("datetime_utc") or "")
            station = row.get("station_id") or ""
            if end is not None and station in points:
                series.setdefault(end.astimezone(UTC), {})[station] = _number(row.get("precip_mm_h"), maximum=MAX_HOURLY_MM)

    frames = []
    if series:
        latest = max(series)
        earliest = max(min(series), latest - timedelta(hours=167))
        end = earliest
        while end <= latest:
            values = [series.get(end, {}).get(key) for key in points]
            frames.append(RainFrame(end=end.isoformat(), values=values, peak=max((v for v in values if v is not None), default=None)))
            end += timedelta(hours=1)
    return RainArchive(source="Open-Meteo Historical Weather", file=hourly.name, points=list(points.values()), frames=frames)


def rain_hourly(settings: Settings) -> RainArchive:
    stations, hourly = _hourly_paths(settings)
    return _read_hourly(stations, hourly, stations.stat().st_mtime_ns, hourly.stat().st_mtime_ns)
