"""区域级站点画像：静态/慢变的孕灾属性（地形、沟道、湖体、雪深…）。

与遥测的分界：动态触发量（雨量、位移速率、湖水位）只能来自观测，不得写进这里；
画像只承载"短期内不会变、变了要重新勘测"的事实，并逐条带来源与勘测时间。
"""

from __future__ import annotations

import math

from pydantic import BaseModel, ConfigDict, Field, model_validator

from aegis.domain.enums import HazardType
from aegis.domain.provenance import DataProvenance


class SiteAttribute(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    key: str = Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=64)
    value: float | str
    unit: str = Field(default="", max_length=24)
    source: str = Field(default="unknown", max_length=64)
    surveyed_at: str | None = Field(default=None, max_length=40)

    @model_validator(mode="after")
    def _value_shape(self) -> SiteAttribute:
        if isinstance(self.value, bool) or not isinstance(self.value, (int, float, str)):
            raise ValueError("属性值必须是数值或字符串")
        if isinstance(self.value, float) and not math.isfinite(self.value):
            raise ValueError("属性值必须有限")
        if isinstance(self.value, str) and len(self.value) > 32:
            raise ValueError("字符串属性值不得超过 32 字符")
        return self


class SiteProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    region_code: str = Field(pattern=r"^[0-9A-Z]{6,24}$")
    name: str = Field(default="", max_length=120)
    hazard_types: list[HazardType] = Field(default_factory=list, max_length=7)
    attributes: list[SiteAttribute] = Field(default_factory=list, max_length=96)
    provenance: DataProvenance = Field(default_factory=DataProvenance)

    @model_validator(mode="after")
    def _unique_keys(self) -> SiteProfile:
        seen: set[str] = set()
        for attr in self.attributes:
            if attr.key in seen:
                raise ValueError(f"属性 key 重复：{attr.key}")
            seen.add(attr.key)
        return self

    def value_of(self, key: str) -> float | str | None:
        for attr in self.attributes:
            if attr.key == key:
                return attr.value
        return None


__all__ = ["SiteAttribute", "SiteProfile"]
