"""站点清单（`GET /api/v1/stations`）：内存台账、维表 SQL 构造与 HTTP 面。

这一层的价值全在"不编造"上：站名、经纬度、高程只有维表里真的有才返回，
内存视图只能证明"这个站报过数、在哪个行政区"。所以用例盯三件事——
清单里绝不出现凭空坐标、两类来源的行形状逐键一致、维表 SQL 真的把 `geom` 取回来
（既有的半径/面查询刻意不取坐标，直接复用会让清单永远画不出点）。
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from aegis.api.app import create_app
from aegis.config import Settings
from aegis.container import create_container
from aegis.domain.messages import TelemetryReading
from aegis.persistence.errors import GeoArgumentError
from aegis.persistence.geo import MAX_LIMIT, build_stations_list, stations_list
from aegis.storage.store import PlatformStore

STATION_KEYS = {"station_id", "name_zh", "region_code", "hazard_focus", "elevation_m", "lon", "lat", "geom"}


def reading(station_id: str, region_code: str, *, metric: str = "rain_10min", value: float = 12.0) -> TelemetryReading:
    return TelemetryReading(station_id=station_id, metric=metric, value=value, unit="mm", region_code=region_code)


class _CapturingConn:
    """替身连接：只记录实参，让"执行编排"这一层能脱离数据库被证明。"""

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self._rows = rows or []

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self.calls.append((sql, args))
        return [dict(row) for row in self._rows]


class TestMemoryInventory:
    async def test_observed_stations_are_listed_without_any_invented_geometry(self) -> None:
        store = PlatformStore()
        await store.telemetry.add([reading("RG-540121-01", "540121"), reading("GNS-540121-02", "540121", metric="displacement_mm")])
        rows = await store.list_stations()

        assert [row["station_id"] for row in rows] == ["GNS-540121-02", "RG-540121-01"], (
            "清单顺序必须是 station_id 升序，否则两轮结果无法逐行对账"
        )
        assert all(set(row) == STATION_KEYS for row in rows), "行形状缺键或多键都会让前端把「没有坐标」读成「这一行不存在」"
        assert all(row["lon"] is None and row["lat"] is None and row["geom"] is None for row in rows)
        assert all(row["name_zh"] == "" and row["elevation_m"] is None for row in rows)

    async def test_region_filter_selects_whole_districts_only(self) -> None:
        store = PlatformStore()
        await store.telemetry.add([reading("RG-540121-01", "540121"), reading("SNW-540321-01", "540321")])

        rows = await store.list_stations(region_code="540321")

        assert [row["station_id"] for row in rows] == ["SNW-540321-01"]
        assert rows[0]["region_code"] == "540321"

    async def test_same_station_reporting_another_region_keeps_the_first_record(self) -> None:
        """区划以首次上报为准：一个站的区划归属属于台账变更，不该被后到的读数静默改写。"""
        store = PlatformStore()
        await store.telemetry.add([reading("RG-540121-01", "540121"), reading("RG-540121-01", "540221")])

        rows = await store.list_stations()

        assert rows[0]["region_code"] == "540121"

    async def test_station_survives_bounded_eviction_of_readings(self) -> None:
        """读数是有界窗口，"这个站报过数"不是：被淘汰掉的读数不该把站点一起带出清单。"""
        store = PlatformStore(telemetry_maxlen=4)
        await store.telemetry.add([reading("RG-540121-01", "540121", value=float(i)) for i in range(20)])
        rows = await store.list_stations()

        assert store.telemetry.size == 4, "窗口没生效的话这条用例什么都没测到"
        assert [row["station_id"] for row in rows] == ["RG-540121-01"]

    async def test_limit_bounds_are_rejected_not_clamped(self) -> None:
        store = PlatformStore()
        await store.telemetry.add([reading("RG-540121-01", "540121"), reading("GNS-540121-02", "540121")])

        assert len(await store.list_stations(limit=1)) == 1
        with pytest.raises(ValueError, match="limit"):
            await store.list_stations(limit=0)


class TestLedgerQueryShape:
    def test_coordinates_are_selected_back_explicitly(self) -> None:
        """维表查询必须把 geom 取回来：STATION_FIELDS 那四条半径/面查询不带坐标。"""
        sql, args = build_stations_list(limit=50)

        assert "ST_X(geom::geometry) AS lon" in sql
        assert "ST_Y(geom::geometry) AS lat" in sql
        assert "ST_AsEWKT(geom::geometry) AS geom" in sql
        assert "ORDER BY station_id" in sql
        assert args == [50]

    def test_region_code_is_bound_never_inlined(self) -> None:
        sql, args = build_stations_list(region_code="540121", limit=10)

        assert "region_code = $1" in sql
        assert args == ["540121", 10]
        assert "540121" not in sql.split("WHERE")[0], "区划值只能出现在绑定参数里"

    @pytest.mark.parametrize("limit", [0, -1, MAX_LIMIT + 1])
    def test_limit_out_of_range_fails_before_touching_the_database(self, limit: int) -> None:
        with pytest.raises(GeoArgumentError):
            build_stations_list(limit=limit)

    async def test_executor_returns_plain_dicts(self) -> None:
        conn = _CapturingConn(
            [
                {
                    "station_id": "RG-1",
                    "name_zh": "拉萨河站",
                    "region_code": "540102",
                    "hazard_focus": ["洪水"],
                    "elevation_m": 3650.0,
                    "lon": 91.1,
                    "lat": 29.65,
                    "geom": "SRID=4326;POINT(91.1 29.65)",
                }
            ]
        )

        rows = await stations_list(conn, region_code="540102", limit=5)

        assert conn.calls[0][1] == ("540102", 5)
        assert rows[0]["lon"] == 91.1 and rows[0]["name_zh"] == "拉萨河站"


class TestStationsEndpoint:
    async def test_empty_platform_reports_no_stations_rather_than_failing(self, settings: Settings) -> None:
        ctn = create_container(settings, with_simulator=False)
        with TestClient(create_app(settings, container=ctn)) as client:
            body = client.get("/api/v1/stations").json()

        assert body == {"count": 0, "items": []}

    async def test_observed_stations_come_through_the_http_surface(self, settings: Settings) -> None:
        """清单与遥测同源：站点不必另登记一次才能被看见。"""
        ctn = create_container(settings, with_simulator=False)
        await ctn.store.telemetry.add([reading("RG-540121-01", "540121"), reading("LKS-540221-03", "540221")])
        with TestClient(create_app(settings, container=ctn)) as client:
            body = client.get("/api/v1/stations").json()

        assert body["count"] == 2
        assert [row["station_id"] for row in body["items"]] == ["LKS-540221-03", "RG-540121-01"]
        assert body["items"][0]["lon"] is None, "HTTP 面也不许出现凭空的坐标"

    def test_region_code_shape_is_validated_like_the_domain_model(self, settings: Settings) -> None:
        """格式与 TelemetryReading.region_code / monitoring_stations 的 CHECK 同口径：
        写错区划应当立刻 422，而不是回一个"这个区没有站"的空清单。"""
        ctn = create_container(settings, with_simulator=False)
        with TestClient(create_app(settings, container=ctn)) as client:
            assert client.get("/api/v1/stations", params={"region_code": "5401"}).status_code == 422
            assert client.get("/api/v1/stations", params={"region_code": "拉萨"}).status_code == 422
            assert client.get("/api/v1/stations", params={"region_code": "540121"}).status_code == 200

    def test_limit_bounds_are_enforced_by_the_schema(self, settings: Settings) -> None:
        ctn = create_container(settings, with_simulator=False)
        with TestClient(create_app(settings, container=ctn)) as client:
            for bad in (0, 1001, -5):
                assert client.get("/api/v1/stations", params={"limit": bad}).status_code == 422
