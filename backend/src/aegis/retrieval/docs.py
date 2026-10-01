"""语料记录与检索产物：本层的数据形状，纯数据、无 I/O、无判断。

边界说明（与 persistence/vectors.py 同一口径）：这里只回答"一条语料/一条召回长什么样"，
不回答"该不该召回""怎么排序"——那是 service.py 与 fusion.py 的事。

字段命名刻意贴 persistence.rows 的列名（hazard_type / region_code / source / created_at），
让密集腿（pgvector 行）与词法腿（进程内索引）能落到同一个产物类型上，
调用方不需要为两条腿各写一套映射。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final, Literal

from aegis.persistence.rows import EMBEDDING_DIM

LEG_DENSE: Final = "dense"
LEG_LEXICAL: Final = "lexical"
LEG_RERANK: Final = "rerank"

LegName = Literal["dense", "lexical", "rerank"]

# 与 persistence.rows.MAX_TEXT_LEN 同量级：入嵌文本再长也会被模型截断，先在这里收口。
MAX_INPUT_CHARS: Final = 2_000


@dataclass(frozen=True, slots=True)
class KnowledgeDoc:
    """一条可检索语料：预警知识/历史处置要点的最小单元。

    `hazard_type`/`region_code` 是结构化过滤面（与 dense 腿的 SQL WHERE 同义），
    不是查询文本的一部分；两者只通过 `to_embedding_input` 参与文本形态。
    """

    doc_id: str
    text: str
    hazard_type: str | None = None
    region_code: str | None = None
    source: str = ""
    updated_at: datetime | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def matches(self, *, hazard_type: str | None = None, region_code: str | None = None) -> bool:
        """结构化过滤谓词：None 表示该维度不设限（与 build_top_k 的参数语义一致）。"""
        return (hazard_type is None or self.hazard_type == hazard_type) and (region_code is None or self.region_code == region_code)


@dataclass(frozen=True, slots=True)
class Provenance:
    """一条结果的来源凭证：哪几条腿命中、各自的原始分、融合与重排分。

    保留原始分而不是只保留名次，是为了让"这条为什么排前面"能被事后审计
    （预警文案要引用依据，无凭证的召回不能进正文）。
    """

    legs: tuple[LegName, ...]
    leg_scores: Mapping[str, float]
    fusion_score: float
    fusion_rank: int
    rerank_score: float | None = None
    degraded_legs: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RetrievedDoc:
    """排序后的一条召回：文本 + 元数据 + 凭证。"""

    doc_id: str
    text: str
    rank: int
    score: float
    provenance: Provenance
    hazard_type: str | None = None
    region_code: str | None = None
    source: str = ""
    updated_at: datetime | None = None

    @property
    def hit_by_dense(self) -> bool:
        return LEG_DENSE in self.provenance.legs

    @property
    def hit_by_lexical(self) -> bool:
        return LEG_LEXICAL in self.provenance.legs

    def as_reference(self, *, max_chars: int = 240) -> dict[str, Any]:
        """给链路 payload 与 HTTP API 的唯一切片：正文按长度收口，凭证原样保留。

        文本必须截断（这条块是喂给下游的上下文，不是文档全文），但分数与腿不能省——
        "为什么这条排在前面"是预警正文引用依据时要能回答的问题。
        链路与 API 共用这一份映射，避免两处各抄一遍字段而漂移。
        """
        return {
            "doc_id": self.doc_id,
            "rank": self.rank,
            "score": round(self.score, 6),
            "source": self.source,
            "text": self.text.strip()[:max_chars],
            "legs": list(self.provenance.legs),
            "leg_scores": {str(key): round(float(value), 6) for key, value in self.provenance.leg_scores.items()},
            "degraded_legs": list(self.provenance.degraded_legs),
        }


def to_embedding_input(doc: KnowledgeDoc, *, with_context: bool = True, max_chars: int = MAX_INPUT_CHARS) -> str:
    """把语料记录映射为送进嵌入模型的文本。

    为什么要加前缀：bge-m3 的密集向量只看 token，看不到行里的 hazard_type/region_code 列。
    对 5 灾种、区县级代码这种小标签空间，灾种与区域词面往往是最有区分度的信号，
    丢掉它们会让同一区域不同灾种的 chunk 向量挤在一起。

    结构化字段仍留在列上做硬过滤，前缀只是给相似度用的，不作为过滤依据。
    """
    text = doc.text.strip()
    if not with_context:
        return _clip(text, max_chars)
    labels = [doc.hazard_type, doc.region_code, doc.source]
    context = " ".join(part for part in labels if part)
    return _clip(f"{context} {text}" if context else text, max_chars)


def _clip(text: str, max_chars: int) -> str:
    if max_chars <= 0:
        raise ValueError(f"max_chars 必须为正：{max_chars}")
    return text[:max_chars]


__all__ = [
    "EMBEDDING_DIM",
    "LEG_DENSE",
    "LEG_LEXICAL",
    "LEG_RERANK",
    "MAX_INPUT_CHARS",
    "KnowledgeDoc",
    "LegName",
    "Provenance",
    "RetrievedDoc",
    "to_embedding_input",
]
