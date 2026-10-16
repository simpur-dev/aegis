"""perceive 感知智能体内环（§3.1）：快腿越限（复用平台规则引擎，经注入 detector）+ 时空聚集弱信号增强。

诚实边界：
- 越限判据【不另立阈值】——经注入的 `AnomalyDetector` 复用平台 `trigger_rules`（单一真源），
  生产由装配点把 RuleEngine 包成 detector 注入；缺位则本内环不臆造站点异常。
- 本智能体真正的新价值是 **时空聚集性分析**（论文 §3.2.2）：同区域、同灾种、时间窗内多站点
  同步异常 → 聚合成一条更强的 `perceive.trigger_hit`（弱信号增强），evidence_refs 溯源到各站点读数。
- 聚集参数（最少站数、每站增益、时间窗）为**未实测初值**，联调后按真数据标定，不据此声称达标。

快腿的第二判据 `EwmaDriftDetector`（§3.1 的"EWMA 越限"位）在本模块内实现：它抓的是
"还没到硬阈值、但已偏离该站点自身常态"的弱信号，与绝对阈值越限互补而不另立判据——
灾种归属从注入的规则表里读，规则表若把同一指标指向多个灾种就不猜（详见该类 docstring）。
语义腿（§3.1 的文本线索）经注入的 `SemanticParser` 委派给平台自己的三路融合解析器，
本内环不复做融合；缺解析器或解析器异常都如实降级并把"建议人工核签"外显，不拿猜测冒充抽取。
装配现状：`start_real_agents` 尚无生产调用点（MA 上总线属集成步），本内环目前只由单测覆盖。

产出与平台消费口径一致：`detect()` 返回的每条命中都是 `TriggerHit` 可校验的 dict
（链路的 handle_agent_message 正用 `TriggerHit.model_validate(payload)` 收 trigger_hit 事件）。
作为独立内环实现，经 emit_event 挂总线、被 _drain_agent_hits 合并的集成属 MB。
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from aegis.agents.agentkit import AgentKit, Reply
from aegis.domain.messages import AgentMessage, parse_iso
from aegis.services.rules.trigger_rules import Rule


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


@runtime_checkable
class SemanticParser(Protocol):
    """语义腿的窄接口——生产注入平台自己的 `services.semantic_parser.DisasterTextParser`。

    §3.1 要的是"上报文本/图像的灾种线索抽取，低置信走平台三路融合口径"。三路融合（规则/案例/
    LLM 与证据腿）已经在 `DisasterTextParser` 里实现并被上报腿（`ReportIntake.submit`）使用，智能体内环
    【不另立一套融合】——否则同一份文本会有两套结论，正是"两张皮"。这里只做委派与如实外显：
    把解析结果的 `decided_by`/`confidence`/`needs_review`/`degradations` 原样带下去。
    """

    async def parse(self, text: str, *, region_code: str | None = None, hazard_hint: str | None = None) -> Any: ...


@dataclass
class PerceiveAgent(AgentKit):
    detector: AnomalyDetector | None = None
    semantic: SemanticParser | None = None
    #: 时空聚集参数（未实测初值，联调标定）：最少几个同步站点算聚集、每多一站增益、判定同窗的秒数。
    min_stations: int = 2
    gain_per_station: float = 0.1
    window_seconds: int = 600
    #: 语义腿置信度低于此值就建议人工核签（§3.2 门限初值，未实测）。
    human_review_below: float = 0.6

    async def handle(self, request: AgentMessage) -> Reply:
        readings = request.payload.get("readings") or []
        fast_hits = self.detect(list(readings))
        semantic = await self._semantic_leg(request.payload)
        hits = [*fast_hits, *semantic.pop("hits")]
        return Reply(payload={"anomaly_count": len(fast_hits), "trigger_hits": hits, "semantic": semantic})

    async def _semantic_leg(self, payload: dict[str, Any]) -> dict[str, Any]:
        """文本线索腿：委派给注入的融合解析器；缺位或异常都【显式降级】，不拿猜测冒充抽取结果。

        图像线索未实现且不声称：平台暂无视觉腿，`image_ref` 只作为证据引用带下去（见 refs）。
        """
        text = str(payload.get("text") or payload.get("note") or "").strip()
        if not text:
            return {"status": "no_text", "hits": [], "human_review_suggested": False}
        if self.semantic is None:
            return {"status": "skipped_no_parser", "hits": [], "human_review_suggested": True}
        try:
            parsed = await self.semantic.parse(text, region_code=payload.get("region_code"), hazard_hint=payload.get("hazard_hint"))
        except Exception as exc:  # 语义腿失败不能带走快腿的命中：降级要可见，链路不能断
            return {
                "status": "degraded",
                "hits": [],
                "reason": f"{type(exc).__name__}: {exc}",
                "human_review_suggested": True,
            }
        hits = [hit.model_dump() if hasattr(hit, "model_dump") else dict(hit) for hit in (getattr(parsed, "hits", ()) or ())]
        confidence = float(getattr(parsed, "confidence", 0.0) or 0.0)
        needs_review = bool(getattr(parsed, "needs_review", False))
        return {
            "status": "ok",
            "hits": hits,
            "decided_by": str(getattr(parsed, "decided_by", "") or ""),
            "confidence": round(confidence, 4),
            "needs_review": needs_review,
            "human_review_suggested": needs_review or confidence < self.human_review_below,
            "degradations": [str(item) for item in (getattr(parsed, "degradations", ()) or ())],
        }

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


@dataclass(slots=True)
class _EwmaState:
    """单站点单指标的指数加权基线。"""

    mean: float
    var: float
    samples: int


def _advance(state: _EwmaState | None, value: float, alpha: float) -> _EwmaState:
    if state is None:
        return _EwmaState(mean=value, var=0.0, samples=1)
    delta = value - state.mean
    return _EwmaState(
        mean=state.mean + alpha * delta,
        var=state.var + alpha * (delta * delta - state.var),
        samples=state.samples + 1,
    )


@dataclass
class EwmaDriftDetector:
    """快腿第二判据（§3.1"EWMA/分位数越限"里的 EWMA 位）：偏离站点【自身常态】即弱信号异常。

    与绝对阈值越限的分工：那条抓"读数超过硬阈值"，这条抓"还没到阈值，但该站点已经不像它自己"
    ——持续降雨后位移从 2mm 缓涨到 6mm，阈值可能还没碰，而自身基线已经漂出 k 倍标准差。

    不另立真源：
    - 判据只用该站点自身的历史基线（指数加权均值/方差），不看任何阈值表；
    - 灾种归属【从注入的规则表里读】（`trigger_rules` 是单一真源）。规则表把同一指标指向
      【多个】灾种时（内置规则表里 `rain_cumulative_24h` 就同时指向泥石流与滑坡），本判据
      如实不报这个指标——宁可少一路弱信号，也不替指挥员猜灾种；消歧属装配点决策，不在此处臆造。
    - 指标压根不在规则表里 → 同样不报（没有灾种，就没有可交给下游研判的命中）。

    参数 `alpha` / `sigma_k` / `min_samples` 为**未实测初值**（对齐 §3.1 的标注纪律），
    联调后按真数据标定；预热不足 `min_samples` 个点时只积累基线、不出异常。
    """

    alpha: float = 0.3
    sigma_k: float = 3.0
    min_samples: int = 5
    #: 平台规则表（注入）：只用来把 metric 归到唯一灾种，不参与阈值判定。
    rules: Sequence[Rule] = ()
    _states: dict[tuple[str, str], _EwmaState] = field(default_factory=dict)

    def flag(self, reading: dict[str, Any]) -> StationAnomaly | None:
        station = str(reading.get("station_id") or "")
        metric = str(reading.get("metric") or "")
        value = reading.get("value")
        observed_at = str(reading.get("observed_at") or "")
        if not station or not metric or not observed_at or not isinstance(value, (int, float)):
            return None
        hazard = self._hazard_of(metric)
        previous = self._states.get((station, metric))
        self._states[(station, metric)] = _advance(previous, float(value), self.alpha)
        if hazard is None or previous is None or previous.samples < self.min_samples:
            return None

        # 用【更新前】的基线判偏离：当前点若先并进基线，会把自身的偏离摊薄掉。
        deviation = abs(float(value) - previous.mean)
        limit = self.sigma_k * math.sqrt(max(previous.var, 0.0))
        if limit <= 0.0:
            # 一路常数（方差为零）后出现任何变化都是偏离自身常态；分数封顶 0.95，不自称满格。
            if deviation <= 0.0:
                return None
            score = 0.95
        else:
            if deviation <= limit:
                return None
            score = min(0.95, 0.5 + 0.5 * (deviation / limit - 1.0))
        return StationAnomaly(
            station_id=station,
            region_code=str(reading.get("region_code") or ""),
            hazard_type=hazard,
            value=float(value),
            unit=str(reading.get("unit") or ""),
            observed_at=observed_at,
            base_score=round(score, 4),
        )

    def baseline_for(self, station_id: str, metric: str) -> tuple[float, float, int] | None:
        """把某站某指标的当前基线原样交出去（联调与报表要看得到"它自己认为常态是多少"）。"""
        state = self._states.get((station_id, metric))
        return None if state is None else (state.mean, state.var, state.samples)

    def _hazard_of(self, metric: str) -> str | None:
        hazards = {
            rule.hazard_type.value
            for rule in self.rules
            if rule.status == "active"
            for condition in rule.conditions
            if condition.metric == metric
        }
        if len(hazards) != 1:
            return None
        return next(iter(hazards))


_EPOCH = datetime.fromtimestamp(0)
