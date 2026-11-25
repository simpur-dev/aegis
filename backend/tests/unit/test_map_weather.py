from datetime import UTC, datetime, timedelta

import httpx
import pytest

from aegis.config import Settings
from aegis.services import map_weather


def test_current_weather_preserves_missing_rain_and_only_future_hours():
    now = datetime(2026, 11, 25, 6, 30, tzinfo=UTC)
    body = {
        "current": {"time": "2026-11-25T06:15", "interval": 900, "rain": None, "showers": 0, "weather_code": 3},
        "hourly": {"time": ["2026-11-25T06:00", "2026-11-25T07:00", "2026-11-25T08:00"], "rain": [7, 1.1, 0], "showers": [0, 0.4, 0]},
    }
    point = map_weather.parse_weather_point("test", body, now)
    assert point is not None
    assert point.valid_at.tzinfo is UTC
    assert point.rain_mm is None
    assert [hour.rain_mm for hour in point.forecast] == [1.5, 0]
    assert map_weather.parse_weather_point("test", body, now - timedelta(hours=3)) is None
    assert map_weather.parse_weather_point("test", body, now + timedelta(hours=3)) is None


@pytest.mark.asyncio
async def test_model_failure_keeps_original_time_as_stale(monkeypatch):
    service = map_weather.MapWeather()
    clock = [1.0]
    monkeypatch.setattr(map_weather, "monotonic", lambda: clock[0])
    now = datetime.now(UTC)
    point = map_weather.parse_weather_point("test", {"current": {"time": now.isoformat(), "interval": 900}}, now)

    async def fetch():
        if clock[0] > 300:
            raise httpx.ConnectError("offline")
        return [point]

    monkeypatch.setattr(service, "_fetch", fetch)
    assert (await service.current(enabled=False)).status == "disabled"
    result = await service.current(enabled=True)
    assert result.status == "available"
    clock[0] = 310
    stale = await service.current(enabled=True)
    assert stale.status == "stale"
    assert stale.fetched_at == result.fetched_at
    assert stale.points == result.points


def test_hourly_archive_keeps_dry_zero_distinct_from_missing_hour(tmp_path):
    (tmp_path / "stations.csv").write_text(
        "station_id,latitude,longitude,name_zh\nP1,28.3,87.7,定结县县域降水代理点\n", encoding="utf-8"
    )
    readings = tmp_path / "hourly.csv"
    readings.write_text(
        "datetime_utc,station_id,precip_mm_h\n2026-11-20T01:00Z,P1,0\n2026-11-20T03:00Z,P1,2.5\n", encoding="utf-8"
    )
    settings = Settings(_env_file=None, weather_map_data_dir=str(tmp_path), precip_hourly_file=str(readings))
    replay = map_weather.rain_hourly(settings)
    assert replay.points[0].name == "定结县"
    assert [frame.values for frame in replay.frames] == [[0], [None], [2.5]]
