"""外部逐小时降水数据源：从本地 CSV 读站点小时降水量，产 `precip_mm_h` 遥测。

**这条腿补的是哪一个空白**：逐日降水腿（`precip_daily`）给的是 24 小时累计——只够喂
规则轨 `R-DEBRIS-RAIN-2`；因子引擎要的 `rainfall_mm_h`（小时雨强）此前**没有任何真实源**
（"日累计不可近似折算"写死在派生表的 UNMAPPED_REASONS 里）。Open-Meteo archive API
（`precipitation`，单位 mm，即 mm/h，许可 CC BY 4.0）让小时口径第一次有了真数据。

**为什么遥测 metric 叫 `precip_mm_h` 而不是直接叫 `rainfall_mm_h`**：平台口径是
"遥测是站点原始量，因子是物理量，换算只发生在派生表"。小时雨强恰好是 scale=1.0 的
恒等派生——也要走同一张表登记（`assess_input.DERIVATIONS`），而不是让连接器绕开口径。
这样 ×6 的教训（日累计误填 rain_10min 被静默放大）对这条腿同样有闸拦着。

**它与逐日腿的分工**：一条腿只给一个口径（同 `precip_daily` 的规矩）。
24h 累计仍归逐日腿——两条腿同时供 `rain_cumulative_24h` 会双计；滚动 24h
如果真要用小时值重算，那是另一条腿的事，不在这条腿上凑。

**数据从哪来**：`scripts/fetch_precip_hourly_openmeteo.py` 按站点表拉 Open-Meteo
archive API（匿名），合并写进运行态 CSV（不进仓库），本腿读那份 CSV。

**口径**：
- 只产**文件里最新一小时**的读数（`observed_at` = 该整点），更早的小时不倒灌；
- 文件最新整点离当前超过 6 小时 → 视为源停更，整轮不产读数（陈年数据冒充当前雨情
  比没数据更危险）；逐日腿的"只取当天"在小时口径下的等价物就是这个滞后上限；
- 缺测跳过；值越出物理不可能线（300 mm/h）标 `suspect`，不静默丢弃。
"""

from __future__ import annotations

import csv
import logging
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from aegis.connectors.base import DataSource
from aegis.connectors.metrics import unit_for
from aegis.connectors.trace import raw_number, source_probe, valid_region
from aegis.domain.messages import TelemetryReading, now_iso
from aegis.errors import SchemaInvalidError

log = logging.getLogger("aegis.connectors.precip_hourly")

#: 这条腿只给这一个 metric——因子名 `rainfall_mm_h` 是它经派生表换算（scale=1.0）后的去处。
METRIC = "precip_mm_h"

#: 小时雨强的物理上限（mm/h），与因子目录 `rainfall_mm_h` 的登记上界同一数。
MAX_HOURLY_MM = 300.0

#: 文件最新整点离当前的最大滞后。超过就整轮不产：源停更 6 小时以上的"实况"不是实况。
MAX_LAG_HOURS = 6

#: CSV 必须有的列。缺列是契约破了（上抛）；行内缺值是单行不合格（跳过计数）。
_REQUIRED_COLUMNS = ("datetime_utc", "station_id", "region_code", "precip_mm_h")


def _parse_utc(raw: str) -> datetime | None:
    """Open-Meteo 的小时戳两种形态都要认：`2026-11-20T14:00`（naive 即 UTC）与带 Z。"""
    text = raw.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment


