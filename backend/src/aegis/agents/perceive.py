"""perceive 感知智能体内环（§3.1）：快腿越限（复用平台规则引擎，经注入 detector）+ 时空聚集弱信号增强。

诚实边界：
- 越限判据【不另立阈值】——经注入的 `AnomalyDetector` 复用平台 `trigger_rules`（单一真源），
  生产由装配点把 RuleEngine 包成 detector 注入；缺位则本内环不臆造站点异常。
- 本智能体真正的新价值是 **时空聚集性分析**（论文 §3.2.2）：同区域、同灾种、时间窗内多站点
  同步异常 → 聚合成一条更强的 `perceive.trigger_hit`（弱信号增强），evidence_refs 溯源到各站点读数。
- 聚集参数（最少站数、每站增益、时间窗）为**未实测初值**，联调后按真数据标定，不据此声称达标。

产出与平台消费口径一致：`detect()` 返回的每条命中都是 `TriggerHit` 可校验的 dict
（链路的 handle_agent_message 正用 `TriggerHit.model_validate(payload)` 收 trigger_hit 事件）。
作为独立内环实现，经 emit_event 挂总线、被 _drain_agent_hits 合并的集成属 MB。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from aegis.agents.agentkit import AgentKit, Reply
from aegis.domain.messages import AgentMessage, parse_iso


@dataclass(slots=True)
class StationAnomaly:
    station_id: str
    region_code: str
    hazard_type: str
    value: float
    unit: str
    observed_at: str
    base_score: float


@runtime_checkable
class AnomalyDetector(Protocol):
    """把平台越限/EWMA 判据抽象成窄接口（生产注入 RuleEngine 包装）；非异常返回 None。"""

    def flag(self, reading: dict[str, Any]) -> StationAnomaly | None: ...


@dataclass
class PerceiveAgent(AgentKit):
    detector: AnomalyDetector | None = None
    #: 时空聚集参数（未实测初值，联调标定）：最少几个同步站点算聚集、每多一站增益、判定同窗的秒数。
    min_stations: int = 2
    gain_per_station: float = 0.1
    window_seconds: int = 600

    async def handle(self, request: AgentMessage) -> Reply:
        readings = request.payload.get("readings") or []
        hits = self.detect(list(readings))
        return Reply(payload={"anomaly_count": len(hits), "trigger_hits": hits})

    def detect(self, readings: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """逐站点越限（复用 detector）→ 按 (区域, 灾种) 时空聚集 → 产出 TriggerHit 兼容命中。"""
        if self.detector is None:
            return []
        anomalies = [a for a in (self.detector.flag(r) for r in readings) if a is not None]
        groups: dict[tuple[str, str], list[StationAnomaly]] = {}
        for a in anomalies:
            groups.setdefault((a.region_code, a.hazard_type), []).append(a)

        hits: list[dict[str, Any]] = []
        for (region, hazard), items in groups.items():
            distinct = {a.station_id for a in items}
            if len(distinct) >= self.min_stations and self._within_window(items):
                hits.append(self._cluster_hit(region, hazard, items, distinct))
            else:
                hits.extend(self._single_hit(a) for a in items)
        return hits

    def _within_window(self, items: list[StationAnomaly]) -> bool:
        moments = [self._parse(a.observed_at) for a in items]
        if any(m is None for m in moments):
            return False
        stamps = [m for m in moments if m is not None]
        return (max(stamps) - min(stamps)).total_seconds() <= self.window_seconds

    def _cluster_hit(self, region: str, hazard: str, items: list[StationAnomaly], distinct: set[str]) -> dict[str, Any]:
        base_max = max(a.base_score for a in items)
        score = min(1.0, base_max + self.gain_per_station * (len(distinct) - 1))
        evidence = [f"station={a.station_id}@{a.observed_at}={a.value}{a.unit}" for a in items]
        latest = max((a.observed_at for a in items), key=lambda t: self._parse(t) or _EPOCH)
        return {
            "rule_id": f"R-SPATIOTEMPORAL-{region}-{hazard}",
            "hazard_type": hazard,
            "region_code": region,
            "score": round(score, 4),
            "evidence_refs": evidence[:32],
            "observed_at": latest,
        }

    @staticmethod
    def _single_hit(a: StationAnomaly) -> dict[str, Any]:
        return {
            "rule_id": f"R-ANOMALY-{a.station_id}",
            "hazard_type": a.hazard_type,
            "region_code": a.region_code,
            "score": round(max(0.0, min(1.0, a.base_score)), 4),
            "evidence_refs": [f"station={a.station_id}@{a.observed_at}={a.value}{a.unit}"],
            "observed_at": a.observed_at,
        }

    @staticmethod
    def _parse(ts: str) -> datetime | None:
        try:
            return parse_iso(ts)
        except Exception:
            return None


_EPOCH = datetime.fromtimestamp(0)
