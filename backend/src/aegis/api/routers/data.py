"""数据面：站点清单、几何查询（半径 / 轨迹面 / 轨迹汇总）与遥测读数。

几何这一路刻意不做内存近似——同一句『这个站在不在范围内』存在两套口径时，看不出差别的是验收方，付代价的是被漏掉的站点指挥员。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from aegis.api.deps import get_container
from aegis.container import PlatformContainer
from aegis.persistence import geo
from aegis.storage.store import geo_query_port

# `since`/`until` 的先后关系表达不进 OpenAPI（两个独立参数），逆序窗是运行期才判的 422。
# 不声明它，契约文档就只认识 FastAPI 的校验错误形状，等于对调用方隐瞒了这个分支——
# 因此 422 用 anyOf 同时声明"窗口业务拒绝"与"参数本身未通过校验"两种合法响应体。
_WINDOW_REJECTION_RESPONSE: dict[int | str, dict[str, Any]] = {
    422: {
        "description": "时间窗不合法：缺时区 / 逆序 / 空窗（跨字段约束），或查询参数未通过校验",
        "content": {
            "application/json": {
                "schema": {
                    "anyOf": [
                        {
                            "type": "object",
                            "required": ["detail"],
                            "additionalProperties": False,
                            "properties": {
                                "detail": {
                                    "type": "object",
                                    "required": ["code", "message"],
                                    "properties": {"code": {"type": "string"}, "message": {"type": "string"}},
                                }
                            },
                        },
                        {"$ref": "#/components/schemas/HTTPValidationError"},
                    ]
                }
            }
        },
    }
}


def _geo_unavailable() -> HTTPException:
    """几何查询不可用时的回答：说清缺什么，而不是留一句"不支持"让人猜。

    选 503 而不是 404：路由存在、能力也存在，只是当前存储后端不提供几何算子——
    调用方据此换配置就能修，按"没有这个接口"处理就会走错方向。
    """
    return HTTPException(
        status_code=503,
        detail={
            "code": "E_GEO_UNAVAILABLE",
            "message": "当前存储后端不提供几何查询（半径/轨迹面）",
            "requires": "AEGIS_STORE_BACKEND=postgres（PostGIS）",
        },
    )


def build_router() -> APIRouter:
    router = APIRouter()

    @router.get("/api/v1/stations", tags=["data"])
    async def list_stations(
        ctn: PlatformContainer = Depends(get_container),
        region_code: str | None = Query(default=None, pattern=r"^[0-9A-Z]{6,24}$"),
        limit: int = Query(default=200, ge=1, le=1_000),
    ) -> dict[str, Any]:
        """站点清单：维表里登记的站（带名称与坐标）+ 报过数但还没登记的站。

        坐标只有在维表真的写了才返回：地图宁可把该站列进"未定位"，也不按区划中心或站点编号
        猜一个经纬度——演示里看不出差别，验收时被当成实测坐标就是事故。
        `region_code` 的格式与 `monitoring_stations` 的 CHECK 同口径，写错了直接 422，
        免得一次笔误变成"这个区一个站都没有"。
        """
        rows = await ctn.store.list_stations(region_code=region_code, limit=limit)
        return {"count": len(rows), "items": rows}

    @router.get("/api/v1/geo/stations-within", tags=["data"])
    async def geo_stations_within(
        ctn: PlatformContainer = Depends(get_container),
        lon: float = Query(..., ge=-180.0, le=180.0),
        lat: float = Query(..., ge=-90.0, le=90.0),
        radius_m: float = Query(..., gt=0.0, le=float(geo.MAX_RADIUS_M)),
        limit: int = Query(default=50, ge=1, le=geo.MAX_LIMIT),
    ) -> dict[str, Any]:
        """半径内的站点，按距离升序。距离由 PostGIS 的 geography 口径算，单位米。

        这一路不做内存近似：同一句"这个站在不在范围内"若存在两套口径，
        看不出差别的是验收方，付代价的是被漏掉的站点指挥员。
        """
        port = geo_query_port(ctn.store)
        if port is None:
            raise _geo_unavailable()
        lon, lat = geo.check_point(lon, lat)
        radius = geo.check_radius(radius_m)
        items = await port.stations_within(lon=lon, lat=lat, radius_m=radius, limit=limit)
        query = {"lon": lon, "lat": lat, "radius_m": radius, "limit": limit}
        return {"driver": "postgis", "query": query, "count": len(items), "items": items}

    @router.get("/api/v1/geo/stations-in-polygon", tags=["data"])
    async def geo_stations_in_polygon(
        ctn: PlatformContainer = Depends(get_container),
        polygon: str = Query(..., min_length=14, description="POLYGON/MULTIPOLYGON 的 WKT（SRID=4326，经度在前）"),
        region_code: str | None = Query(default=None, pattern=r"^[0-9A-Z]{6,24}$"),
    ) -> dict[str, Any]:
        """轨迹面内的站点：一张灾害面罩住哪些站，决定靶向发布给谁。"""
        port = geo_query_port(ctn.store)
        if port is None:
            raise _geo_unavailable()
        # 参数在进入存储之前就按 geo 的唯一口径判掉：`QueryArgumentError` 的语义就是
        # "调用方的错，尚未触达数据库"，交给数据库报错会把 4xx 变成一次真实查询。
        wkt = geo.check_polygon(polygon)
        items = await port.stations_in_polygon(polygon_wkt=wkt, region_code=region_code)
        return {
            "driver": "postgis",
            "query": {"polygon": wkt, "region_code": region_code},
            "count": len(items),
            "items": items,
        }

    @router.get("/api/v1/geo/hazard-trace", tags=["data"])
    async def geo_hazard_trace(
        ctn: PlatformContainer = Depends(get_container),
        polygon: str = Query(..., min_length=14),
        since: datetime = Query(..., description="时间窗起点，必须带时区"),
        until: datetime | None = Query(default=None),
        hazard_type: str | None = None,
    ) -> dict[str, Any]:
        """灾害轨迹面汇总：面内站数、面内读数条数、面内预警条数（含预警编号）。

        `since`/`until` 的裸时间与逆序窗由 `persistence.geo` 直接拒绝——
        少写时区会被按本地时区猜一遍，轨迹统计就整体平移几小时，这种错最难发现。
        """
        port = geo_query_port(ctn.store)
        if port is None:
            raise _geo_unavailable()
        wkt = geo.check_polygon(polygon)
        geo.check_window(since, until)
        summary = await port.hazard_trace_summary(polygon_wkt=wkt, since=since, until=until, hazard_type=hazard_type)
        return {
            "driver": "postgis",
            "query": {"polygon": wkt, "since": since, "until": until, "hazard_type": hazard_type},
            "summary": summary,
        }

    @router.get("/api/v1/telemetry", tags=["data"], responses=_WINDOW_REJECTION_RESPONSE)
    async def list_telemetry(
        ctn: PlatformContainer = Depends(get_container),
        station_id: str | None = None,
        metric: str | None = None,
        region_code: str | None = None,
        since: datetime | None = Query(default=None, description="时间窗起点，必须带时区"),
        until: datetime | None = Query(default=None, description="时间窗终点，必须带时区"),
        limit: int = Query(default=200, ge=1, le=5_000),
    ) -> dict[str, Any]:
        """遥测读数：可按站点/指标/区域与时间窗过滤。

        时间窗的口径与几何查询一致：不带时区的时间会被按本地时区猜一遍，整窗读数就平移几小时；
        逆序窗直接 422，避免把"写反了"当成"没有数据"。
        """
        for value in (since, until):
            if value is not None and value.tzinfo is None:
                raise HTTPException(status_code=422, detail={"code": "E_BAD_WINDOW", "message": "since/until 必须带时区"})
        if since is not None and until is not None and until <= since:
            raise HTTPException(
                status_code=422,
                detail={"code": "E_BAD_WINDOW", "message": "时间窗逆序或为空窗：until 必须晚于 since"},
            )
        rows = ctn.store.telemetry.query(
            station_id=station_id,
            metric=metric,
            region_code=region_code,
            since=since,
            until=until,
            limit=limit,
        )
        return {"count": len(rows), "items": [r.model_dump() for r in rows]}

    @router.get("/api/v1/assess/evidence", tags=["data"])
    async def get_assess_evidence(
        ctn: PlatformContainer = Depends(get_container),
        region_code: str = Query(..., pattern=r"^[0-9A-Z]{6,24}$"),
        window_minutes: int = Query(default=60, ge=1, le=9_999),
    ) -> dict[str, Any]:
        """研判证据包读面：与平台推给智能体的证据同一构造器、同一派生口径。

        外部实现出进程部署时经此拉取（推送腿之外的拉取腿）；进程内装配仍走推送。
        """
        builder = ctn.assess_evidence
        if builder is None:
            raise HTTPException(
                status_code=503,
                detail={"code": "E_EVIDENCE_UNAVAILABLE", "message": "研判证据构建器未装配"},
            )
        evidence = await builder.build(region=region_code, hits=[], window_minutes=window_minutes)
        return evidence.model_dump(mode="json")

    @router.get("/api/v1/site-profiles", tags=["data"])
    async def list_site_profiles(
        ctn: PlatformContainer = Depends(get_container),
        region_code: str | None = Query(default=None, pattern=r"^[0-9A-Z]{6,24}$"),
        limit: int = Query(default=200, ge=1, le=1_000),
    ) -> dict[str, Any]:
        """区域级站点画像：静态孕灾属性（坡度/流域面积/坝高等），不含动态触发量。"""
        rows = await ctn.store.list_site_profiles(region_code=region_code, limit=limit)
        return {"count": len(rows), "items": [r.model_dump(mode="json") for r in rows]}

    @router.get("/api/v1/site-profiles/{region_code}", tags=["data"])
    async def get_site_profile(region_code: str, ctn: PlatformContainer = Depends(get_container)) -> dict[str, Any]:
        """按区划读单个画像；没有就明确 404，不返回空对象让人误以为"属性为空"。"""
        profile = await ctn.store.get_site_profile(region_code)
        if profile is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "E_SITE_PROFILE_NOT_FOUND", "message": f"区域 {region_code} 未配置站点画像"},
            )
        return profile.model_dump(mode="json")

    return router
