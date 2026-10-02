"""空间查询端点：PostGIS 的半径/轨迹面能力必须能从应用被调用到。

审计实测（2026-10-02）：`stations_within` / `hazard_trace_summary` 这些 SQL 早就写好也被测过，
但它们只活在集成测试里——没进存储协议、没有任何 API 调用方。
"PostGIS 用于空间轨迹查询"这条考核口径因此只在测试进程里成立。

内存实现**不去伪造**这些查询：距离与覆盖面由 PostGIS 负责，另写一份 haversine/射线法
就会有两套几何口径，而验收时看不出差别。缺能力时端点响亮拒绝并说清要配什么后端。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from aegis.api.app import create_app
from aegis.config import Settings
from aegis.container import create_container
from aegis.persistence import geo
from aegis.storage.store import SupportsGeoQueries, geo_query_port

BASE = "http://testserver"

GEO_PATHS = ("/api/v1/geo/stations-within", "/api/v1/geo/stations-in-polygon", "/api/v1/geo/hazard-trace")
POLYGON = "POLYGON((91.0 29.0, 92.0 29.0, 92.0 30.0, 91.0 30.0, 91.0 29.0))"


class StubGeoStore:
    """只回答"装配有没有把请求原样交给几何侧"，不重复实现几何。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def stations_within(self, *, lon: float, lat: float, radius_m: float, limit: int = 50) -> list[dict[str, Any]]:
        self.calls.append(("stations_within", {"lon": lon, "lat": lat, "radius_m": radius_m, "limit": limit}))
        return [{"station_id": "RG54010101", "distance_m": 812.5}]

    async def stations_in_polygon(self, *, polygon_wkt: str, region_code: str | None = None) -> list[dict[str, Any]]:
        self.calls.append(("stations_in_polygon", {"polygon_wkt": polygon_wkt, "region_code": region_code}))
        return [{"station_id": "RG54010101"}]

    async def hazard_trace_summary(
        self, *, polygon_wkt: str, since: Any, until: Any = None, hazard_type: str | None = None
    ) -> dict[str, Any]:
        self.calls.append(("hazard_trace_summary", {"polygon_wkt": polygon_wkt}))
        return {"stations": 3, "readings": 42, "warnings": 2, "warning_ids": ["w-1"]}


def base_settings() -> Settings:
    return Settings(env="test", bus_backend="memory", store_backend="memory")


async def _client(store: Any) -> httpx.AsyncClient:
    container = create_container(base_settings(), with_simulator=False)
    container.store = store
    app = create_app(container.settings, container=container)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE)


@pytest.fixture
async def geo_store() -> StubGeoStore:
    return StubGeoStore()


@pytest.fixture
async def geo_client(geo_store: StubGeoStore) -> AsyncIterator[httpx.AsyncClient]:
    client = await _client(geo_store)
    yield client
    await client.aclose()


@pytest.fixture
async def plain_client() -> AsyncIterator[httpx.AsyncClient]:
    # 不替换 store：内存视图就是这次要量的"没有几何能力"那一侧。
    client = await _client(create_container(base_settings(), with_simulator=False).store)
    yield client
    await client.aclose()


class TestPortDiscovery:
    def test_内存视图不谎报自己有几何能力(self) -> None:
        from aegis.storage.store import PlatformStore

        assert geo_query_port(PlatformStore()) is None

    def test_替身满足协议(self) -> None:
        assert isinstance(StubGeoStore(), SupportsGeoQueries)

    def test_PostgresStore真的满足这个协议(self) -> None:
        # 这条防"协议与实现各说各话"：实现改名或漏方法时，能力发现会静默变成"不支持"，
        # 而端点仍会以 503 回答——看起来像"你没配 postgres"，实际是代码漂了。
        from aegis.persistence.postgres import PostgresStore

        for name in ("stations_within", "stations_in_polygon", "hazard_trace_summary"):
            assert callable(getattr(PostgresStore, name, None)), name


