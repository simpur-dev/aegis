"""数据出处：一条结论建立在什么数据、什么方法上。

为什么独立成一层：`AssessEvidence`（输入侧）与 `RiskVerdict`（输出侧）都要携带它，
而两者分属 `domain.assess` 与 `domain.verdict`；放在任一侧都会造成循环 import。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

DataKind = Literal["observed", "derived", "reported", "mock", "demo", "mixed", "unknown"]
MethodKind = Literal[
    "telemetry_rule",
    "factor_engine_mock",
    "llm",
    "report_parse",
    "mock_agent",
    "unknown",
]


class DataProvenance(BaseModel):
    """数据出处。`is_mock=True` 表示结论包含替身/演示数据，混合来源也置 True。"""

    model_config = ConfigDict(extra="forbid")

    data_kind: DataKind = "unknown"
    sources: list[str] = Field(default_factory=list, max_length=16)
    method: MethodKind = "unknown"
    method_version: str = Field(default="", max_length=64)
    is_mock: bool = False
    disclaimer: str = Field(default="", max_length=500)

    @classmethod
    def from_payload(cls, raw: object) -> DataProvenance:
        """从 `risk` 载荷里的嵌套 dict 还原；认不出就诚实退回 unknown，不猜。"""
        if isinstance(raw, cls):
            return raw
        if isinstance(raw, dict):
            try:
                return cls.model_validate(raw)
            except ValidationError:
                return cls()
        return cls()


__all__ = ["DataKind", "DataProvenance", "MethodKind"]
