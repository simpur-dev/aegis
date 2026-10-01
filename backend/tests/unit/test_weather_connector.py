"""公开气象/水文拉取腿：响应形状、客户端归属与失败面。

两条口径决定了这里的大部分断言：
1. 形状不符约定就抛错，不当成"本轮没有数据"——上游改字段与上游真的没数据必须可区分；
2. 错误上抛给摄取服务按源隔离（`IngestReport.sources_failed`），连接器自己不吞异常，
   但要把"这轮采了几次、多少条、失败几次"记下来，否则状态行只能空喊"已启用"。
凭据可能写在运营方填的 base_url 里，而 httpx 的异常文本会把 URL 原样带出来——
错误摘要进状态面前必须先抹掉 URL。
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from aegis.config import Settings
from aegis.connectors.weather_api import WeatherApiSource
from aegis.container import create_container
from aegis.errors import SchemaInvalidError
from aegis.integrations import build_weather

BASE = "https://weather.example.internal"


def observation_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "observed_at": "2026-09-30T04:20:00+00:00",
        "stations": [
            {"id": "RG-540121-01", "region_code": "540121", "rain_10min": 18.5, "debris_level": 42.0},
            {"id": "RG-540221-07", "region_code": "540221", "displacement_mm": 12.4, "lake_level_m": None},
        ],
    }
    body.update(overrides)
    return body


class RecordingClient:
    """替身客户端：记录调用与关闭，不做任何网络。"""

    def __init__(self, payload: Any, *, status: int = 200) -> None:
        self.payload = payload
        self.status = status
        self.urls: list[str] = []
        self.closed = 0

    async def get(self, url: str, **_: Any) -> httpx.Response:
        self.urls.append(url)
        # raise_for_status() 要求 response 上挂着 request：手工造响应时得补上
        return httpx.Response(self.status, json=self.payload, request=httpx.Request("GET", url))

    async def aclose(self) -> None:
        self.closed += 1


def mock_client(payload: Any, *, status: int = 200, seen: list[httpx.Request] | None = None) -> httpx.AsyncClient:
    def _handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return httpx.Response(status, json=payload)

    return httpx.AsyncClient(transport=httpx.MockTransport(_handler))


class TestShapeParsing:
    async def test_disabled_without_base_url_collects_nothing(self) -> None:
        source = WeatherApiSource("")
        assert source.enabled is False
        assert await source.collect() == []

    async def test_metrics_become_readings_with_units(self) -> None:
        client = mock_client(observation_body())
        source = WeatherApiSource(BASE, client)
        readings = await source.collect()

        assert [(r.station_id, r.metric, r.value, r.unit) for r in readings] == [
            ("RG-540121-01", "rain_10min", 18.5, "mm"),
            ("RG-540121-01", "debris_level", 42.0, "m"),
            ("RG-540221-07", "displacement_mm", 12.4, "mm"),
        ]
        assert {r.source for r in readings} == {"weather_api"}
        assert {r.region_code for r in readings} == {"540121", "540221"}

    async def test_null_and_unparseable_values_are_skipped_per_metric(self) -> None:
        client = mock_client(
            {
                "stations": [
                    {
                        "id": "RG-540121-02",
                        "region_code": "540121",
                        "rain_10min": None,
                        "debris_level": "n/a",
                        "displacement_mm": "7.5",
                    }
                ]
            }
        )
        readings = await WeatherApiSource(BASE, client).collect()
        assert [(r.metric, r.value) for r in readings] == [("displacement_mm", 7.5)]

    async def test_station_without_id_or_short_region_is_dropped(self) -> None:
        client = mock_client(
            {
                "stations": [
                    {"id": "", "region_code": "540121", "rain_10min": 1.0},
                    {"id": "RG-540121-03", "region_code": "5401", "rain_10min": 2.0},
                    "not-an-object",
                ]
            }
        )
        assert await WeatherApiSource(BASE, client).collect() == []

    async def test_negative_readings_are_suspect_except_temperature(self) -> None:
        client = mock_client(
            {
                "stations": [
                    {
                        "id": "RG-540121-04",
                        "region_code": "540121",
                        "displacement_mm": -3.0,
                        "air_temperature_c": -11.5,
                    }
                ]
            }
        )
        readings = await WeatherApiSource(BASE, client).collect()
        flags = {r.metric: r.quality_flag for r in readings}
        assert flags == {"displacement_mm": "suspect", "air_temperature_c": "ok"}

    async def test_region_code_is_uppercased_and_bounded(self) -> None:
        client = mock_client({"stations": [{"id": "S-1", "region_code": "x540121z" + "0" * 40, "rain_10min": 1.0}]})
        readings = await WeatherApiSource(BASE, client).collect()
        assert readings[0].region_code == "X540121Z" + "0" * 16
        assert len(readings[0].region_code) == 24

    async def test_observed_at_is_normalized_to_utc_z(self) -> None:
        client = mock_client(observation_body())
        readings = await WeatherApiSource(BASE, client).collect()
        assert readings[0].observed_at == "2026-09-30T04:20:00Z"

    async def test_missing_or_bogus_observed_at_falls_back_to_now(self) -> None:
        for body in ({"stations": observation_body()["stations"]}, {**observation_body(), "observed_at": "昨天"}):
            client = mock_client(body)
            readings = await WeatherApiSource(BASE, client).collect()
            assert readings[0].observed_at.endswith("Z")


class TestShapeErrorsAreNotSilence:
    async def test_missing_stations_array_raises(self) -> None:
        client = mock_client({"observed_at": "2026-09-30T04:20:00Z"})
        source = WeatherApiSource(BASE, client)
        with pytest.raises(SchemaInvalidError):
            await source.collect()
        assert source.status()["failures"] == 1

    async def test_non_object_payload_raises(self) -> None:
        client = mock_client([{"id": "S-1"}])
        with pytest.raises(SchemaInvalidError):
            await WeatherApiSource(BASE, client).collect()

    async def test_http_error_status_raises_and_counts(self) -> None:
        client = mock_client({"detail": "quota"}, status=503)
        source = WeatherApiSource(BASE, client)
        with pytest.raises(httpx.HTTPStatusError):
            await source.collect()
        status = source.status()
        assert (status["rounds"], status["readings"], status["failures"]) == (0, 0, 1)
        assert "HTTPStatusError" in str(status["last_error"])


class TestEndpointNeverLeaks:
    async def test_connection_failure_summary_drops_the_url(self) -> None:
        # 运营方可能把凭据填在 base_url 里（Basic 段或 query token），而 httpx 的异常文本会把 URL 原样带出来
        base = "https://user:***@weather.example.internal"

        def _fail(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError(f"连接失败: {request.url}", request=request)

        client = httpx.AsyncClient(transport=httpx.MockTransport(_fail))
        source = WeatherApiSource(base, client)
        with pytest.raises(httpx.ConnectError):
            await source.collect()

        status = json.dumps(source.status(), ensure_ascii=False)
        assert "<endpoint>" in status
        assert "s3cr3tkey" not in status and "topsecret" not in status


class TestClientOwnership:
    async def test_path_without_leading_slash_is_normalized(self) -> None:
        client = RecordingClient(observation_body())
        source = WeatherApiSource(BASE, client, path="observation")
        assert source.path == "/observation"
        await source.collect()
        assert client.urls == [f"{BASE}/observation"]

    async def test_injected_client_is_not_closed_by_the_source(self) -> None:
        client = RecordingClient(observation_body())
        source = WeatherApiSource(BASE, client)
        await source.collect()
        await source.aclose()
        assert client.closed == 0
        # 关完之后仍可继续采集：注入的客户端生命周期归注入方
        assert len(await source.collect()) == 3

    async def test_self_created_client_is_closed_on_aclose(self) -> None:
        built = RecordingClient(observation_body())

        class _StubSource(WeatherApiSource):
            def _build_client(self) -> Any:
                return built

        source = _StubSource(BASE)
        assert len(await source.collect()) == 3
        await source.aclose()
        assert built.closed == 1
        assert built.urls == [f"{BASE}/observation"]


class TestAssembly:
    def test_absent_base_url_keeps_the_leg_out_entirely(self) -> None:
        source, state = build_weather(Settings(env="test", bus_backend="memory"))
        assert source is None
        assert (state.name, state.enabled, state.driver) == ("weather", False, "off")

    def test_enabled_row_reports_redacted_target_only(self) -> None:
        settings = Settings(
            env="test",
            bus_backend="memory",
            weather_api_base_url="https://user:***@weather.example.internal/v2",
            weather_api_path="/obs",
            weather_api_timeout_ms=1500,
        )
        source, state = build_weather(settings, client=RecordingClient(observation_body()))

        assert source is not None and state.enabled is True
        assert state.driver == "http"
        assert state.detail["target"] == "weather.example.internal"
        assert state.detail["path"] == "/obs"
        assert state.detail["timeout_ms"] == 1500
        assert "sup3rs3cr3t" not in json.dumps(state.as_dict())


@pytest.fixture
async def weather_container():
    settings = Settings(
        env="test",
        bus_backend="memory",
        simulator_enabled=False,
        delivery_mode="mock",
        weather_api_base_url=BASE,
    )
    container = create_container(settings, weather_client=mock_client(observation_body()))
    await container.start()
    try:
        yield container
    finally:
        await container.shutdown()


class TestIngestionIntegration:
    async def test_weather_leg_is_a_source_and_its_facts_show_up(self, weather_container) -> None:
        report = await weather_container.ingest.ingest_once()

        assert "weather_api" in report.sources_ok
        assert report.readings == 3
        stored = weather_container.store.telemetry.query(metric="rain_10min", limit=10)
        assert len(stored) == 1

        row = {state.name: state for state in weather_container.integration_status()}["weather"]
        assert row.enabled is True
        assert row.detail["rounds"] == 1
        assert row.detail["readings"] == 3
        assert row.detail["failures"] == 0

    async def test_broken_upstream_isolated_to_one_source(self) -> None:
        settings = Settings(
            env="test",
            bus_backend="memory",
            simulator_enabled=False,
            delivery_mode="mock",
            weather_api_base_url=BASE,
        )
        container = create_container(settings, weather_client=mock_client({"oops": 1}, status=500))
        await container.start()
        try:
            report = await container.ingest.ingest_once()
            assert "weather_api" in report.sources_failed
            assert report.readings == 0
            row = {state.name: state for state in container.integration_status()}["weather"]
            assert row.detail["failures"] == 1
            assert "HTTPStatusError" in str(row.detail["last_error"])
        finally:
            await container.shutdown()