class TestStationsWithin:
    async def test_半径查询把参数原样交给几何侧(self, geo_client: httpx.AsyncClient, geo_store: StubGeoStore) -> None:
        response = await geo_client.get("/api/v1/geo/stations-within", params={"lon": 91.5, "lat": 29.5, "radius_m": 30000, "limit": 5})
        assert response.status_code == 200
        body = response.json()
        assert body["driver"] == "postgis"
        assert body["count"] == 1
        assert body["items"][0]["distance_m"] == 812.5
        assert geo_store.calls == [("stations_within", {"lon": 91.5, "lat": 29.5, "radius_m": 30000.0, "limit": 5})]

    @pytest.mark.parametrize(
        "params",
        [
            {"lon": 999.0, "lat": 29.5, "radius_m": 100},  # 经度越界
            {"lon": 91.5, "lat": 999.0, "radius_m": 100},  # 纬度越界
            {"lon": 91.5, "lat": 29.5, "radius_m": 0},  # 零半径在 geography 口径下无意义
            {"lon": 91.5, "lat": 29.5, "radius_m": -1},
            {"lon": 91.5, "lat": 29.5, "radius_m": 100, "limit": 0},
            {"lon": 91.5, "lat": 29.5, "radius_m": geo.MAX_RADIUS_M + 1},
        ],
    )
    async def test_边界输入直接422而不是送去数据库试错(self, geo_client: httpx.AsyncClient, params: dict[str, float]) -> None:
        assert (await geo_client.get("/api/v1/geo/stations-within", params=params)).status_code == 422


class TestPolygonQueries:
    async def test_面内站点(self, geo_client: httpx.AsyncClient) -> None:
        response = await geo_client.get("/api/v1/geo/stations-in-polygon", params={"polygon": POLYGON})
        assert response.status_code == 200
        assert response.json()["driver"] == "postgis"

    async def test_非法WKT被拒(self, geo_client: httpx.AsyncClient) -> None:
        response = await geo_client.get("/api/v1/geo/stations-in-polygon", params={"polygon": "LINESTRING(0 0, 1 1)"})
        assert response.status_code == 422

    async def test_轨迹汇总要求带时区且正向的时间窗(self, geo_client: httpx.AsyncClient) -> None:
        ok = await geo_client.get("/api/v1/geo/hazard-trace", params={"polygon": POLYGON, "since": "2026-10-01T00:00:00Z"})
        assert ok.status_code == 200
        assert ok.json()["summary"]["warnings"] == 2
        for params in (
            {"polygon": POLYGON, "since": "2026-10-01T00:00:00"},  # 无时区
            {"polygon": POLYGON, "since": "2026-10-02T00:00:00Z", "until": "2026-10-01T00:00:00Z"},  # 逆序
            {"polygon": POLYGON, "since": "2026-10-01T00:00:00Z", "until": "2026-10-01T00:00:00Z"},  # 空窗
        ):
            assert (await geo_client.get("/api/v1/geo/hazard-trace", params=params)).status_code == 422


class TestCapabilityIsVisible:
    async def test_内存形态下三个端点都响亮拒绝(self, plain_client: httpx.AsyncClient) -> None:
        probes = {
            GEO_PATHS[0]: {"lon": 91.5, "lat": 29.5, "radius_m": 1000},
            GEO_PATHS[1]: {"polygon": POLYGON},
            GEO_PATHS[2]: {"polygon": POLYGON, "since": "2026-10-01T00:00:00Z"},
        }
        for path, params in probes.items():
            response = await plain_client.get(path, params=params)
            assert response.status_code == 503, path
            detail = response.json()["detail"]
            assert detail["code"] == "E_GEO_UNAVAILABLE"
            # 拒绝必须给出下一步：说清要配哪个后端，而不是留一句"不支持"。
            assert "postgres" in detail["requires"]

    def test_三个端点都只读(self) -> None:
        # 契约门禁按方法判定；这里钉住"空间查询不写任何东西"。
        container = create_container(base_settings(), with_simulator=False)
        app = create_app(container.settings, container=container)
        get_paths = {route.path for route in app.routes if "GET" in (getattr(route, "methods", None) or set())}
        assert set(GEO_PATHS) <= get_paths
        write_paths: set[str] = set()
        for route in app.routes:
            methods = getattr(route, "methods", None) or set()
            if methods & {"POST", "PUT", "PATCH", "DELETE"}:
                write_paths.add(route.path)
        assert not [path for path in write_paths if path.startswith("/api/v1/geo/")]