class PrecipHourlySource(DataSource):
    """从本地 CSV 读站点逐小时降水量，只产最新整点。"""

    name = "precip_hourly"

    def __init__(self, path: Path | str, *, metric: str = METRIC) -> None:
        self._path = Path(path)
        self._metric = metric
        self.rounds = 0
        self.readings = 0
        self.failures = 0
        self.skipped = 0
        self.stale_rounds = 0
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
            probe.inputs.update(kind="csv", file=self._path.name, metric=self._metric, raw_unit="rows", max_lag_hours=MAX_LAG_HOURS)
        if not self.enabled:
            if probe is not None:
                probe.note("file_missing", "小时降水 CSV 不可读或已移除")
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
        """给状态行的事实：读到几行、跳过几条、源停更几轮、最后一次错在哪。"""
        return {
            "rounds": self.rounds,
            "readings": self.readings,
            "failures": self.failures,
            "skipped": self.skipped,
            "stale_rounds": self.stale_rounds,
            "last_error": self.last_error,
            "metric": self._metric,
            "file": self._path.name,
        }

    def _read(self) -> list[TelemetryReading]:
        probe = source_probe.get()
        if probe is not None:
            probe.capture(0)
        rows: list[tuple[datetime, dict[str, str]]] = []
        with self._path.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            missing = [column for column in _REQUIRED_COLUMNS if column not in (reader.fieldnames or [])]
            if missing:
                # 表头缺列是**契约破了**（上游改了取数格式），不是"这批没数"——上抛。
                raise SchemaInvalidError(
                    "小时降水 CSV 缺列",
                    detail={"missing": missing, "columns": list(reader.fieldnames or [])},
                )
            for row in reader:
                if probe is not None:
                    probe.capture(
                        observed_at=row.get("datetime_utc"), station_id=row.get("station_id"),
                        region_code=row.get("region_code"), value=raw_number(row.get("precip_mm_h")),
                    )
                moment = _parse_utc(row.get("datetime_utc") or "")
                if moment is None:
                    self.skipped += 1
                    if probe is not None:
                        probe.note("invalid_time", "小时降水行的观测时间无效", observed_at=row.get("datetime_utc"))
                    continue
                rows.append((moment, row))
        if not rows:
            return []
        latest = max(moment for moment, _row in rows)
        if probe is not None:
            probe.inputs["latest_observed_at"] = latest.isoformat()
        if datetime.now(UTC) - latest > timedelta(hours=MAX_LAG_HOURS):
            # 源停更：如实整轮不产（计数），绝不把上周的雨当现在的。
            self.stale_rounds += 1
            if probe is not None:
                probe.note("stale_source", "最新整点超出允许滞后时间，本轮停止出数", observed_at=latest.isoformat())
            log.warning("小时降水源停更：最新整点 %s 超过 %dh，本轮不产读数", latest.isoformat(), MAX_LAG_HOURS)
            return []
        readings: list[TelemetryReading] = []
        history = 0
        for moment, row in rows:
            if moment != latest:
                # 只取最新整点：遥测库是运行态事实，倒灌历史小时会污染触发判据。
                self.skipped += 1
                history += 1
                continue
            reading = self._to_reading(moment, row)
            if reading is not None:
                readings.append(reading)
            elif probe is not None:
                probe.note(
                    "invalid_row", "小时降水行缺测或字段格式有误",
                    station_id=row.get("station_id"), region_code=row.get("region_code"), value=raw_number(row.get("precip_mm_h")),
                )
        if probe is not None:
            probe.inputs["filtered_history"] = history
        return readings

    def _to_reading(self, moment: datetime, row: dict[str, str]) -> TelemetryReading | None:
        station_id = (row.get("station_id") or "").strip()
        region_code = (row.get("region_code") or "").strip().upper()
        raw_value = (row.get("precip_mm_h") or "").strip()
        if not station_id:
            self.skipped += 1
            return None
        if not raw_value:
            # 缺测：跳过，不补、不插值。补出来的"真值"会一路走进因子分与预警正文。
            self.skipped += 1
            return None
        try:
            value = float(raw_value)
        except ValueError:
            self.skipped += 1
            return None
        quality = "ok" if 0.0 <= value <= MAX_HOURLY_MM else "suspect"
        if not valid_region(region_code[:24]) or not math.isfinite(value):
            # 区划码不合约定（`^[0-9A-Z]{6,24}$`）的读数进不去——如实跳过并计数。
            self.skipped += 1
            return None
        return TelemetryReading(
            station_id=station_id,
            metric=self._metric,
            value=value,
            unit=unit_for(self._metric),
            region_code=region_code[:24],
            observed_at=moment.isoformat().replace("+00:00", "Z"),
            ingested_at=now_iso(),
            source=self.name,
            quality_flag=quality,
        )


__all__ = ["MAX_HOURLY_MM", "MAX_LAG_HOURS", "METRIC", "PrecipHourlySource"]
