"""公开气象数据源适配器（httpx 异步、可注入客户端以便单测打桩）。

真实接口因地区与授权而异，故本适配器只约定输入形状（见 `_METRIC_FIELDS` 上方注释）。
采集失败**向上抛出**：摄取服务按源隔离失败并计入 `IngestReport.sources_failed`，
在这里吞掉错误只会让"外部 API 挂了"变成"平台这边静默少了一批数"——那正是查不出来的那种故障。
未注入客户端时，第一次采集才创建 httpx 客户端：容器构造阶段不建连接池，
`aclose()` 也只关自己建的那个（注入进来的客户端归注入方所有）。
"""

from __future__ import annotations

import logging
from typing import Any

from aegis.connectors.base import DataSource
from aegis.connectors.metrics import unit_for
from aegis.domain.messages import TelemetryReading, now_iso, parse_iso, utc_now
from aegis.errors import SchemaInvalidError

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


DEFAULT_PATH = "/observation"
DEFAULT_TIMEOUT_MS = 4_000


class WeatherApiSource(DataSource):
    name = "weather_api"

    def __init__(
        self,
        base_url: str,
        client: Any = None,
        *,
        path: str = DEFAULT_PATH,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = client
        # 注入进来的客户端归注入方所有：本类只关自己建的那个，绝不替别人 aclose。
        self._owns_client = client is None
        self._path = path if path.startswith("/") else f"/{path}"
        self._timeout_s = timeout_ms / 1000
        self.rounds = 0
        self.readings = 0
        self.failures = 0
        self.last_error: str | None = None

    @property
    def enabled(self) -> bool:
        return bool(self._base_url)

    @property
    def path(self) -> str:
        """采集路径：装配层要把它写进状态行，运维据此核对端点是否填对。"""
        return self._path

    async def collect(self) -> list[TelemetryReading]:
        if not self.enabled:
            return []
        client = self._client if self._client is not None else self._new_client()
        url = f"{self._base_url}{self._path}"
        try:
            response = await client.get(url, timeout=self._timeout_s)
            response.raise_for_status()
            readings = self._parse(response.json())
        except Exception as exc:
            # 计数与上抛同时发生：吞掉错误会让"外部 API 挂了"变成"平台静默少一批数"
            self.failures += 1
            self.last_error = _error_note(exc, url)
            raise
        self.rounds += 1
        self.readings += len(readings)
        return readings

    def status(self) -> dict[str, Any]:
        """给装配层并入状态行的事实：只看得到"这轮真的采到东西没有"，看不到端点与凭据。"""
        return {
            "rounds": self.rounds,
            "readings": self.readings,
            "failures": self.failures,
            "last_error": self.last_error,
            "timeout_ms": int(self._timeout_s * 1_000),
        }

    async def aclose(self) -> None:
        if not self._owns_client or self._client is None:
            return
        client, self._client = self._client, None
        await client.aclose()

    def _new_client(self) -> Any:
        self._client = self._build_client()
        return self._client

    def _build_client(self) -> Any:
        """连接池构造点：测试通过覆盖它换掉真实网络，而不是去改私有字段。"""
        import httpx

        return httpx.AsyncClient(headers={"accept": "application/json"})

    def _parse(self, body: Any) -> list[TelemetryReading]:
        """形状不符约定就抛错，而不是当成"本轮没有数据"：上游改了字段必须看得见。"""
        if not isinstance(body, dict):
            raise SchemaInvalidError("气象接口响应不是 JSON 对象", detail={"type": type(body).__name__})
        stations = body.get("stations")
        if not isinstance(stations, list):
            raise SchemaInvalidError(
                "气象接口响应缺少 stations 数组",
                detail={"keys": sorted(str(key) for key in body)[:8]},
            )
        observed = _extract_observed(body)
        readings: list[TelemetryReading] = []
        for station in stations:
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
            return parse_iso(raw).isoformat().replace("+00:00", "Z")
        except ValueError:
            pass
    return utc_now().isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _error_note(exc: BaseException, url: str) -> str:
    """错误摘要：把完整请求 URL 换成占位符。

    端点由运营方填写，可能带着 token 或 Basic 凭据，而 httpx 的异常文本会把 URL 原样带出来；
    这条摘要会进 `/api/v1/integrations`，所以先抹掉 URL 再入账。
    """
    text = str(exc).replace(url, "<endpoint>")
    return f"{type(exc).__name__}: {text}"[:240]


__all__ = ["WeatherApiSource"]
