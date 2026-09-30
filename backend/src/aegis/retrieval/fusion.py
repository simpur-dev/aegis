"""融合腿：Reciprocal Rank Fusion（RRF）。

为什么是 RRF 而不是"归一化后加权求和"：两条腿的分数量纲不可比
（余弦相似度落在 [-1,1]，BM25 是无上界的饱和和），任何线性归一化都要引入
"当前这批候选的分布"这个额外假设，而候选集大小随查询变化，权重就得跟着调；
RRF 只看名次，天然对分布变化与单腿抖动鲁棒（Cormack et al. 2009 的原始结论也是这个）。

本模块只做数学：输入若干条已排序的腿，输出排序后的融合结果与逐腿凭证。
不 import 同包除 docs/onnx_io（常量与错误类型）之外的任何模块，也不碰 I/O。

确定性口径（单测锁住的就是这三条）：
- 名次从 1 开始，单腿内重复 doc_id 取最好的一次；
- 融合分 = Σ_腿 weight(腿) / (k + 名次)，k 默认 60；
- 同分按 doc_id 升序，凭证按腿名字典序 —— 与传入顺序无关，回放才可复现。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from aegis.retrieval.docs import LEG_DENSE, LEG_LEXICAL, LegName
from aegis.retrieval.onnx_io import RetrievalArgumentError

# 论文默认值：k 越大越平缓（头部名次的边际收益越小）。60 在本仓语料规模下不需要重估。
RRF_K: Final = 60

DEFAULT_WEIGHTS: Final[Mapping[LegName, float]] = {LEG_DENSE: 1.0, LEG_LEXICAL: 1.0}


@dataclass(frozen=True, slots=True)
class Ranking:
    """一条腿的排序结果：doc_ids 已按相关性降序，scores 与其同序（可以全 0）。"""

    leg: LegName
    doc_ids: tuple[str, ...]
    scores: tuple[float, ...]

    @classmethod
    def of(cls, leg: LegName, pairs: Iterable[tuple[str, float]]) -> Ranking:
        materialized = list(pairs)
        return cls(
            leg=leg,
            doc_ids=tuple(doc_id for doc_id, _ in materialized),
            scores=tuple(float(score) for _, score in materialized),
        )

    def __len__(self) -> int:
        return len(self.doc_ids)

    def score_of(self, doc_id: str) -> float | None:
        """首个出现位置的分；腿内 id 与 scores 等长由 `Ranking.of` 保证，
        直接构造的短 scores 按"没有分"处理而不是抛 IndexError。"""
        try:
            return self.scores[self.doc_ids.index(doc_id)]
        except (ValueError, IndexError):
            return None


@dataclass(frozen=True, slots=True)
class Contribution:
    """某条腿对某文档的贡献凭证：名次、该腿原始分、该腿算进融合分的那一项。"""

    leg: LegName
    rank: int
    score: float
    weighted: float


@dataclass(frozen=True, slots=True)
class FusedHit:
    doc_id: str
    score: float
    contributions: tuple[Contribution, ...]

    @property
    def legs(self) -> tuple[LegName, ...]:
        return tuple(item.leg for item in self.contributions)

    @property
    def raw_scores(self) -> Mapping[str, float]:
        return {item.leg: item.score for item in self.contributions}


def fuse(
    rankings: Sequence[Ranking],
    *,
    k: int = RRF_K,
    weights: Mapping[LegName, float] | None = None,
    limit: int | None = None,
) -> list[FusedHit]:
    """多腿 RRF。空腿与缺腿都只是"没有贡献"，不改变其它文档的相对次序。

    权重为 0 等价于该腿不参与：只被 0 权重腿命中的文档不会出现在结果里
    （融合分为 0 的文档留着只会把 limit 名额让给噪声）。
    """
    if k < 0:
        raise RetrievalArgumentError("RRF 的 k 不得为负", detail={"k": k})
    if limit is not None and limit <= 0:
        raise RetrievalArgumentError("limit 为正或省略", detail={"limit": limit})

    resolved = dict(DEFAULT_WEIGHTS)
    if weights:
        for leg, value in weights.items():
            if value < 0:
                raise RetrievalArgumentError("腿权重不得为负", detail={"leg": leg, "weight": value})
            resolved[leg] = float(value)

    totals: dict[str, float] = {}
    seen: dict[str, list[Contribution]] = {}
    for ranking in rankings:
        weight = float(resolved.get(ranking.leg, 1.0))
        claimed: set[str] = set()
        for position, doc_id in enumerate(ranking.doc_ids, start=1):
            if doc_id in claimed:
                continue
            claimed.add(doc_id)
            weighted = weight / (k + position)
            totals[doc_id] = totals.get(doc_id, 0.0) + weighted
            seen.setdefault(doc_id, []).append(
                Contribution(
                    leg=ranking.leg,
                    rank=position,
                    score=float(ranking.score_of(doc_id) or 0.0),
                    weighted=weighted,
                )
            )

    ordered = sorted((doc_id for doc_id, total in totals.items() if total > 0.0), key=lambda doc_id: (-totals[doc_id], doc_id))
    if limit is not None:
        ordered = ordered[:limit]
    return [
        FusedHit(
            doc_id=doc_id,
            score=totals[doc_id],
            contributions=tuple(sorted(seen[doc_id], key=lambda item: item.leg)),
        )
        for doc_id in ordered
    ]
