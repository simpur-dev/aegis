"""定级结论：一次"这条威胁有多大"的领域判断的载体。

为什么住在 domain 而不是 `services/risk_engine.py`：它是**跨层流转的纯数据**——
研判智能体产出它、执行段消费它、`chain_runs.risk` 原样落库、API 与前端直接读它的字段。
放在服务层就意味着下游（storage/persistence/pipeline）要反向 import 服务模块才能拿到一个数据结构，
而 `RiskEngine`（真正带规则表与降级逻辑的那一半）留在 services 里没有任何东西需要下沉。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from aegis.domain.enums import HazardType, RiskLevel
from aegis.domain.messages import TriggerHit, now_iso


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
    #: §3.5 的低置信护栏。此前它只在 `rationale` 那句"（建议人工核签：置信度 …）"里活着，
    #: 于是全仓只有生产者、没有读者——一个布尔变成一列事实，值班员才可能有一张待办清单。
    human_review_suggested: bool = False

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
            "human_review_suggested": self.human_review_suggested,
        }
