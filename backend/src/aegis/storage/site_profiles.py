"""区域级站点画像的内存存储：一个区一条，后写覆盖前写。

容量上限而不是无界字典：画像由导入命令显式写入，正常只会是"区数"量级；
一旦有调用方按站号误写，无界会把这条错误藏很久，有界能立刻暴露。
"""

from __future__ import annotations

from aegis.domain.site import SiteProfile


class SiteProfileStore:
    def __init__(self, max_regions: int = 1_000) -> None:
        if max_regions <= 0:
            raise ValueError("max_regions 必须为正")
        self._max_regions = max_regions
        self._rows: dict[str, SiteProfile] = {}

    def upsert(self, profile: SiteProfile) -> None:
        if profile.region_code not in self._rows and len(self._rows) >= self._max_regions:
            raise ValueError(f"site_profiles 容量已满（{self._max_regions}），拒绝新增 {profile.region_code}")
        self._rows[profile.region_code] = profile

    def get(self, region_code: str) -> SiteProfile | None:
        return self._rows.get(region_code)

    def list(self, *, region_code: str | None = None, limit: int = 200) -> list[SiteProfile]:
        if limit <= 0:
            raise ValueError("limit 必须为正")
        regions = sorted(self._rows) if region_code is None else ([region_code] if region_code in self._rows else [])
        return [self._rows[region] for region in regions[:limit]]

    @property
    def size(self) -> int:
        return len(self._rows)
