"""风险定级引擎：把规则命中折算为统一 1-5 级风险口径（等级数字越小越危险）。

降级路径的唯一事实来源：研判智能体缺位/超时时，平台以此产出可用的定级结论。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from aegis.domain.enums import HazardType, RiskLevel
from aegis.domain.messages import TriggerHit, now_iso
from aegis.services.trigger_rules import Rule, RuleEngine

# 佐证数量对置信度的增益上限（单条规则命中不足以给出高置信）
_CORROBORATION_GAIN = 0.15


@dataclass(slots=True)
class RiskVerdict:
    hazard_type: HazardType
    region_code: str
    risk_level: RiskLevel
    confidence: float
    rationale: str
    hits: list[TriggerHit] = field(default_factory=list)
    assessed_at: str = field(default_factory=now_iso)
    assessed_by: str = "platform.risk_engine"

    def as_payload(self) -> dict[str, object]:
        return {
            "hazard_type": self.hazard_type.value,
            "region_code": self.region_code,
            "risk_level": int(self.risk_level),
            "confidence": round(self.confidence, 4),
            "rationale": self.rationale,
            "evidence_refs": [e for hit in self.hits for e in hit.evidence_refs],
            "assessed_at": self.assessed_at,
            "assessed_by": self.assessed_by,
        }


class RiskEngine:
    def __init__(self, rule_engine: RuleEngine | None = None) -> None:
        self._rule_engine = rule_engine or RuleEngine()
        self._refresh()
        # 规则库换版后，等级与权重映射必须跟着换：否则研判还在按上一版阈值定级，
        # 而 `/api/v1/rules` 已经写着新版本号——两份口径同时对外，就是最难对上的那种漂移。
        hook = getattr(self._rule_engine, "on_reload", None)
        if callable(hook):
            hook(self._refresh)

    def _refresh(self) -> None:
        self._levels: dict[str, RiskLevel] = {rule.rule_id: rule.triggered_level for rule in self._rule_engine.rules}
        self._weights: dict[str, float] = {rule.rule_id: rule.weight for rule in self._rule_engine.rules}

    def rules(self) -> tuple[Rule, ...]:
        return self._rule_engine.rules

    def level_of(self, rule_id: str) -> RiskLevel | None:
        return self._levels.get(rule_id)

    def assess(self, hits: list[TriggerHit], *, region_code: str | None = None) -> RiskVerdict | None:
        if not hits:
            return None

        target_region = region_code or hits[0].region_code
        scoped = [h for h in hits if h.region_code == target_region] or hits
        levels = [self._levels.get(h.rule_id) or RiskLevel.YELLOW for h in scoped]
        worst = min(int(level) for level in levels)
        anchor = RiskLevel(worst)

        # 最严重灾种由达最高等级的命中项决定；同级时取加权得分更高者
        decisive = max(
            (h for h, level in zip(scoped, levels, strict=False) if int(RiskLevel(self._levels.get(h.rule_id, anchor))) == worst),
            key=lambda h: h.score * self._weights.get(h.rule_id, 1.0),
        )
        corroboration = sum(1 for h in scoped if h.hazard_type == decisive.hazard_type)
        base = sum(h.score for h in scoped) / len(scoped)
        confidence = min(0.99, base * 0.8 + _CORROBORATION_GAIN * min(corroboration, 3))

        rationale = "；".join(f"[{h.rule_id}] {e}" for h in scoped for e in (h.evidence_refs[:1] or ["证据缺失"])) or "无证据"
        return RiskVerdict(
            hazard_type=HazardType(decisive.hazard_type),
            region_code=target_region,
            risk_level=anchor,
            confidence=round(confidence, 4),
            rationale=rationale,
            hits=scoped,
        )

    def assess_many(self, hits: list[TriggerHit]) -> list[RiskVerdict]:
        """按区域分组定级（多站点并发场景，单次调用产出多个区域结论）。"""
        verdicts: list[RiskVerdict] = []
        for region in dict.fromkeys(h.region_code for h in hits):
            if (verdict := self.assess([h for h in hits if h.region_code == region], region_code=region)) is not None:
                verdicts.append(verdict)
        return verdicts

    @staticmethod
    def as_of(moment: datetime | None = None) -> str:
        return (moment or datetime.now()).isoformat()
