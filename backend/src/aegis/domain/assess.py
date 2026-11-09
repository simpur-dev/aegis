"""研判契约（assess.v1）：平台与任一实现（builtin / mock / ext）共用的唯一请求/响应形状。

设计口径：
- 请求携带**证据**（`AssessEvidence`）而不是让实现自己猜 hits 的含义；证据由平台数据面
  统一构造，推给实现；实现也可以按 `data_refs` 从 `/api/v1/assess/evidence` 拉。
- 响应必须给 `confidence` 与 `confidence_basis`：平台只校验 0..1 与"依据非空"，
  **不代填**——没依据的置信度就是伪证。
- 所有模型 `extra="forbid"`：契约从严，拼错的字段当场失败，不留"配了没生效"的暗面。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from aegis.domain.enums import HazardType, RiskLevel
from aegis.domain.messages import TriggerHit, now_iso
from aegis.domain.provenance import DataProvenance
from aegis.domain.site import SiteProfile
from aegis.domain.verdict import RiskVerdict

CONTRACT_VERSION: Literal["assess.v1"] = "assess.v1"
WINDOW_PATTERN = r"^\d{1,4}(m|h)$"
REGION_PATTERN = r"^[0-9A-Z]{6,24}$"
FACTOR_PATTERN = r"^[a-z][a-z0-9_]*$"


class AssessErrorCode(StrEnum):
    """研判段错误码：全部满足 AgentMessage schema 的 `^E[A-Z0-9_]{3,20}$`。"""

    INSUFFICIENT = "E_ASSESS_INSUFFICIENT"
    UNCERTAIN = "E_ASSESS_UNCERTAIN"
    FAILED = "E_ASSESS_FAILED"
    HAZARD_UNSUP = "E_ASSESS_HAZARD_UNSUP"
    BAD_LEVEL = "E_ASSESS_BAD_LEVEL"
    CONF_MISSING = "E_ASSESS_CONF_MISSING"
    BAD_INPUT = "E_ASSESS_BAD_INPUT"


class FactorObservation(BaseModel):
    """一个因子的窗口聚合观测。数值与类别至少其一；数值必须有限。"""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    factor: str = Field(pattern=FACTOR_PATTERN, max_length=64)
    unit: str = Field(default="", max_length=24)
    samples: int = Field(ge=1, le=100_000)
    min_value: float | None = None
    max_value: float | None = None
    mean_value: float | None = None
    class_value: str | None = Field(default=None, max_length=32)
    class_counts: dict[str, int] | None = None
    observed_at: str = Field(max_length=40)
    station_ids: list[str] = Field(default_factory=list, max_length=32)
    quality: Literal["ok", "partial", "site"] = "ok"
    sources: list[str] = Field(default_factory=list, max_length=8)
    derivation: str = Field(default="", max_length=120)

    @model_validator(mode="after")
    def _value_shape(self) -> FactorObservation:
        numeric = [v for v in (self.min_value, self.max_value, self.mean_value) if v is not None]
        if not numeric and self.class_value is None:
            raise ValueError("观测必须携带数值或类别值")
        if self.min_value is not None and self.max_value is not None and self.min_value > self.max_value:
            raise ValueError("min_value 不得大于 max_value")
        return self


class UnmappedMetric(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metric: str = Field(max_length=64)
    samples: int = Field(ge=1)
    reason: str = Field(max_length=200)


class AssessEvidence(BaseModel):
    """研判证据包：窗口内观测 + 区域画像 + 出处。由平台数据面构造，实现只读。"""

    model_config = ConfigDict(extra="forbid")

    region_code: str = Field(pattern=REGION_PATTERN)
    window: str = Field(pattern=WINDOW_PATTERN)
    generated_at: str = Field(max_length=40)
    observations: list[FactorObservation] = Field(default_factory=list, max_length=48)
    site_profile: SiteProfile | None = None
    provenance: DataProvenance = Field(default_factory=DataProvenance)
    unmapped_metrics: list[UnmappedMetric] = Field(default_factory=list, max_length=24)
    notes: list[str] = Field(default_factory=list, max_length=16)
    truncated: bool = False


class AssessRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract_version: Literal["assess.v1"] = CONTRACT_VERSION
    region_code: str = Field(pattern=REGION_PATTERN)
    window: str = Field(pattern=WINDOW_PATTERN)
    hazard_type: HazardType | None = None
    candidate_hazards: list[HazardType] = Field(default_factory=list, max_length=7)
    hits: list[TriggerHit] = Field(default_factory=list, max_length=64)
    rule_level: RiskLevel | None = None
    intake: Literal["telemetry", "report"] = "telemetry"
    evidence: AssessEvidence | None = None
    data_refs: list[str] = Field(default_factory=list, max_length=32)
    requested_at: str = Field(default_factory=now_iso, max_length=40)

    @model_validator(mode="after")
    def _hazard_in_candidates(self) -> AssessRequest:
        if self.hazard_type is not None and self.candidate_hazards and self.hazard_type not in self.candidate_hazards:
            raise ValueError(f"hazard_type={self.hazard_type} 不在 candidate_hazards 内")
        return self


class AssessResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    contract_version: Literal["assess.v1"] = CONTRACT_VERSION
    hazard_type: HazardType
    region_code: str = Field(pattern=REGION_PATTERN)
    risk_level: RiskLevel
    confidence: float = Field(ge=0.0, le=1.0)
    confidence_basis: str = Field(min_length=1, max_length=200)
    rationale: str = Field(default="", max_length=1000)
    evidence_refs: list[str] = Field(default_factory=list, max_length=32)
    assessed_at: str = Field(default_factory=now_iso, max_length=40)
    assessed_by: str = Field(max_length=64)
    human_review_suggested: bool = False
    double_blind: str = Field(default="", max_length=32)
    double_blind_mode: str = Field(default="", max_length=16)
    source: str = Field(max_length=64)
    data_provenance: DataProvenance = Field(default_factory=DataProvenance)
    assessment: dict[str, Any] = Field(default_factory=dict)

    def to_verdict(self, *, hits: list[TriggerHit]) -> RiskVerdict:
        return RiskVerdict(
            hazard_type=self.hazard_type,
            region_code=self.region_code,
            risk_level=self.risk_level,
            confidence=self.confidence,
            rationale=self.rationale[:1000] or "智能体结论",
            hits=hits,
            assessed_at=self.assessed_at,
            assessed_by=self.assessed_by,
            human_review_suggested=self.human_review_suggested,
            data_provenance=self.data_provenance,
            confidence_basis=self.confidence_basis,
            assessment=self.assessment,
        )


__all__ = [
    "CONTRACT_VERSION",
    "AssessErrorCode",
    "AssessEvidence",
    "AssessRequest",
    "AssessResponse",
    "FactorObservation",
    "UnmappedMetric",
]
