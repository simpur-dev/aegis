"""演练腿（模拟站）：**默认关**；开着时按 surge 场景自产读数，会越过规则轨的降雨阈值。

这条腿不是"数据源"而是"演练发生器"：它每轮造一批读数喂给摄取链路，让五段链路在没有
真实站端的情况下也能跑起来。代价必须说清楚——surge 场景的 `rain_10min` 落在 32–55 mm/10min，
越过 `R-DEBRIS-RAIN-1`（`rain_10min >= 30.0`）的阈值，所以**开着它就是让平台自己给自己发
橙色预警**。因此它：

1. **代码默认关**（`Settings.simulator_enabled` 默认 False）——默认形态不该自带一个持续报警源；
2. **状态面有一行**（`GET /api/v1/integrations` 的 `simulator` 行）——运维要能一眼读出
   "演练腿开着还是关着"，而不是从"预警突然变多"倒推；
3. **生产环境启动期响亮警告**——不拒绝启动（带演练数据部署做演练验收是正当用法），
   但必须在那行日志里说破，别让它混进真告警堆里。

演示档要开着它，走 `scripts/dev.mjs`（本地编排会显式注入 `AEGIS_SIMULATOR_ENABLED=true`），
不要靠改全局默认。
"""

from __future__ import annotations

from aegis.config import Settings, get_settings
from aegis.connectors.simulator import HazardScenarioSimulator
from aegis.integrations.state import IntegrationState


def build_simulator(
    settings: Settings | None = None,
    *,
    enabled: bool | None = None,
) -> tuple[HazardScenarioSimulator | None, IntegrationState]:
    """装配演练腿：默认关。

    `enabled` 形参优先于配置项（`None` 表示"听配置的"）——与容器构造的 `with_simulator`
    同一语义：用例可以显式开或显式关，不必去改环境变量。

    状态面这一行的 `detail` 只带**场景与节拍**，不带随机数序列：surge 与 normal 的差别
    是运维必须知道的（surge 会越过降雨阈值），而随机种子已经在配置里另有一处登记。
    """
    cfg = settings or get_settings()
    use = cfg.simulator_enabled if enabled is None else enabled
    if not use:
        return None, IntegrationState(name="simulator", enabled=False, driver="off")

    source = HazardScenarioSimulator(seed=cfg.simulator_seed)
    return source, IntegrationState(
        name="simulator",
        enabled=True,
        driver="simulator",
        detail={
            "scenario": source.scenario,
            "interval_seconds": cfg.simulator_interval_seconds,
            "seed": cfg.simulator_seed,
        },
    )


__all__ = ["build_simulator"]
