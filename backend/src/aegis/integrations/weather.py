"""公开气象/水文拉取腿：只在填了 base_url 时才存在这条腿；base_url 可能带 token，状态面只留 host[:port]。（P2 纯搬运：函数体逐字未改。）"""

from __future__ import annotations

from typing import Any

from aegis.config import Settings, get_settings
from aegis.connectors.weather_api import WeatherApiSource
from aegis.integrations.shared import target_of
from aegis.integrations.state import IntegrationState


def build_weather(
    settings: Settings | None = None,
    *,
    client: Any = None,
) -> tuple[WeatherApiSource | None, IntegrationState]:
    """装配公开气象/水文拉取腿：只在填了 base_url 时才存在这条腿。

    与 MQTT 那条 push 腿互补：站端不上报、只有公开接口可查的区域靠这条腿补数。
    `client` 参数是给测试注入 httpx.MockTransport 用的；生产不传，连接器在第一次采集时
    才建连接池。采集失败按源隔离进 `IngestReport.sources_failed`，这里只报"这条腿在不在、
    采了几轮、失败几次"。base_url 可能带着运营方 token，状态面只留 host[:port]。
    """
    cfg = settings or get_settings()
    if not cfg.weather_api_base_url:
        return None, IntegrationState(name="weather", enabled=False, driver="off")

    source = WeatherApiSource(
        cfg.weather_api_base_url,
        client,
        path=cfg.weather_api_path,
        timeout_ms=cfg.weather_api_timeout_ms,
    )
    return source, IntegrationState(
        name="weather",
        enabled=True,
        driver="http",
        detail={
            "target": target_of(cfg.weather_api_base_url),
            "path": source.path,
            "timeout_ms": cfg.weather_api_timeout_ms,
        },
    )
