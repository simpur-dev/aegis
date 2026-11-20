"""外部逐日降水腿：本地派生表 → 平台遥测。

装配规则与其它腿一致：**默认关**。填了 `AEGIS_PRECIP_DAILY_FILE`（且文件存在）才进摄取轮次；
关着时状态面报 `driver=off`，运维一眼看出这条腿在不在。

它提供的是平台第一个**真实**雨量来源（外部公开数据目录里五套都是灾情/普查/点位，
唯独触发因子缺）。口径与边界见 `aegis/connectors/precip_daily.py` 的模块注释。
"""

from __future__ import annotations

from pathlib import Path

from aegis.config import Settings, get_settings
from aegis.connectors.precip_daily import PrecipDailySource
from aegis.integrations.state import IntegrationState


def build_precip(
    settings: Settings | None = None,
) -> tuple[PrecipDailySource | None, IntegrationState]:
    """装配外部逐日降水腿：只在填了文件路径且文件在位时才有这条腿。

    文件路径由运营方给出（派生表由 `scripts/derive_precip_station_daily.py` 产出，
    落在运行态目录，不进仓库），所以状态面只报文件名与行数，不报绝对路径——
    绝对路径会把部署目录结构带进对外接口。
    """
    cfg = settings or get_settings()
    raw = (cfg.precip_daily_file or "").strip()
    if not raw or not cfg.precip_enabled:
        return None, IntegrationState(name="precip", enabled=False, driver="off")

    path = Path(raw)
    if not path.exists():
        # 填了路径但文件不在 → 如实说"这条腿不存在"，不静默当成没数据。
        return None, IntegrationState(
            name="precip",
            enabled=False,
            driver="off",
            detail={"error": "派生表文件不存在", "file": path.name},
        )

    source = PrecipDailySource(path)
    return source, IntegrationState(
        name="precip",
        enabled=True,
        driver="csv",
        detail={"file": path.name, "metric": source.metric},
    )


__all__ = ["build_precip"]
