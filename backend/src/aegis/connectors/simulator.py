"""高原监测场景模拟器：为无真实物联网的开发/压测/演示阶段提供可复现数据。

设计约束：
- 结果按 seed 完全确定，压测与指标复算可重复；
- surge 场景在数轮内把泥石流/滑坡触发条件推到阈值以上，normal 场景保持不触发——
  两者共同构成端到端测试的正/负样本（对应"多灾种预警准确率"的评测口径）。
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from aegis.connectors.base import DataSource
from aegis.domain.messages import TelemetryReading, now_iso, utc_now

# (station_id, region_code, 指标集)
STATIONS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("RG-540121-01", "540121", ("rain_10min", "rain_cumulative_24h", "debris_level")),
    ("GNS-540121-02", "540121", ("displacement_mm",)),
    ("RG-540221-01", "540221", ("rain_10min", "rain_cumulative_24h")),
    ("LKS-540221-03", "540221", ("lake_level_m", "dam_seepage_turbidity_ntu")),
    ("SNW-540321-01", "540321", ("new_snow_cm", "wind_speed_ms", "air_temperature_c", "snow_water_equivalent_mm")),
    ("RCK-540321-02", "540321", ("crack_aperture_mm", "freeze_thaw_cycles")),
)

_UNITS = {
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

# 正常基线与激增上限
_BASELINE = {
    "rain_10min": (0.2, 3.0),
    "rain_cumulative_24h": (4.0, 20.0),
    "debris_level": (0.0, 0.2),
    "displacement_mm": (0.0, 1.5),
    "lake_level_m": (0.0, 0.05),
    "dam_seepage_turbidity_ntu": (2.0, 12.0),
    "new_snow_cm": (0.0, 4.0),
    "wind_speed_ms": (2.0, 8.0),
    "air_temperature_c": (-12.0, -2.0),
    "snow_water_equivalent_mm": (10.0, 25.0),
    "crack_aperture_mm": (1.0, 6.0),
    "freeze_thaw_cycles": (0.0, 1.0),
}
_SURGE = {
    "rain_10min": (32.0, 55.0),
    "rain_cumulative_24h": (85.0, 130.0),
    "debris_level": (1.1, 2.4),
    "displacement_mm": (22.0, 48.0),
    "lake_level_m": (0.55, 1.2),
    "dam_seepage_turbidity_ntu": (55.0, 140.0),
    "new_snow_cm": (28.0, 60.0),
    "wind_speed_ms": (15.0, 26.0),
    "air_temperature_c": (2.5, 9.0),
    "snow_water_equivalent_mm": (45.0, 90.0),
    "crack_aperture_mm": (16.0, 40.0),
    "freeze_thaw_cycles": (3.5, 8.0),
}


@dataclass
class HazardScenarioSimulator(DataSource):
    name: str = "sim_field_nodes"
    scenario: str = "surge"  # surge | normal
    seed: int = 202_609_29
    tick: int = 0
    suspect_rate: float = 0.05  # 模拟传感器劣化读数，验证质量标记过滤
    _rng: random.Random = field(default=None, repr=False)  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.scenario not in ("surge", "normal"):
            raise ValueError(f"未知场景: {self.scenario}")
        if not 0.0 <= self.suspect_rate <= 1.0:
            raise ValueError("suspect_rate 取值范围 [0,1]")
        self._rng = random.Random(self.seed)

    @property
    def station_count(self) -> int:
        return len(STATIONS)

    async def collect(self) -> list[TelemetryReading]:
        return self.collect_at(utc_now())

    def collect_at(self, moment) -> list[TelemetryReading]:
        """按给定时刻产出一轮读数（纯函数化，便于边界测试与回放）。"""
        observed = moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        band = _SURGE if self.scenario == "surge" else _BASELINE
        readings: list[TelemetryReading] = []
        for station_id, region_code, metrics in STATIONS:
            for metric in metrics:
                low, high = band[metric]
                value = self._rng.uniform(low, high)
                if self.scenario == "surge" and self.tick > 0:
                    value *= 1.0 + min(self.tick, 5) * 0.02  # 逐轮抬升，模拟灾害演化
                quality = "ok"
                if self._rng.random() < self.suspect_rate:
                    quality, value = "suspect", value * 10  # 劣化读数明显离群
                readings.append(
                    TelemetryReading(
                        station_id=station_id,
                        metric=metric,
                        value=round(value, 3),
                        unit=_UNITS.get(metric, "unit"),
                        region_code=region_code,
                        observed_at=observed,
                        ingested_at=now_iso(),
                        source="simulator",
                        quality_flag=quality,
                    )
                )
        self.tick += 1
        return readings
