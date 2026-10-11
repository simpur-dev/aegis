"""真智能体运行时的装配点（§3 / §6 MA"五智能体 API 版"）：把已建内环按【真依赖】挂起来。

`build_real_agents` 只负责构造，注入什么留给调用方——这条把"生产该注什么"一次说清：
快腿越限用平台规则引擎（阈值单一真源）、快腿漂移用规则表反查灾种的 EWMA、语义腿委派平台
三路融合解析器、研判佐证走 REST。缺哪个依赖就【如实缺位】（内环自己降级），不塞会谎报的替身。

execute 只产文案：装配进来的 execute 内环不持发送权，触达证据由平台交付腿的真回执唯一提供
（§3.4/铁律 7）。构造的是五个内环，不是四个。
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from aegis.agents.factory import build_real_agents
from aegis.agents.perceive import EwmaDriftDetector, StationAnomaly
from aegis.agents.rest_context import RestAssessContext
from aegis.bus.transport import BusTransport
from aegis.domain.messages import TelemetryReading
from aegis.services.trigger_rules import RuleEngine


class RuleEngineDetector:
    """快腿第一判据（绝对阈值越限）：直接问平台 `RuleEngine`，本模块不持有任何阈值。

    一次读数单独喂进去，命中即成一条 `StationAnomaly`：score 用规则给的，灾种用规则给的，
    这里不改尺度也不补权重——否则"规则引擎说 0.8"与"perceive 报 0.9"就成了两张皮。
    读数本身不合 `TelemetryReading` 形状时返回 None（判不了就沉默，不猜一个越限）。
    """

    def __init__(self, engine: RuleEngine) -> None:
        self._engine = engine

    def flag(self, reading: dict[str, Any]) -> StationAnomaly | None:
        try:
            parsed = TelemetryReading.model_validate(reading)
        except (ValidationError, TypeError):
            return None
        hits = self._engine.evaluate([parsed], region_code=parsed.region_code).hits
        if not hits:
            return None
        hit = max(hits, key=lambda item: item.score)
        return StationAnomaly(
            station_id=parsed.station_id,
            region_code=str(hit.region_code or parsed.region_code),
            hazard_type=str(hit.hazard_type),
            value=parsed.value,
            unit=parsed.unit,
            observed_at=parsed.observed_at,
            base_score=float(hit.score),
        )


class _CompositeDetector:
    """越限优先、漂移补位：同一条读数先问硬阈值，没碰阈值再问该站点自身常态。

    为什么不是"两个都报"：一条已经越限的读数若同时被 EWMA 也算异常，同一站点会产出两条命中，
    聚集腿把增益重复计一次——分数虚高，而这两条根本不是独立证据。
    """

    def __init__(self, threshold: RuleEngineDetector, drift: EwmaDriftDetector) -> None:
        self.threshold = threshold
        self.drift = drift

    def flag(self, reading: dict[str, Any]) -> StationAnomaly | None:
        hit = self.threshold.flag(reading)
        return hit if hit is not None else self.drift.flag(reading)

    def baseline_for(self, station_id: str, metric: str) -> tuple[float, float, int] | None:
        """把漂移腿的自身常态基线透出去：报表与联调要能回答"这台仪器自己认为常态是多少"。"""
        return self.drift.baseline_for(station_id, metric)


def build_agent_runtime(
    transport: BusTransport,
    *,
    rule_engine: RuleEngine,
    base_url: str,
    llm: Any | None = None,
    memory: Any | None = None,
    parser: Any | None = None,
    contracts: Any | None = None,
    service_token: str = "",
    id_suffix: str = "real01",
) -> list[Any]:
    """构造五智能体运行时（不启动；启动交给 `factory.start_real_agents`）。"""
    ewma = EwmaDriftDetector(rules=rule_engine.rules)
    perceive_detector = _CompositeDetector(RuleEngineDetector(rule_engine), ewma)
    return build_real_agents(
        transport,
        llm=llm,
        memory=memory,
        context=RestAssessContext(base_url=base_url, token=service_token),
        detector=perceive_detector,
        semantic=parser,
        contracts=contracts,
        id_suffix=id_suffix,
    )
