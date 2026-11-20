"""外部逐小时降水腿：Open-Meteo 取数表 → 平台遥测（因子引擎的 `rainfall_mm_h` 从这来）。

装配规则与其它腿一致：**默认关**。填了 `AEGIS_PRECIP_HOURLY_FILE`（且文件存在）才进摄取轮次；
关着时状态面报 `driver=off`，运维一眼看出这条腿在不在。

它给的是**小时口径**（`precip_mm_h` → 派生表恒等换算成因子 `rainfall_mm_h`）；
24h 累计归逐日降水腿（`integrations/precip.py`），两条腿一个口径一件事，不重叠。
取数脚本与口径边界见 `aegis/connectors/precip_hourly.py` 的模块注释。
"""

from __future__ import annotations

from pathlib import Path

from aegis.config import Settings, get_settings
from aegis.connectors.precip_hourly import PrecipHourlySource
from aegis.integrations.state import IntegrationState


def build_precip_hourly(
    settings: Settings | None = None,
) -> tuple[PrecipHourlySource | None, IntegrationState]:
    """装配外部逐小时降水腿：只在填了文件路径且文件在位时才有这条腿。"""
    cfg = settings or get_settings()
    raw = (cfg.precip_hourly_file or "").strip()
    if not raw or not cfg.precip_hourly_enabled:
        return None, IntegrationState(name="precip_hourly", enabled=False, driver="off")

    path = Path(raw)
    if not path.exists():
        # 填了路径但文件不在 → 如实说"这条腿不存在"，不静默当成没数据。
        return None, IntegrationState(
            name="precip_hourly",
            enabled=False,
            driver="off",
            detail={"error": "小时降水取数文件不存在", "file": path.name},
        )

    source = PrecipHourlySource(path)
    return source, IntegrationState(
        name="precip_hourly",
        enabled=True,
        driver="csv",
        detail={"file": path.name, "metric": source.metric},
    )


__all__ = ["build_precip_hourly"]
