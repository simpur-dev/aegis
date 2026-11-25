"""外部逐日降水数据源：从本地 CSV 读站点逐日降水量，产 `rain_cumulative_24h` 遥测。

**这条腿提供的是什么**：平台第一个**真实**雨量来源。外部公开数据目录里五套数据都是
灾情/普查/点位，唯独触发因子（降雨）缺——这条腿把那一块接上。

**它为什么只给 `rain_cumulative_24h`、不给 `rainfall_mm_h`**：逐日降水量是 24 小时累计，
小时雨强是另一个口径（另一个 metric `rain_10min`）。两者不可近似——平台派生表里对
`rain_cumulative_24h` 有明确的不映射处置（`assess_input.UNMAPPED_REASONS`：
"24h 累计与小时雨强口径不同，禁止近似"）。所以：

- **规则轨用得上**：`R-DEBRIS-RAIN-2`（`rain_cumulative_24h >= 80` 且 `debris_level >= 1` → 红）
  的口径正好是 24 小时累计；
- **因子引擎用不上**（`rainfall_mm_h`）：缺的是 10 分钟节拍的雨量，那要站端设备或气象 API，
  不是格点日值能给的。这条腿**不假装**自己补上了小时雨强。

**数据从哪来**：`scripts/fetch_precip_chmpre.py` 取 CHM_PRE V2 原件（运行态目录），
`scripts/derive_precip_station_daily.py` 派生成站点逐日 CSV，本腿读那份 CSV。
原件与派生都不进仓库（46.8 GB 与逐日表），平台只读派生表。

**口径**：
- 只产**当天**的读数（`observed_at` = 数据日期），历史日期不倒灌——遥测库是运行态事实，
  把三个月前的日值当成今天的读数会污染触发判据；
- 缺测（CSV 里为空）跳过，不补、不插值；
- 值域越界的读数标 `suspect`（交给下游剔），不静默丢弃。
"""

from __future__ import annotations

import csv
import logging
import math
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from aegis.connectors.base import DataSource
from aegis.connectors.metrics import unit_for
from aegis.connectors.trace import raw_number, source_probe, valid_region
from aegis.domain.messages import TelemetryReading, now_iso
from aegis.errors import SchemaInvalidError

log = logging.getLogger("aegis.connectors.precip")

#: 这条腿只给这一个 metric——口径不同的事不在这条腿上凑。
METRIC = "rain_cumulative_24h"

#: 逐日降水量的物理上限（mm/24h）。超过就是坏数据，不进平台。
MAX_DAILY_MM = 1_500.0

#: CSV 必须有的列。缺列是契约破了（上抛）；行内缺值是单行不合格（跳过计数）。
_REQUIRED_COLUMNS = ("date", "station_id", "region_code", "precip_mm")


