"""公开气象数据源适配器（httpx 异步、可注入客户端以便单测打桩）。

真实接口因地区与授权而异，故本适配器只约定输入形状（见 _EXPECTED_SHAPE 注释），
不可用时返回空集合而不抛错——弱网/无凭据场景下摄取轮次必须继续。
"""

from __future__ import annotations

import logging
from typing import Any

from aegis.connectors.base import DataSource
from aegis.connectors.metrics import unit_for
from aegis.domain.messages import TelemetryReading, now_iso, utc_now

log = logging.getLogger("aegis.connectors.weather")

# 期望响应形状：
# {"stations":[{"id":"RG54010101","region_code":"540101","rain_10min":12.5,
#   "rain_cumulative_24h":60.0,"debris_level":null,...}]}
_METRIC_FIELDS = (
    "rain_10min",
    "rain_cumulative_24h",
    "debris_level",
    "displacement_mm",
    "lake_level_m",
    "dam_seepage_turbidity_ntu",
    "new_snow_cm",
    "wind_speed_ms",
    "air_temperature_c",
    "snow_water_equivalent_mm",
    "crack_aperture_mm",
    "freeze_thaw_cycles",
)


class WeatherApiSource(DataSource):
    name = "weather_api"

    def __init__(
        self,
        base_url: str,
        client: Any = None,
        *,
        path: str = "/observation",
        timeout_ms: int = 4_000,
    ) -> None:
        if base_url and client is None:
            raise ValueError("注入 httpx 客户端后才能启用 HTTP 采集")
        self._base_url = base_url.rstrip("/")
        self._client = client
        self._path = path
        self._timeout_s = timeout_ms / 1000

    @property
    def enabled(self) -> bool:
        return bool(self._base_url and self._client is not None)

    async def collect(self) -> list[TelemetryReading]:
        if not self.enabled:
            return []
        response = await self._client.get(f"{self._base_url}{self._path}", timeout=self._timeout_s)
        response.raise_for_status()
        body = response.json()
        return self._parse(body)

    def _parse(self, body: dict[str, Any]) -> list[TelemetryReading]:
        observed = _extract_observed(body)
        readings: list[TelemetryReading] = []
        for station in body.get("stations", []):
            if not isinstance(station, dict):
                continue
            station_id = str(station.get("id", "")).strip()
            region_code = str(station.get("region_code", "")).strip().upper()
            if not station_id or len(region_code) < 6:
                log.debug("跳过不完整站点记录", extra={"station": station_id or "<missing>"})
                continue
            for metric in _METRIC_FIELDS:
                value = station.get(metric)
                if value is None:
                    continue
                try:
                    numeric = float(value)
                except (TypeError, ValueError):
                    continue
                readings.append(
                    TelemetryReading(
                        station_id=station_id,
                        metric=metric,
                        value=numeric,
                        unit=unit_for(metric),
                        region_code=region_code[:24],
                        observed_at=observed,
                        ingested_at=now_iso(),
                        source=self.name,
                        quality_flag="ok" if numeric >= 0 or metric == "air_temperature_c" else "suspect",
                    )
                )
        return readings


def _extract_observed(body: dict[str, Any]) -> str:
    raw = body.get("observed_at")
    if isinstance(raw, str):
        try:
            from aegis.domain.messages import parse_iso

            return parse_iso(raw).isoformat().replace("+00:00", "Z")
        except ValueError:
            pass
    return utc_now().isoformat(timespec="milliseconds").replace("+00:00", "Z")
