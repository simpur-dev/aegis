"""摄取轮次的失败隔离：一个数据源坏掉，整轮采集不能跟着崩。

`connectors/base.py` 头部承诺"单源独立超时与失败隔离（高原弱网常见）"，而实现里
`asyncio.gather(..., return_exceptions=False)` 会把第一个异常直接抛出 `ingest_once`——
下面的 `isinstance(outcome, BaseException)` 分支于是永远走不到，"隔离"只存在于注释里。
这条用例是该承诺的唯一守卫：此前没有任何测试构造过"有好有坏"的源组合。
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient

from aegis.api.app import create_app
from aegis.bus.gateway import AgentGateway
from aegis.config import Settings
from aegis.connectors.base import DataSource, IngestService
from aegis.container import create_container
from aegis.domain.messages import AgentMessage, TelemetryReading, now_iso
from aegis.observability.tracer import Tracer
from aegis.storage.store import PlatformStore
from conftest import readings_rain_burst


class _Source(DataSource):
    """好源：固定返回几条读数。"""

    def __init__(self, name: str, readings: Sequence[TelemetryReading]) -> None:
        self.name = name
        self._readings = list(readings)

    async def collect(self) -> list[TelemetryReading]:
        return list(self._readings)


class _BoomSource(DataSource):
    name = "boom"

    async def collect(self) -> list[TelemetryReading]:
        raise RuntimeError("站端掉电")


class _SlowSource(DataSource):
    name = "slow"

    async def collect(self) -> list[TelemetryReading]:
        await asyncio.sleep(1.0)
        return readings_rain_burst()


def build_ingest(
    gateway: AgentGateway,
    tracer: Tracer,
    settings: Settings,
    store: PlatformStore,
    sources: list[DataSource],
) -> IngestService:
    return IngestService(
        gateway=gateway,
        store=store,
        tracer=tracer,
        settings=settings,
        sources=sources,
        per_source_timeout_ms=40,
    )


@pytest.fixture
async def started_gateway(gateway: AgentGateway) -> AgentGateway:
    await gateway.start()
    return gateway


@pytest.fixture
def store() -> PlatformStore:
    return PlatformStore()


def mixed_sources() -> list[DataSource]:
    return [_Source("good_a", readings_rain_burst()[:1]), _BoomSource(), _SlowSource()]


class TestPerSourceIsolation:
    async def test_processing_endpoint_keeps_failed_source_and_invalid_reading_separate(self, settings: Settings) -> None:
        container = create_container(settings)
        valid = readings_rain_burst()[0]
        invalid = valid.model_copy(update={"value": float("nan")})
        for source in (_Source("mixed", [valid, invalid]), _BoomSource()):
            container.ingest.add_source(source)
        await container.start()
        app = create_app(settings, container=container)
        try:
            result = await container.ingest.ingest_once()
            assert (result.readings, result.published) == (1, 1)
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                response = await client.get("/api/v1/telemetry/processing")
                assert response.status_code == 200
                summary = response.json()["items"][0]
                assert summary["status"] == "partial"
                response = await client.get(f"/api/v1/telemetry/processing/{summary['id']}")
                assert response.status_code == 200
                detail = response.json()
                assert detail["source_details"][1]["issues"][0]["code"] == "RuntimeError"
                stages = {stage["key"]: stage for stage in detail["stages"]}
                assert stages["normalize"]["issues"][0]["samples"][0]["value"] == "nan"
                assert stages["normalize"]["results"]["rejected_values"] == 1
                assert stages["store"]["results"]["queryable"] == 1
                assert stages["durability"]["status"] == "skipped"
                assert stages["publish"]["result_samples"][0]["message_id"].startswith("msg_")
                assert (await client.get("/api/v1/telemetry/processing/unknown")).status_code == 404
        finally:
            await container.shutdown()

    async def test_one_broken_source_does_not_kill_the_round(
        self, started_gateway: AgentGateway, tracer: Tracer, settings: Settings, store: PlatformStore
    ) -> None:
        ingest = build_ingest(started_gateway, tracer, settings, store, mixed_sources())
        report = await ingest.ingest_once()

        assert report.sources_ok == ["good_a"]
        assert set(report.sources_failed) == {"boom", "slow"}
        assert "站端掉电" in report.sources_failed["boom"]
        assert report.readings == 1
        assert report.ok is False

    async def test_timeout_reason_names_the_source_and_the_budget(
        self, started_gateway: AgentGateway, tracer: Tracer, settings: Settings, store: PlatformStore
    ) -> None:
        ingest = build_ingest(started_gateway, tracer, settings, store, mixed_sources())
        report = await ingest.ingest_once()
        assert "slow" in report.sources_failed["slow"]
        assert "0.04s" in report.sources_failed["slow"]

    async def test_healthy_readings_reach_the_store_even_when_peers_failed(
        self, started_gateway: AgentGateway, tracer: Tracer, settings: Settings, store: PlatformStore
    ) -> None:
        ingest = build_ingest(started_gateway, tracer, settings, store, mixed_sources())
        await ingest.ingest_once()
        assert [r.station_id for r in store.telemetry.query(limit=10)] == ["RG-01"]

    async def test_ingest_latency_stops_at_readable_storage(
        self,
        started_gateway: AgentGateway,
        tracer: Tracer,
        settings: Settings,
        store: PlatformStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        available_at = datetime(2026, 11, 24, 3, tzinfo=UTC)
        clock = available_at
        reading = readings_rain_burst()[0].model_copy(
            update={
                "observed_at": now_iso(available_at - timedelta(seconds=20)),
                "ingested_at": now_iso(available_at - timedelta(seconds=2)),
            },
        )

        async def publish(item: TelemetryReading, _message: AgentMessage) -> None:
            nonlocal clock
            assert store.telemetry.query(station_id=item.station_id) == [item]
            clock += timedelta(seconds=45)

        monkeypatch.setattr("aegis.connectors.base.utc_now", lambda: clock)
        monkeypatch.setattr(started_gateway, "publish_telemetry", publish)
        ingest = build_ingest(started_gateway, tracer, settings, store, [_Source("weather", [reading])])

        report = await ingest.ingest_once()

        assert report.published == 1
        assert report.max_ingest_latency_seconds == pytest.approx(20.0)
        assert tracer.ledger.stats("ingest_store_to_query_ms").max == pytest.approx(2000.0)

    async def test_round_fully_recovers_once_the_broken_sources_are_gone(
        self, started_gateway: AgentGateway, tracer: Tracer, settings: Settings, store: PlatformStore
    ) -> None:
        ingest = build_ingest(started_gateway, tracer, settings, store, mixed_sources())
        await ingest.ingest_once()
        assert ingest.remove_source("boom") is True
        assert ingest.remove_source("slow") is True

        report = await ingest.ingest_once()
        assert report.ok is True
        assert report.readings == 1

    async def test_no_sources_yields_an_empty_report_rather_than_an_error(
        self, started_gateway: AgentGateway, tracer: Tracer, settings: Settings, store: PlatformStore
    ) -> None:
        ingest = build_ingest(started_gateway, tracer, settings, store, [])
        report = await ingest.ingest_once()
        assert (report.readings, report.sources_ok, report.sources_failed) == (0, [], {})

    def test_settings_fixture_keeps_the_simulator_off(self, settings: Settings) -> None:
        """夹具的 `simulator_enabled=False` 现在真的生效：否则容器里的源清单就不是这里构造的那份。"""
        assert settings.simulator_enabled is False
