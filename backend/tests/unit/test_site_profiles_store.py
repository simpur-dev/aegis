"""区域级站点画像存储：一个区一条、替换语义与两端签名一致。"""

from __future__ import annotations

import inspect

import pytest

from aegis.domain.enums import HazardType
from aegis.domain.provenance import DataProvenance
from aegis.domain.site import SiteAttribute, SiteProfile
from aegis.storage.site_profiles import SiteProfileStore
from aegis.storage.store import PlatformStore, StoreProtocol


def make_profile(region: str = "540121", slope: float = 38.0, *, kind: str = "demo") -> SiteProfile:
    return SiteProfile(
        region_code=region,
        name="演示区",
        hazard_types=[HazardType.LANDSLIDE],
        attributes=[SiteAttribute(key="slope_angle_deg", value=slope, unit="°")],
        provenance=DataProvenance(data_kind=kind, sources=["site_profile:demo"], is_mock=kind == "demo"),
    )


class TestMemoryStore:
    def test_upsert_get_list_roundtrip(self) -> None:
        store = SiteProfileStore()
        store.upsert(make_profile())
        fetched = store.get("540121")
        assert fetched is not None and fetched.value_of("slope_angle_deg") == 38.0
        assert store.get("999999") is None
        assert [row.region_code for row in store.list()] == ["540121"]

    def test_replacement_keeps_one_row_per_region(self) -> None:
        store = SiteProfileStore()
        store.upsert(make_profile(slope=38.0))
        store.upsert(make_profile(slope=41.0))
        assert store.size == 1
        assert store.get("540121").value_of("slope_angle_deg") == 41.0  # type: ignore[union-attr]

    def test_region_filter_and_limit(self) -> None:
        store = SiteProfileStore()
        store.upsert(make_profile("540121"))
        store.upsert(make_profile("540221"))
        assert [row.region_code for row in store.list(region_code="540221")] == ["540221"]
        assert len(store.list(limit=1)) == 1
        with pytest.raises(ValueError, match="limit"):
            store.list(limit=0)

    def test_capacity_refuses_new_region_but_allows_update(self) -> None:
        store = SiteProfileStore(max_regions=1)
        store.upsert(make_profile("540121"))
        with pytest.raises(ValueError, match="容量"):
            store.upsert(make_profile("540221"))
        store.upsert(make_profile("540121", slope=40.0))
        assert store.size == 1

    async def test_platform_store_forwards_and_counts(self) -> None:
        store = PlatformStore()
        await store.upsert_site_profile(make_profile())
        fetched = await store.get_site_profile("540121")
        assert fetched is not None and fetched.name == "演示区"
        assert [row.region_code for row in await store.list_site_profiles()] == ["540121"]
        assert store.snapshot()["site_profile_count"] == 1


class TestProtocolParity:
    def test_port_declares_site_profile_methods(self) -> None:
        names = {name for name, _ in inspect.getmembers(StoreProtocol, predicate=callable)}
        assert {"upsert_site_profile", "get_site_profile", "list_site_profiles"} <= names

    def test_memory_and_postgres_signatures_match(self) -> None:
        try:
            from aegis.persistence.postgres import PostgresStore
        except Exception as exc:  # asyncpg 属于可选 extra，缺席是正常部署形态
            pytest.skip(f"未安装 postgres extra：{type(exc).__name__}")
        for method in ("upsert_site_profile", "get_site_profile", "list_site_profiles"):
            memory = inspect.signature(getattr(PlatformStore, method))
            durable = inspect.signature(getattr(PostgresStore, method))
            assert list(memory.parameters) == list(durable.parameters), f"{method} 两端签名分叉"
