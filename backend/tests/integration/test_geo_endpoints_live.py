"""真 PostGIS 上的空间查询端点：API → 能力发现 → PostGIS 这整条链。

单测里的替身只能证明"参数被原样交出去"，证不了两件事：
① `geo_query_port` 在真实现面前会**放行**（协议与实现改名脱节时它会静默变成"不支持"，
   端点照样以 503 回答，看起来像"你没配 postgres"）；
② `ST_DWithin` 真的按米算、`ST_Covers` 真的按面罩住站。
所以这里跑部署形态那条链，用的库与 `test_persistence_postgres.py` 同一个。

起库与运行方式见该文件头注释；本文件的站点一律落在阿里地区（约 85°E, 33°N），
与那份文件的拉萨点位（91.28°E, 29.9°N）相距 700km 以上，互不污染对方的计数断言。
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import timedelta

import httpx
import pytest

from aegis.api.app import create_app
from aegis.config import Settings
from aegis.container import PlatformContainer, create_container
from aegis.domain.messages import utc_now

DSN = os.getenv("AEGIS_TEST_PG_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="未设置 AEGIS_TEST_PG_DSN，跳过真实 Postgres 测试")

BASE = "http://testserver"
# 约 1.9km 与约 63km：半径查询要同时看到"进来两个"与"只留最近的那个"。
NEAR_A = ("GEOAPI-N1", "542500", 85.0000, 33.0000)
NEAR_B = ("GEOAPI-N2", "542500", 85.0200, 33.0000)
FAR_C = ("GEOAPI-F1", "542500", 85.6000, 33.3000)
ZONE_NEAR = "POLYGON((84.99 32.99, 85.03 32.99, 85.03 33.01, 84.99 33.01, 84.99 32.99))"
ZONE_EMPTY = "POLYGON((78.00 32.00, 78.01 32.00, 78.01 32.01, 78.00 32.01, 78.00 32.00))"


@pytest.fixture
async def container() -> AsyncIterator[PlatformContainer]:
    settings = Settings(
        env="test",
        bus_backend="memory",
        store_backend="postgres",
        pg_dsn=DSN,
        pg_apply_migrations_on_start=True,
        simulator_enabled=False,
    )
    ctn = create_container(settings, with_simulator=False)
    await ctn.start()
    for station_id, region, lon, lat in (NEAR_A, NEAR_B, FAR_C):
        await ctn.store.upsert_station(station_id, region, lon, lat, name_zh="门禁站")
    # 写侧走有界缓冲：不落一次 flush，读侧可能还看不到刚登记的站。
    await ctn.store.flush()  # type: ignore[attr-defined]
    yield ctn
    await ctn.shutdown()


@pytest.fixture
async def client(container: PlatformContainer) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(container.settings, container=container)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as http_client:
        yield http_client


class TestGeoEndpointsOnRealPostGIS:
    async def test_能力发现在真实现前放行(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/api/v1/geo/stations-within", params={"lon": 85.0, "lat": 33.0, "radius_m": 5000})
        assert response.status_code == 200, response.text
        assert response.json()["driver"] == "postgis"

    async def test_半径查询按米算并按距离升序(self, client: httpx.AsyncClient) -> None:
        body = (await client.get("/api/v1/geo/stations-within", params={"lon": 85.0, "lat": 33.0, "radius_m": 5000})).json()
        assert [item["station_id"] for item in body["items"]] == ["GEOAPI-N1", "GEOAPI-N2"]
        assert body["count"] == 2
        first, second = body["items"]
        assert first["distance_m"] < second["distance_m"]
        # 约 1.9km 量级：按平面度数算不会落进这个带，按测地线算才会
        assert 500 < second["distance_m"] < 5000

    async def test_收紧半径就只剩中心站(self, client: httpx.AsyncClient) -> None:
        body = (await client.get("/api/v1/geo/stations-within", params={"lon": 85.0, "lat": 33.0, "radius_m": 200})).json()
        assert [item["station_id"] for item in body["items"]] == ["GEOAPI-N1"]

    async def test_空候选集是空表而不是报错(self, client: httpx.AsyncClient) -> None:
        body = (await client.get("/api/v1/geo/stations-within", params={"lon": 70.0, "lat": 40.0, "radius_m": 1000})).json()
        assert (body["count"], body["items"]) == (0, [])

    async def test_面内站点只罩住面里的那两个(self, client: httpx.AsyncClient) -> None:
        inside = (await client.get("/api/v1/geo/stations-in-polygon", params={"polygon": ZONE_NEAR})).json()
        assert {item["station_id"] for item in inside["items"]} == {"GEOAPI-N1", "GEOAPI-N2"}
        outside = (await client.get("/api/v1/geo/stations-in-polygon", params={"polygon": ZONE_EMPTY})).json()
        assert outside["count"] == 0

    async def test_区域过滤与面查询叠加时仍然保守(self, client: httpx.AsyncClient) -> None:
        # 写错区号会得到"空集"而不是"整张表"：这类查询宁可少给，也不要给成假的靶向范围。
        body = (await client.get("/api/v1/geo/stations-in-polygon", params={"polygon": ZONE_NEAR, "region_code": "540100"})).json()
        assert body["count"] == 0

    async def test_轨迹面汇总给出站数与计数形状(self, client: httpx.AsyncClient) -> None:
        since = (utc_now() - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
        body = (await client.get("/api/v1/geo/hazard-trace", params={"polygon": ZONE_NEAR, "since": since})).json()
        summary = body["summary"]
        assert summary["station_count"] == 2
        assert {key: summary[key] for key in summary if key in ("readings", "warnings")} == {"readings": 0, "warnings": 0}
        assert isinstance(summary["warning_ids"], list)

    async def test_非法几何参数在触达数据库之前就被拒(self, client: httpx.AsyncClient) -> None:
        # 真库上这条尤其重要：一次坏参数查询会真的建连接、真的解析 WKT。
        assert (await client.get("/api/v1/geo/stations-in-polygon", params={"polygon": "POINT(85 33)"})).status_code == 422
        assert (
            await client.get("/api/v1/geo/hazard-trace", params={"polygon": ZONE_NEAR, "since": "2026-10-01T00:00:00"})
        ).status_code == 422
