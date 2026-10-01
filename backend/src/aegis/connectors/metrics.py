"""监测量的公共口径：指标名 → 单位。

这张表是"多源接入"三腿（模拟站 / 气象 API / MQTT 站端推送）唯一的单位真源。此前每个连接器
各带一份完全相同的字典，加一个观测量就得改三处——漏改一处不会报错，只会让落库的单位列静默
不一致，而下游阈值规则与报告都直接读这一列。
"""

from __future__ import annotations

from collections.abc import Mapping

METRIC_UNITS: Mapping[str, str] = {
    "rain_10min": "mm",
    "rain_cumulative_24h": "mm",
    "debris_level": "m",
    "displacement_mm": "mm",
    "lake_level_m": "m",
    "dam_seepage_turbidity_ntu": "NTU",
    "new_snow_cm": "cm",
    "wind_speed_ms": "m/s",
    "air_temperature_c": "C",
    "snow_water_equivalent_mm": "mm",
    "crack_aperture_mm": "mm",
    "freeze_thaw_cycles": "count",
}

KNOWN_METRICS: frozenset[str] = frozenset(METRIC_UNITS)

# 未登记指标仍要能接入（现场会增加新传感器），但单位只能是这个显式的未知标记：
# 空串会让下游把"不知道单位"误读成"无量纲"。
UNKNOWN_UNIT = "unknown"


def unit_for(metric: str) -> str:
    return METRIC_UNITS.get(metric, UNKNOWN_UNIT)
