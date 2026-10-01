"""案例库 → 检索语料：把 `HazardCase` 摊成 dense/lexical 两腿共同消费的 `KnowledgeDoc`。

为什么映射放在检索层：`KnowledgeDoc` 的字段口径由检索层定义，案例模型不该为了"被检索到"
而知道自己会被切成什么形状；反过来检索层需要读懂案例字段，这个方向是单向的、可测的。

为什么 `hazard_type`/`region_code` 留空：一条案例可以挂多个灾种、多个**前缀**语义的区域，
而 `KnowledgeDoc.matches()` 是单值等值过滤。硬压成单值会造成静默丢失（按 debris_flow 过滤时
"同时适用泥石流与滑坡"的案例直接消失），比不过滤更糟。因此结构化过滤仍由调用方用案例模型
自己的谓词（`matches_hazard`/`matches_region`）完成，语料只负责"被召回"。

区域前缀进正文而不是进列：`searchable_text()` 里没有行政区划信息，而区县代码是本地性最强的
信号之一；把它拼进语料，词法腿才有机会按区域排序，密集腿也能从 token 里看到它。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Final

from aegis.knowledge.cases import HazardCase
from aegis.retrieval.docs import KnowledgeDoc

# 语料来源标识：会随每条结果的 provenance.source 一起出现在凭证里，用于区分"案例库"与后续接入的 chunk 表
CASES_SOURCE: Final = "knowledge:hazard_cases"


def doc_from_case(case: HazardCase) -> KnowledgeDoc:
    """一条案例 = 一条语料。`doc_id` 直接用 `case_id`，召回结果无需再查表就能回到案例本体。"""
    regions = " ".join(case.region_prefixes)
    text = f"{case.searchable_text()} {regions}".strip()
    return KnowledgeDoc(
        doc_id=case.case_id,
        text=text,
        source=CASES_SOURCE,
        updated_at=case.valid_at,
        metadata={
            "title": case.title,
            "category": case.category,
            "phase": case.phase,
            "hazard_types": list(case.hazard_types),
            "region_prefixes": list(case.region_prefixes),
            "applies_to_levels": list(case.applies_to_levels),
            "confidence": case.confidence,
            "estimated_delay_hours": case.estimated_delay_hours,
        },
    )


def docs_from_cases(cases: Iterable[HazardCase]) -> list[KnowledgeDoc]:
    """按 case_id 去重并保持输入顺序：重复语料会让 BM25 的文档长度统计失真。"""
    seen: set[str] = set()
    docs: list[KnowledgeDoc] = []
    for case in cases:
        if case.case_id in seen:
            continue
        seen.add(case.case_id)
        docs.append(doc_from_case(case))
    return docs


def case_ids(docs: Sequence[KnowledgeDoc]) -> list[str]:
    """语料覆盖到的案例 id：用于状态接口报告"检索层到底看得见多少案例"。"""
    return [doc.doc_id for doc in docs]


__all__ = ["CASES_SOURCE", "case_ids", "doc_from_case", "docs_from_cases"]
