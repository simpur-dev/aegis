"""进程内案例库提供者：确定性关键词/子串打分，是降级链在"无图谱可用"时的真实实现。

设计取舍：
- 打分只依赖 `hazard_cases.json` 的字段与查询串，无网络、无随机、无外部索引——同输入必同输出；
- 中文没有空格分词，故对 CJK 串做 2-gram 切分（兼顾"泥位超阈值"与"泥位"这类部分命中）；
- 时效惩罚项（`estimated_delay_hours`）显式存在：预警生成 ≤3min 指标要求先出见效快的预案。
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, ClassVar

from aegis.knowledge.cases import load_builtin_cases
from aegis.knowledge.provider import CaseMatch, LearnOutcome, RecallSource

if TYPE_CHECKING:
    from collections.abc import Sequence

    from aegis.knowledge.cases import HazardCase

_TOKEN_SPLIT = re.compile(r"[^0-9a-z_\u4e00-\u9fff]+")
_CJK_RUN = re.compile(r"[\u4e00-\u9fff]{2,}")

# 字段权重：命中标题/征兆/动作比命中背景描述更能说明"这条案例就是在说这事"
_FIELD_WEIGHTS: dict[str, float] = {
    "title": 1.00,
    "trigger_signals": 0.80,
    "actions": 0.55,
    "why_now": 0.35,
    "target_groups": 0.25,
    "monitoring_metrics": 0.20,
    "category": 0.45,
    "phase": 0.30,
    "hazard": 0.45,
}
_HAZARD_BONUS = 0.35
_REGION_BONUS = 0.12
_DELAY_PENALTY_PER_HOUR = 0.004
_DELAY_PENALTY_CAP = 0.15


def tokenize(text: str) -> list[str]:
    """ASCII 词 + CJK 整串 + CJK 2-gram；顺序稳定，便于 matched_on 可复现。"""
    tokens: list[str] = []
    seen: set[str] = set()
    for chunk in _TOKEN_SPLIT.split(text.lower()):
        if not chunk:
            continue
        candidates: list[str] = [chunk]
        for run in _CJK_RUN.findall(chunk):
            candidates.extend(run[index : index + 2] for index in range(len(run) - 1))
        for token in candidates:
            if len(token) >= 2 and token not in seen:
                seen.add(token)
                tokens.append(token)
    return tokens


def _field_terms(case: HazardCase) -> dict[str, str]:
    return {
        "title": case.title,
        "trigger_signals": " ".join(case.trigger_signals),
        "actions": " ".join(case.actions),
        "why_now": case.why_now,
        "target_groups": " ".join(case.target_groups),
        "monitoring_metrics": " ".join(case.monitoring_metrics),
        "category": case.category,
        "phase": case.phase,
        "hazard": " ".join([*case.hazard_types, *case.hazard_labels]),
    }


class InMemoryKnowledgeProvider:
    """确定性关键词召回 + 进程内案例学习。"""

    name = "in_memory"
    driver: ClassVar[RecallSource] = "in_memory"

    def __init__(self, cases: Sequence[HazardCase] | None = None) -> None:
        self._cases: dict[str, HazardCase] = {case.case_id: case for case in (cases if cases is not None else load_builtin_cases())}

    @property
    def cases(self) -> tuple[HazardCase, ...]:
        return tuple(self._cases.values())

    def __len__(self) -> int:
        return len(self._cases)

    async def recall(
        self,
        query: str,
        *,
        hazard_type: str | None = None,
        region_code: str | None = None,
        limit: int = 5,
        budget_ms: float | None = None,
    ) -> list[CaseMatch]:
        """纯 CPU 打分（毫秒级），`budget_ms` 只用于与图谱实现保持同一签名与预算语义。"""
        del budget_ms
        if limit <= 0:
            return []
        candidates = [case for case in self._cases.values() if case.matches_hazard(hazard_type) and case.matches_region(region_code)]
        if not candidates and hazard_type is not None:
            # 灾种词表外（如"融雪""冰雪灾害"）时放宽灾种只保留区域约束，避免预案生成为空
            candidates = [case for case in self._cases.values() if case.matches_region(region_code)]

        tokens = tokenize(query)
        scored: list[tuple[float, str, list[str]]] = []
        for case in candidates:
            score, matched = _score(case, tokens, hazard_type=hazard_type, region_code=region_code)
            if matched or hazard_type in case.hazard_types:
                scored.append((score, case.case_id, matched))

        scored.sort(key=lambda item: (-item[0], item[1]))
        return [
            CaseMatch.from_case(
                self._cases[case_id],
                score=score,
                source="in_memory",
                matched_on=matched,
            )
            for score, case_id, matched in scored[:limit]
        ]

    async def learn(self, case: HazardCase) -> None:
        """同 case_id 覆盖：复盘修正后的案例应立即成为召回结果。"""
        self._cases[case.case_id] = case

    async def learn_case(self, case: HazardCase) -> LearnOutcome:
        await self.learn(case)
        return LearnOutcome(case_id=case.case_id, driver=self.driver)


def _score(
    case: HazardCase,
    tokens: Sequence[str],
    *,
    hazard_type: str | None,
    region_code: str | None,
) -> tuple[float, list[str]]:
    fields = {name: text.lower() for name, text in _field_terms(case).items()}
    matched: list[str] = []
    score = 0.0
    for token in tokens:
        best_field = ""
        best_weight = 0.0
        for name, text in fields.items():
            if token in text and _FIELD_WEIGHTS[name] > best_weight:
                best_weight = _FIELD_WEIGHTS[name]
                best_field = name
        if best_field:
            score += best_weight
            matched.append(f"{best_field}:{token}")
    if hazard_type and hazard_type in case.hazard_types:
        score += _HAZARD_BONUS
        matched.append(f"hazard:{hazard_type}")
    if region_code and case.region_prefixes and case.matches_region(region_code):
        score += _REGION_BONUS
        matched.append(f"region:{region_code}")
    score += case.confidence * 0.2
    score -= min(_DELAY_PENALTY_CAP, case.estimated_delay_hours * _DELAY_PENALTY_PER_HOUR)
    return score, matched
