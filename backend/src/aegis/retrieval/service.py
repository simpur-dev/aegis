"""混合检索服务：dense + lexical 并发取候选 -> RRF -> 可选重排 -> 带凭证的排序结果。

三条不可让的口径（P0「预警信息生成时间 ≤3min，且 LLM 不进检索回路」的落地方式）：

1. **LLM 不在回路里。** 本模块的依赖只有嵌入协议、进程内倒排索引、`persistence.vectors` 的
   真 SQL 路径与交叉编码器协议；import 图上没有 `aegis.services.llm_gateway`，也没有 `aegis.api`。
   预警生成侧拿到的是已经排好序的上下文块，检索阶段一次 LLM 调用都不发生。
2. **预算优先于完整性。** `budget_ms` 到点就交卷：跑完的腿照常出结果，超时的腿被取消并记一条
   degradation。检索的运行故障永不冒泡进预警路径——预警宁可用降级上下文生成，也不能因为
   检索抖动而超 3 分钟或报错。唯一的例外是调用方参数错（`RetrievalArgumentError` 与持久层的
   `VectorArgumentError` 族）：那是代码缺陷，静默降级会把 bug 伪装成"能用但变慢了"。
3. **结果必须可审计。** 每条 `RetrievedDoc` 带命中腿、各腿原始分、融合分与名次；
   预警正文引用依据时要能说出这条是模型召回的还是词面命中的。

线程口径：ONNX 是阻塞 CPU 推理，一律 `asyncio.to_thread` 出事件循环。
超时只能取消"等待"，取消不了已经在跑的线程——因此每条腿都有硬上限
（嵌入侧是 max_length，重排侧是 rerank_top_n），止损靠上限而不是靠打断。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final

from aegis.observability.tracer import Tracer
from aegis.persistence.errors import VectorArgumentError
from aegis.persistence.vectors import MAX_K, VectorHit, search_top_k
from aegis.retrieval.docs import LEG_DENSE, LEG_LEXICAL, LEG_RERANK, KnowledgeDoc, LegName, Provenance, RetrievedDoc
from aegis.retrieval.embedder import Embedder, embed_one
from aegis.retrieval.fusion import RRF_K, FusedHit, Ranking, fuse
from aegis.retrieval.lexical import LexicalIndex, rank
from aegis.retrieval.onnx_io import RetrievalArgumentError
from aegis.retrieval.reranker import Reranker

log = logging.getLogger("aegis.retrieval.service")

# 检索预算：3 分钟是预警生成的总口径，检索只占其中一小段，剩下的留给研判与文案。
# 默认值口径见 REPORT：它必须由配置注入，因为不同边缘盒的 CPU 差出一个量级。
DEFAULT_BUDGET_MS: Final = 8_000.0

# 每条腿多取几倍候选：RRF 靠"两腿名次都靠前"抬分，只取 k 条会让重叠信号消失。
CANDIDATE_MULTIPLIER: Final = 3

METRIC_TOTAL: Final = "retrieval_total_ms"

_OUTCOME_OK: Final = "ok"
_OUTCOME_TIMEOUT: Final = "timeout"
_OUTCOME_ERROR: Final = "error"
_OUTCOME_SKIPPED: Final = "skipped"

# 调用方参数错必须原样上抛（与持久层 QueryArgumentError / QueryFailedError 的分界同一判据）。
_PROPAGATE: Final = (VectorArgumentError, RetrievalArgumentError)


@dataclass(frozen=True, slots=True)
class RetrievalQuery:
    """一次检索请求：查询文本 + 结构化过滤 + 预算。"""

    text: str
    k: int = 8
    hazard_type: str | None = None
    region_code: str | None = None
    since: datetime | None = None
    until: datetime | None = None
    score_threshold: float | None = None
    budget_ms: float | None = None
    trace_id: str | None = None
    rerank: bool = True

    def candidate_k(self, multiplier: int = CANDIDATE_MULTIPLIER) -> int:
        """单腿取多少候选：≥ k，且不超过持久层单次检索上限。"""
        if self.k <= 0:
            raise RetrievalArgumentError("k 必须为正", detail={"k": self.k})
        return min(self.k * max(1, multiplier), MAX_K)


@dataclass(frozen=True, slots=True)
class Degradation:
    """一次降级的记录：谁降的、为什么、当时还剩多少预算。"""

    leg: str
    reason: str
    outcome: str = _OUTCOME_ERROR
    detail: Mapping[str, object] = field(default_factory=dict)

    @property
    def is_skipped(self) -> bool:
        """跳过是装配状态（缺连接、缺语料、重排未启用），不是本次请求的运行故障。

        调用方据此决定要不要记账：把"这台盒子没有 pgvector"写进每一次预警的降级列表，
        只会把真正的故障淹没。
        """
        return self.outcome == _OUTCOME_SKIPPED

    def as_dict(self) -> dict[str, object]:
        return {"leg": self.leg, "reason": self.reason, "outcome": self.outcome, "detail": dict(self.detail)}


@dataclass(frozen=True, slots=True)
class RetrievalOutcome:
    """检索结果 + 运行画像。`docs` 为空且 `degraded` 为真，就是降级的正确形状（不是异常）。"""

    docs: tuple[RetrievedDoc, ...]
    elapsed_ms: float
    budget_ms: float
    leg_timings_ms: Mapping[str, float]
    degradations: tuple[Degradation, ...] = ()
    query: str = ""

    @property
    def degraded(self) -> bool:
        return bool(self.degradations)

    @property
    def legs_completed(self) -> tuple[str, ...]:
        """真正跑完的腿：有计时记录（= 启动过）且没有降级记录。

        未启动的腿（缺连接/缺语料/预算耗尽跳过重排）不在这里出现——
        "跳过"与"跑完"必须是两个集合，否则降级在指标里会被读成成功。
        """
        failed = {item.leg for item in self.degradations}
        return tuple(leg for leg in (LEG_DENSE, LEG_LEXICAL, LEG_RERANK) if leg in self.leg_timings_ms and leg not in failed)

    def context_block(self, *, max_docs: int | None = None, per_doc_chars: int = 240) -> str:
        """渲染成可直接拼进预警正文的上下文块。"""
        return render_context(self.docs, max_docs=max_docs, per_doc_chars=per_doc_chars)

    def as_dict(self) -> dict[str, object]:
        return {
            "query": self.query,
            "count": len(self.docs),
            "elapsed_ms": round(self.elapsed_ms, 3),
            "budget_ms": self.budget_ms,
            "degraded": self.degraded,
            "legs_completed": list(self.legs_completed),
            "leg_timings_ms": {key: round(value, 3) for key, value in self.leg_timings_ms.items()},
            "degradations": [item.as_dict() for item in self.degradations],
        }


@dataclass(frozen=True, slots=True)
class _Candidate:
    """腿内候选：排名信息与取回的行绑在一起，避免两个列表按下标配对。"""

    doc_id: str
    leg: LegName
    score: float
    text: str
    hazard_type: str | None = None
    region_code: str | None = None
    source: str = ""
    updated_at: datetime | None = None

    @classmethod
    def from_hit(cls, hit: VectorHit) -> _Candidate:
        """密集腿的投影列里没有 chunk 全文（standardized_task_units 只有 objective）。

        这是 persistence.vectors 现有 SQL 的既有形状：正文之外不取大字段。
        预警知识 chunk 表接入后，同一位置换成 chunk 正文即可，此处不需第二套映射。
        """
        payload = hit.payload
        return cls(
            doc_id=hit.id,
            leg=LEG_DENSE,
            score=hit.score,
            text=str(payload.get("objective") or ""),
            hazard_type=_text_or_none(payload.get("hazard_type")),
            region_code=_text_or_none(payload.get("region_code")),
            source="pgvector:standardized_task_units",
            updated_at=_as_datetime(payload.get("created_at")),
        )

    @classmethod
    def from_doc(cls, doc: KnowledgeDoc, score: float) -> _Candidate:
        return cls(
            doc_id=doc.doc_id,
            leg=LEG_LEXICAL,
            score=score,
            text=doc.text,
            hazard_type=doc.hazard_type,
            region_code=doc.region_code,
            source=doc.source or "lexical_index",
            updated_at=doc.updated_at,
        )


class HybridRetrievalService:
    """混合检索装配体：无全局可变状态，依赖全部注入，同一实例可并发复用。"""

    def __init__(
        self,
        *,
        embedder: Embedder,
        lexicon: LexicalIndex,
        conn: Any | None = None,
        acquire: Callable[[], Any] | None = None,
        reranker: Reranker | None = None,
        tracer: Tracer | None = None,
        default_budget_ms: float = DEFAULT_BUDGET_MS,
        candidate_multiplier: int = CANDIDATE_MULTIPLIER,
        fusion_k: int = RRF_K,
        leg_weights: Mapping[LegName, float] | None = None,
        rerank_top_n: int = 20,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        if default_budget_ms <= 0:
            raise RetrievalArgumentError("default_budget_ms 必须为正", detail={"default_budget_ms": default_budget_ms})
        self._embedder = embedder
        self._lexicon = lexicon
        self._conn = conn
        self._acquire = acquire
        self._reranker = reranker
        self._tracer = tracer
        self._default_budget_ms = float(default_budget_ms)
        self._candidate_multiplier = candidate_multiplier
        self._fusion_k = fusion_k
        self._leg_weights = dict(leg_weights) if leg_weights else None
        self._rerank_top_n = max(1, rerank_top_n)
        self._clock = clock

    @property
    def embedder(self) -> Embedder:
        return self._embedder

    @property
    def reranker(self) -> Reranker | None:
        return self._reranker

    @property
    def default_budget_ms(self) -> float:
        return self._default_budget_ms

    @property
    def _has_connection(self) -> bool:
        return self._conn is not None or self._acquire is not None

    async def retrieve(self, query: RetrievalQuery) -> RetrievalOutcome:
        """一次混合检索：永远返回 outcome，运行故障记成 degradation。"""
        started = self._clock()
        budget_ms = self._default_budget_ms if query.budget_ms is None else float(query.budget_ms)
        if budget_ms <= 0:
            raise RetrievalArgumentError("budget_ms 必须为正", detail={"budget_ms": budget_ms})
        if not query.text.strip():
            # 空查询是合法输入（上游没拿到正文），不是降级：空结果 + 零耗时。
            return self._finish(query, (), started, budget_ms, {}, [])

        deadline = started + budget_ms / 1000.0
        timings: dict[str, float] = {}
        degradations: list[Degradation] = []

        # 只有"依赖真的在"的腿才启动：缺连接/缺语料是装配状态，跳过并记账，
        # 而不是把它塞进协程里再靠异常捕获——捕获到的降级和异常降级混在一起就查不动了。
        legs: list[LegName] = []
        jobs: list[Callable[[], Awaitable[Sequence[_Candidate]]]] = []
        if self._has_connection:
            legs.append(LEG_DENSE)
            jobs.append(lambda: self._dense_leg(query))
        else:
            degradations.append(Degradation(leg=LEG_DENSE, reason="未注入连接（conn/acquire），密集腿跳过", outcome=_OUTCOME_SKIPPED))
        if not self._lexicon.is_empty:
            legs.append(LEG_LEXICAL)
            jobs.append(lambda: self._lexical_leg(query))
        else:
            degradations.append(Degradation(leg=LEG_LEXICAL, reason="词法索引为空（语料未预置），词法腿跳过", outcome=_OUTCOME_SKIPPED))

        # 两条腿并发：词法腿是毫秒级 CPU 活，密集腿要等推理线程 + 一次 SQL 往返。
        # 并发让总时延趋于 max(腿) 而不是 sum(腿)——预算守得住主要靠这一步。
        # return_exceptions=True 保证两条腿都被取回（否则调用方参数错时另一条腿的异常会漂在事件循环里）。
        settled = await asyncio.gather(
            *(self._run_leg(leg, deadline, timings, degradations, job) for leg, job in zip(legs, jobs, strict=True))
        )
        for item in settled:
            if isinstance(item, BaseException):
                raise item
        rows_by_leg = dict(zip(legs, settled, strict=True))
        dense_rows: Sequence[_Candidate] = rows_by_leg.get(LEG_DENSE, ())
        lexical_rows: Sequence[_Candidate] = rows_by_leg.get(LEG_LEXICAL, ())

        # 词法腿后写：同一 doc_id 两边都命中时保留索引里的全文，
        # 密集腿的投影只有 objective 摘要，拿它覆盖会让注入给预警的正文变短。
        candidates = {item.doc_id: item for item in (*dense_rows, *lexical_rows)}
        fused = fuse(
            [
                Ranking.of(LEG_DENSE, ((item.doc_id, item.score) for item in dense_rows)),
                Ranking.of(LEG_LEXICAL, ((item.doc_id, item.score) for item in lexical_rows)),
            ],
            k=self._fusion_k,
            weights=self._leg_weights,
        )
        reranker = self._reranker if (query.rerank and self._reranker is not None and self._reranker.enabled) else None
        head = fused[: min(len(fused), self._rerank_top_n if reranker is not None else query.k)]
        docs = self._to_docs(head, candidates)
        if reranker is not None and docs:
            docs = await self._rerank(query, reranker, docs, deadline, timings, degradations)
        return self._finish(query, docs[: query.k], started, budget_ms, timings, degradations)

    # ------------------------------------------------------------------ 腿

    async def _run_leg(
        self,
        leg: LegName,
        deadline: float,
        timings: dict[str, float],
        degradations: list[Degradation],
        body: Callable[[], Awaitable[Sequence[_Candidate]]],
    ) -> tuple[_Candidate, ...]:
        """执行一条腿：超时与运行故障都在这里收成 degradation，不外泄。"""
        started = self._clock()
        remaining = deadline - started
        if remaining <= 0:
            timings[leg] = 0.0
            degradations.append(Degradation(leg=leg, reason="预算耗尽，未启动", outcome=_OUTCOME_SKIPPED))
            return ()
        try:
            rows = await asyncio.wait_for(body(), timeout=remaining)
        except TimeoutError:
            timings[leg] = (self._clock() - started) * 1000.0
            degradations.append(
                Degradation(
                    leg=leg,
                    reason=f"超过剩余预算 {remaining * 1000.0:.0f}ms",
                    outcome=_OUTCOME_TIMEOUT,
                    detail={"budget_left_ms": round(remaining * 1000.0, 1)},
                )
            )
            return ()
        except _PROPAGATE:
            raise
        except Exception as exc:  # 装载失败、库不可达、模型形状不符……一律降级
            timings[leg] = (self._clock() - started) * 1000.0
            degradations.append(
                Degradation(
                    leg=leg,
                    reason=str(exc) or type(exc).__name__,
                    outcome=_OUTCOME_ERROR,
                    detail={"cause_type": type(exc).__name__},
                )
            )
            log.warning("检索腿降级", extra={"leg": leg, "error": str(exc)[:200]})
            return ()
        timings[leg] = (self._clock() - started) * 1000.0
        return tuple(rows)

    async def _dense_leg(self, query: RetrievalQuery) -> Sequence[_Candidate]:
        """密集腿：query -> 嵌入（工作线程）-> pgvector 余弦 top-k（真 SQL 路径）。"""
        # 先嵌入再借连接：CPU 推理可能跑几百毫秒，握着池里的连接等推理等于白占协同链路的槽位。
        embedding = await asyncio.to_thread(embed_one, self._embedder, query.text)
        async with self._connection() as conn:
            hits = await search_top_k(
                conn,
                embedding,
                k=query.candidate_k(self._candidate_multiplier),
                hazard_type=query.hazard_type,
                region_code=query.region_code,
                since=query.since,
                until=query.until,
                score_threshold=query.score_threshold,
            )
        return [_Candidate.from_hit(hit) for hit in hits]

    async def _lexical_leg(self, query: RetrievalQuery) -> Sequence[_Candidate]:
        """词法腿：进程内 BM25；灾种/区域过滤用谓词，与密集腿的 WHERE 同语义。"""
        if self._lexicon.is_empty:
            return ()
        hits = await asyncio.to_thread(
            rank,
            self._lexicon,
            query.text,
            k=query.candidate_k(self._candidate_multiplier),
            where=_filter_for(query),
        )
        candidates: list[_Candidate] = []
        for hit in hits:
            doc = self._lexicon.get(hit.doc_id)
            if doc is not None:
                candidates.append(_Candidate.from_doc(doc, hit.score))
        return candidates

    @asynccontextmanager
    async def _connection(self) -> AsyncIterator[Any]:
        """密集腿的连接来源：注入的裸连接（测试/单例）或借还池连接（生产）。"""
        if self._conn is not None:
            yield self._conn
            return
        if self._acquire is not None:
            context = self._acquire()
            async with context as pooled:
                yield pooled
            return
        raise RetrievalArgumentError(
            "密集腿不可用：未注入 conn 或 acquire",
            detail={"embedder": self._embedder.model_id},
        )

    # ------------------------------------------------------------------ 融合与重排

    def _to_docs(self, fused: Sequence[FusedHit], candidates: Mapping[str, _Candidate]) -> tuple[RetrievedDoc, ...]:
        docs: list[RetrievedDoc] = []
        for position, hit in enumerate(fused, start=1):
            row = candidates.get(hit.doc_id)
            if row is None:  # pragma: no cover - fuse 的 id 全部来自候选
                continue
            docs.append(
                RetrievedDoc(
                    doc_id=hit.doc_id,
                    text=row.text,
                    rank=position,
                    score=hit.score,
                    provenance=Provenance(
                        legs=hit.legs,
                        leg_scores=hit.raw_scores,
                        fusion_score=hit.score,
                        fusion_rank=position,
                    ),
                    hazard_type=row.hazard_type,
                    region_code=row.region_code,
                    source=row.source,
                    updated_at=row.updated_at,
                )
            )
        return tuple(docs)

    async def _rerank(
        self,
        query: RetrievalQuery,
        reranker: Reranker,
        docs: Sequence[RetrievedDoc],
        deadline: float,
        timings: dict[str, float],
        degradations: list[Degradation],
    ) -> tuple[RetrievedDoc, ...]:
        """重排只在"还有预算"时发生；任何失败都退回 RRF 次序。"""
        remaining = deadline - self._clock()
        if remaining <= 0:
            degradations.append(Degradation(leg=LEG_RERANK, reason="预算耗尽，跳过重排", outcome=_OUTCOME_SKIPPED))
            return tuple(docs)
        pairs = [(doc.doc_id, doc.text) for doc in docs]
        started = self._clock()
        try:
            hits = await asyncio.wait_for(asyncio.to_thread(reranker.rerank, query.text, pairs), timeout=remaining)
        except TimeoutError:
            timings[LEG_RERANK] = (self._clock() - started) * 1000.0
            degradations.append(Degradation(leg=LEG_RERANK, reason="重排超时，保留 RRF 次序", outcome=_OUTCOME_TIMEOUT))
            return tuple(docs)
        except _PROPAGATE:
            raise
        except Exception as exc:
            timings[LEG_RERANK] = (self._clock() - started) * 1000.0
            degradations.append(
                Degradation(
                    leg=LEG_RERANK,
                    reason=str(exc) or type(exc).__name__,
                    outcome=_OUTCOME_ERROR,
                    detail={"cause_type": type(exc).__name__},
                )
            )
            return tuple(docs)
        timings[LEG_RERANK] = (self._clock() - started) * 1000.0

        by_id = {doc.doc_id: doc for doc in docs}
        reranked: list[RetrievedDoc] = []
        for hit in hits:
            doc = by_id.pop(hit.doc_id, None)
            if doc is None:
                # 重排器报了个不在候选集里的 id：不采信，宁可少一条也不能引入无凭证内容。
                log.warning("重排返回了未知 doc_id", extra={"doc_id": hit.doc_id})
                continue
            reranked.append(_with_rerank(doc, score=hit.score))
        # 没被重排器返回的候选排在其后：它们仍在融合结果里，只是没拿到重排分。
        return _renumber(tuple(reranked) + tuple(by_id.values()))

    # ------------------------------------------------------------------ 记账

    def _finish(
        self,
        query: RetrievalQuery,
        docs: Sequence[RetrievedDoc],
        started: float,
        budget_ms: float,
        timings: Mapping[str, float],
        degradations: Sequence[Degradation],
    ) -> RetrievalOutcome:
        elapsed_ms = max((self._clock() - started) * 1000.0, 0.0)
        outcomes = {item.outcome for item in degradations}
        if _OUTCOME_TIMEOUT in outcomes:
            total_outcome = _OUTCOME_TIMEOUT
        elif degradations:
            total_outcome = _OUTCOME_ERROR
        else:
            total_outcome = _OUTCOME_OK
        for leg, value in timings.items():
            self._record(f"retrieval_{leg}_ms", value, trace_id=query.trace_id)
        self._record(METRIC_TOTAL, elapsed_ms, outcome=total_outcome, trace_id=query.trace_id)
        return RetrievalOutcome(
            docs=tuple(docs),
            elapsed_ms=elapsed_ms,
            budget_ms=budget_ms,
            leg_timings_ms=dict(timings),
            degradations=tuple(degradations),
            query=query.text,
        )

    def _record(self, name: str, ms: float, *, outcome: str = _OUTCOME_OK, trace_id: str | None = None) -> None:
        """指标只走 Tracer 这一个出口：检索时延要与其它考核指标同账本，才可比。"""
        if self._tracer is None:
            return
        try:
            self._tracer.record(name, ms, outcome=outcome, trace_id=trace_id)
        except Exception:  # pragma: no cover - 记账失败绝不能反过来影响检索
            log.debug("检索指标记账失败", extra={"metric": name})


def _renumber(docs: Sequence[RetrievedDoc]) -> tuple[RetrievedDoc, ...]:
    return tuple(_at_rank(doc, position) for position, doc in enumerate(docs, start=1))


def _at_rank(doc: RetrievedDoc, rank: int) -> RetrievedDoc:
    if doc.rank == rank:
        return doc
    return RetrievedDoc(
        doc_id=doc.doc_id,
        text=doc.text,
        rank=rank,
        score=doc.score,
        provenance=doc.provenance,
        hazard_type=doc.hazard_type,
        region_code=doc.region_code,
        source=doc.source,
        updated_at=doc.updated_at,
    )


def _with_rerank(doc: RetrievedDoc, *, score: float) -> RetrievedDoc:
    provenance = Provenance(
        legs=(*doc.provenance.legs, LEG_RERANK),
        leg_scores={**doc.provenance.leg_scores, LEG_RERANK: score},
        fusion_score=doc.provenance.fusion_score,
        fusion_rank=doc.provenance.fusion_rank,
        rerank_score=score,
    )
    return RetrievedDoc(
        doc_id=doc.doc_id,
        text=doc.text,
        rank=doc.rank,
        score=score,
        provenance=provenance,
        hazard_type=doc.hazard_type,
        region_code=doc.region_code,
        source=doc.source,
        updated_at=doc.updated_at,
    )


def _filter_for(query: RetrievalQuery) -> Callable[[KnowledgeDoc], bool]:
    def predicate(doc: KnowledgeDoc) -> bool:
        return doc.matches(hazard_type=query.hazard_type, region_code=query.region_code)

    return predicate


def _text_or_none(value: Any) -> str | None:
    if value is None or value == "":
        return None
    return str(value)


def _as_datetime(value: Any) -> datetime | None:
    return value if isinstance(value, datetime) else None


def render_context(docs: Iterable[RetrievedDoc], *, max_docs: int | None = None, per_doc_chars: int = 240) -> str:
    """把召回渲染成注入预警正文的上下文块（无结果返回空串，调用方据此不注入）。"""
    lines: list[str] = []
    taken = list(docs) if max_docs is None else list(docs)[:max_docs]
    for doc in taken:
        excerpt = " ".join(doc.text.split())
        if per_doc_chars > 0:
            excerpt = excerpt[:per_doc_chars]
        provenance = "+".join(doc.provenance.legs)
        lines.append(f"[{doc.rank}] ({provenance} {doc.score:.3f}) {excerpt}")
    return "\n".join(lines)


__all__ = [
    "CANDIDATE_MULTIPLIER",
    "DEFAULT_BUDGET_MS",
    "METRIC_TOTAL",
    "Degradation",
    "HybridRetrievalService",
    "RetrievalOutcome",
    "RetrievalQuery",
    "render_context",
]
