"""山地灾害预案案例：字段集从 NexusMind `intervention_library.py` 的干预模板移植，改为数据驱动。

为什么用 JSON 数据而不是 Python 常量：案例库要能被现场复盘持续追加、被图谱批量摄取，
硬编码列表会让"新增一条预案"变成一次代码改动（NexusMind 的教训）。

`state_effects` 的键是本平台的处置态势向量（与 NexusMind 的 6 维舆情状态变量同构，换了口径）：
    risk_exposure      受威胁人员与资产暴露度（越低越好）
    casualty_risk      人员伤亡风险（越低越好）
    secondary_hazard   灾害链次生风险（越低越好）
    road_disruption    交通干线中断程度（越低越好）
    response_delay     处置时延（越低越好）
    monitoring_coverage 监测与预警覆盖（越高越好）
    public_trust       群众对预警的信任与响应度（越高越好）
    resource_strain    应急资源挤占（越低越好）
值域 [-1, 1]，表示该预案执行后各分量的相对变化方向与幅度。
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from aegis.domain.enums import HAZARD_CN
from aegis.domain.messages import now_iso, parse_iso
from aegis.errors import SchemaInvalidError

CaseCategory = Literal["survey", "monitor", "warn", "evacuate", "dispatch", "engineering", "communication"]
CasePhase = Literal["early", "response", "handling", "recovery"]

_HAZARD_TOKEN = re.compile(r"^[a-z][a-z0-9_]{2,31}$")
_REGION_PREFIX = re.compile(r"^[0-9]{2,6}$")

DEFAULT_STATE_EFFECT_KEYS: frozenset[str] = frozenset(
    {
        "risk_exposure",
        "casualty_risk",
        "secondary_hazard",
        "road_disruption",
        "response_delay",
        "monitoring_coverage",
        "public_trust",
        "resource_strain",
    }
)


class HazardCase(BaseModel):
    """一条可执行的预案案例：既是一个数据模型，也是一段图谱剧集（episode）的正文来源。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str = Field(pattern=r"^case_[a-z0-9_]{4,60}$")
    title: str = Field(min_length=4, max_length=120)
    hazard_types: list[str] = Field(min_length=1, max_length=8)
    category: CaseCategory
    phase: CasePhase
    why_now: str = Field(min_length=6, max_length=600)
    target_groups: list[str] = Field(min_length=1, max_length=12)
    actions: list[str] = Field(min_length=1, max_length=12)
    expected_effects: list[str] = Field(default_factory=list, max_length=12)
    possible_side_effects: list[str] = Field(default_factory=list, max_length=12)
    required_prerequisites: list[str] = Field(default_factory=list, max_length=12)
    monitoring_metrics: list[str] = Field(default_factory=list, max_length=12)
    trigger_signals: list[str] = Field(default_factory=list, max_length=12)
    estimated_delay_hours: float = Field(ge=0.0, le=720.0)
    confidence: float = Field(ge=0.0, le=1.0)
    state_effects: dict[str, float] = Field(default_factory=dict, max_length=16)
    region_prefixes: list[str] = Field(default_factory=list, max_length=16)
    applies_to_levels: list[int] = Field(default_factory=list, max_length=5)
    source_note: str = Field(default="", max_length=400)
    observed_at: str = Field(default_factory=now_iso)

    @field_validator("hazard_types")
    @classmethod
    def _hazards_lowercase_tokens(cls, value: list[str]) -> list[str]:
        invalid = [h for h in value if not _HAZARD_TOKEN.match(h)]
        if invalid:
            raise ValueError(f"灾种标识须为小写下划线 token: {invalid}")
        if len(set(value)) != len(value):
            raise ValueError(f"灾种标识重复: {value}")
        return value

    @field_validator("region_prefixes")
    @classmethod
    def _region_prefixes_numeric(cls, value: list[str]) -> list[str]:
        invalid = [p for p in value if not _REGION_PREFIX.match(p)]
        if invalid:
            raise ValueError(f"区域前缀须为 2—6 位行政区划数字: {invalid}")
        return value

    @field_validator("applies_to_levels")
    @classmethod
    def _levels_in_range(cls, value: list[int]) -> list[int]:
        # RiskLevel 口径：1 红 … 5 无风险，预案只允许挂在 1—5 上
        if any(not 1 <= level <= 5 for level in value):
            raise ValueError(f"适用等级越界: {value}")
        return sorted(set(value))

    @model_validator(mode="after")
    def _state_effects_bounded(self) -> HazardCase:
        out_of_range = {k: v for k, v in self.state_effects.items() if not -1.0 <= v <= 1.0}
        if out_of_range:
            raise ValueError(f"state_effects 取值须在 [-1,1]: {out_of_range}")
        parse_iso(self.observed_at)
        return self

    @property
    def valid_at(self) -> datetime:
        """案例经验的生效时刻（双时态里的 valid time；事务时间由图谱写入时间给出）。"""
        return parse_iso(self.observed_at)

    @property
    def hazard_labels(self) -> list[str]:
        return [HAZARD_CN.get(h, h) for h in self.hazard_types]

    def matches_hazard(self, hazard_type: str | None) -> bool:
        return hazard_type is None or hazard_type in self.hazard_types

    def matches_region(self, region_code: str | None) -> bool:
        if region_code is None or not self.region_prefixes:
            return True
        return any(region_code.startswith(prefix) for prefix in self.region_prefixes)

    def searchable_text(self) -> str:
        """召回打分的统一语料：把标签、动作、征兆压成一段可被关键词命中的文本。"""
        parts = [
            self.title,
            self.why_now,
            self.category,
            self.phase,
            " ".join(self.hazard_types),
            " ".join(self.hazard_labels),
            " ".join(self.trigger_signals),
            " ".join(self.actions),
            " ".join(self.target_groups),
            " ".join(self.monitoring_metrics),
            " ".join(self.expected_effects),
        ]
        return " ".join(p for p in parts if p).lower()

    def to_episode_text(self) -> str:
        """图谱剧集正文：固定字段顺序保证同一案例的文本可复现（否则去重与比对无意义）。"""
        lines = [
            f"预案案例 {self.case_id}：{self.title}",
            f"灾种：{'、'.join(self.hazard_labels)}（{'、'.join(self.hazard_types)}）",
            f"动作类别：{self.category}｜处置阶段：{self.phase}｜适用风险等级：{self.applies_to_levels or '不限'}",
            f"适用区域前缀：{'、'.join(self.region_prefixes) or '不限'}",
            f"成灾征兆：{'；'.join(self.trigger_signals) or '未标注'}",
            f"为何此刻执行：{self.why_now}",
            f"作用对象：{'；'.join(self.target_groups)}",
            f"处置动作：{'；'.join(self.actions)}",
            f"预期效果：{'；'.join(self.expected_effects) or '未标注'}",
            f"潜在副作用：{'；'.join(self.possible_side_effects) or '未标注'}",
            f"前置条件：{'；'.join(self.required_prerequisites) or '未标注'}",
            f"监测指标：{'；'.join(self.monitoring_metrics) or '未标注'}",
            f"见效时延：{self.estimated_delay_hours} 小时｜案例置信度：{self.confidence}",
            f"态势影响：{'；'.join(f'{k}={v:+g}' for k, v in sorted(self.state_effects.items())) or '未标注'}",
            f"案例经验生效时间：{self.observed_at}",
        ]
        if self.source_note:
            lines.append(f"案例来源：{self.source_note}")
        return "\n".join(lines)

    def plan_brief(self) -> dict[str, Any]:
        """给预案生成环节的直接可用切片（不含图谱细节）。"""
        return {
            "case_id": self.case_id,
            "title": self.title,
            "category": self.category,
            "phase": self.phase,
            "actions": list(self.actions),
            "why_now": self.why_now,
            "required_prerequisites": list(self.required_prerequisites),
            "possible_side_effects": list(self.possible_side_effects),
            "monitoring_metrics": list(self.monitoring_metrics),
            "estimated_delay_hours": self.estimated_delay_hours,
            "confidence": self.confidence,
            "state_effects": dict(self.state_effects),
        }