class PrecipDailySource(DataSource):
    """从本地 CSV 读站点逐日降水量。"""

    name = "precip_daily"

    def __init__(self, path: Path | str, *, metric: str = METRIC) -> None:
        self._path = Path(path)
        self._metric = metric
        self.rounds = 0
        self.readings = 0
        self.failures = 0
        self.skipped = 0
        self.last_error: str | None = None

    @property
    def enabled(self) -> bool:
        return self._path.exists()

    @property
    def metric(self) -> str:
        """这条腿产出的 metric 名（状态面要报出来：它只给一个口径）。"""
        return self._metric

    @property
    def path(self) -> Path:
        return self._path

    async def collect(self) -> list[TelemetryReading]:
        probe = source_probe.get()
        if probe is not None:
            probe.inputs.update(kind="csv", file=self._path.name, metric=self._metric, raw_unit="rows")
        if not self.enabled:
            if probe is not None:
                probe.note("file_missing", "降水 CSV 不可读或已移除")
            return []
        try:
            readings = self._read()
        except Exception as exc:
            # 计数与上抛同时发生：吞掉错误会让"数据文件坏了"变成"平台静默少一批雨量"
            self.failures += 1
            self.last_error = f"{type(exc).__name__}: {exc}"[:240]
            raise
        self.rounds += 1
        self.readings += len(readings)
        return readings

    def status(self) -> dict[str, Any]:
        """给状态行的事实：读到几行、跳过几条、最后一次错在哪。"""
        return {
            "rounds": self.rounds,
            "readings": self.readings,
            "failures": self.failures,
            "skipped": self.skipped,
            "last_error": self.last_error,
            "metric": self._metric,
            "file": self._path.name,
        }

    def _read(self) -> list[TelemetryReading]:
        # "当天"按 UTC：派生表的日期列按 UTC epoch 解码（derive 脚本同口径），本地时区
        # 的 date.today() 在 UTC+8 的 00:00–08:00 会超前一天，把 UTC 当日行整批判"非当天"。
        today = datetime.now(UTC).date()
        probe = source_probe.get()
        history = 0
        if probe is not None:
            probe.inputs["filter_date"] = today.isoformat()
            probe.capture(0)
        readings: list[TelemetryReading] = []
        with self._path.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            missing = [column for column in _REQUIRED_COLUMNS if column not in (reader.fieldnames or [])]
            if missing:
                # 表头缺列是**契约破了**（上游改了派生格式），不是"这批没数"——上抛。
                raise SchemaInvalidError(
                    "降水 CSV 缺列",
                    detail={"missing": missing, "columns": list(reader.fieldnames or [])},
                )
            for row in reader:
                sample = {
                    "date": row.get("date"), "station_id": row.get("station_id"),
                    "region_code": row.get("region_code"), "value": raw_number(row.get("precip_mm")),
                }
                if probe is not None:
                    probe.capture(**sample)
                reading = self._to_reading(row, today)
                if reading is None:
                    try:
                        other_day = date.fromisoformat((row.get("date") or "").strip()) != today
                    except ValueError:
                        other_day = False
                    if other_day:
                        history += 1
                    elif probe is not None:
                        probe.note("invalid_row", "逐日降水行缺测或字段格式有误", **sample)
                    continue
                readings.append(reading)
        if probe is not None:
            probe.inputs["filtered_history"] = history
        return readings

    def _to_reading(self, row: dict[str, str], today: date) -> TelemetryReading | None:
        station_id = (row.get("station_id") or "").strip()
        region_code = (row.get("region_code") or "").strip().upper()
        raw_date = (row.get("date") or "").strip()
        raw_value = (row.get("precip_mm") or "").strip()
        if not station_id or not raw_date:
            self.skipped += 1
            return None
        if not raw_value:
            # 缺测：跳过，不补、不插值。补出来的"真值"会一路走到预警里。
            self.skipped += 1
            return None
        try:
            value = float(raw_value)
            observed_date = date.fromisoformat(raw_date)
        except ValueError:
            self.skipped += 1
            return None
        if observed_date != today:
            # 只取当天的读数：遥测库是运行态事实，倒灌历史日值会污染触发判据。
            self.skipped += 1
            return None
        quality = "ok" if 0.0 <= value <= MAX_DAILY_MM else "suspect"
        if not valid_region(region_code[:24]) or not math.isfinite(value):
            # 区划码不合约定（`^[0-9A-Z]{6,24}$`）的读数进不去——如实跳过并计数。
            self.skipped += 1
            return None
        observed = datetime.combine(observed_date, datetime.min.time(), tzinfo=UTC)
        return TelemetryReading(
            station_id=station_id,
            metric=self._metric,
            value=value,
            unit=unit_for(self._metric),
            region_code=region_code[:24],
            observed_at=observed.isoformat().replace("+00:00", "Z"),
            ingested_at=now_iso(),
            source=self.name,
            quality_flag=quality,
        )


def yesterday_iso() -> str:
    """昨天的 UTC 日期（派生脚本与排程用得上）。"""
    return (datetime.now(UTC).date() - timedelta(days=1)).isoformat()


__all__ = ["MAX_DAILY_MM", "METRIC", "PrecipDailySource"]