DEFAULT_CASES_PATH = Path(__file__).resolve().parent / "data" / "hazard_cases.json"


def load_cases(path: Path) -> list[HazardCase]:
    """从 JSON 读取案例：单条不合法即整体报错，不做静默丢弃（残缺预案库比空库更危险）。"""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise SchemaInvalidError(f"预案案例库不可读: {path}", detail={"path": str(path), "error": str(exc)}) from exc
    except json.JSONDecodeError as exc:
        raise SchemaInvalidError(f"预案案例库不是合法 JSON: {path}", detail={"error": str(exc)}) from exc
    if not isinstance(raw, list) or not raw:
        raise SchemaInvalidError("预案案例库必须是非空对象数组", detail={"path": str(path), "type": type(raw).__name__})

    cases: list[HazardCase] = []
    errors: list[str] = []
    for index, item in enumerate(raw):
        try:
            cases.append(HazardCase.model_validate(item))
        except ValueError as exc:
            errors.append(f"#{index}: {exc}")
    if errors:
        raise SchemaInvalidError(f"预案案例库中 {len(errors)} 条不合法", detail={"errors": errors[:3]})

    duplicated = _duplicates([c.case_id for c in cases])
    if duplicated:
        raise SchemaInvalidError("预案案例库存在重复 case_id", detail={"case_ids": duplicated[:5]})
    return cases


def _duplicates(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value in seen and value not in out:
            out.append(value)
        seen.add(value)
    return out


@lru_cache(maxsize=4)
def _builtin_cases(path_str: str) -> tuple[HazardCase, ...]:
    return tuple(load_cases(Path(path_str)))


def load_builtin_cases(path: Path | None = None) -> list[HazardCase]:
    """内置案例库（带缓存，返回副本列表以避免调用方污染缓存）。"""
    target = path or DEFAULT_CASES_PATH
    return list(_builtin_cases(str(target)))


def cases_by_id(cases: list[HazardCase] | tuple[HazardCase, ...] | None = None) -> dict[str, HazardCase]:
    return {case.case_id: case for case in (cases if cases is not None else load_builtin_cases())}
