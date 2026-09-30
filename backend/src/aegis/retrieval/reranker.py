"""重排腿：cross-encoder 对 (query, passage) 逐对打分，只在融合后的头部候选上跑。

顺序是"检索 -> 融合 -> 重排"而不是"边检索边重排"：重排的代价是每条候选一次完整
前向（bge-reranker-v2-m3 int8 在 CPU 上远慢于一次 1024 维向量点积），
把 LLM/交叉编码器放进召回回路会让时延不可预算。RRF 先把候选压到 top-n，
重排只看这 n 条，SLA 才守得住。

`enabled` 是协议的一部分：调用方要能问"这次重排是真推理还是恒等序"，
而不是靠 isinstance 猜实现类型——降级状态必须进凭证，预警依据要可审计。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from aegis.retrieval.onnx_io import (
    RerankShapeError,
    RetrievalArgumentError,
    SessionFactory,
    SessionLike,
    TokenizerFactory,
    TokenizerLike,
    build_feeds,
    encode,
    load_model_config,
    load_session,
    load_tokenizer,
    numpy,
    pad_token_id_of,
    session_input_names,
    sigmoid,
)

DEFAULT_MAX_LENGTH = 512


@dataclass(frozen=True, slots=True)
class RerankedHit:
    """一条重排结果：score 是 cross-encoder 的 logits[0]（默认不做 sigmoid，见 OnnxReranker）。"""

    doc_id: str
    score: float


@runtime_checkable
class Reranker(Protocol):
    """重排面：给定 query 与 (doc_id, text) 候选，返回按相关性降序的同一批候选。

    实现不得增删候选（数量与 id 集合都必须守恒），否则融合凭证与最终排名会对不上。
    """

    name: str
    enabled: bool

    def rerank(self, query: str, candidates: Sequence[tuple[str, str]]) -> list[RerankedHit]: ...


class OnnxReranker:
    """bge-reranker-v2-m3 交叉编码器（ONNX int8，CPU）。

    分数取模型 logits 首列，不做 sigmoid：名次只依赖大小关系，sigmoid 是单调变换，
    加它只会给日志里的数字一种" calibrated 概率"的错觉。要落 [0,1] 阈值口径时再传 `squash=True`。

    会话与分词器注入，理由与 OnnxEmbedder 相同：单测用假会话驱动真实代码路径。
    """

    def __init__(
        self,
        *,
        session_factory: SessionFactory,
        tokenizer_factory: TokenizerFactory,
        name: str = "bge-reranker-v2-m3-int8",
        max_length: int = DEFAULT_MAX_LENGTH,
        batch_size: int = 4,
        squash: bool = False,
        pad_token_id: int | None = None,
    ) -> None:
        if batch_size <= 0:
            raise RetrievalArgumentError("batch_size 必须为正", detail={"batch_size": batch_size})
        self.name = name
        self.enabled = True
        self.max_length = max_length
        self.batch_size = batch_size
        self.squash = squash
        self._session_factory = session_factory
        self._tokenizer_factory = tokenizer_factory
        self._session: SessionLike | None = None
        self._tokenizer: TokenizerLike | None = None
        self._pad_token_id: int | None = pad_token_id

    @classmethod
    def from_dir(
        cls,
        model_dir: str | Path,
        *,
        model_name: str = "model_int8.onnx",
        tokenizer_name: str = "tokenizer.json",
        name: str = "bge-reranker-v2-m3-int8",
        max_length: int = DEFAULT_MAX_LENGTH,
        batch_size: int = 4,
        intra_op_threads: int | None = None,
    ) -> OnnxReranker:
        root = Path(model_dir)
        config = load_model_config(root)
        pad_token_id = config.get("pad_token_id")
        return cls(
            session_factory=lambda: load_session(root / model_name, intra_op_threads=intra_op_threads),
            tokenizer_factory=lambda: load_tokenizer(root / tokenizer_name),
            name=name,
            max_length=max_length,
            batch_size=batch_size,
            pad_token_id=int(pad_token_id) if isinstance(pad_token_id, int) else None,
        )

    def rerank(self, query: str, candidates: Sequence[tuple[str, str]]) -> list[RerankedHit]:
        if not candidates:
            return []
        session = self._ensure_session()
        tokenizer = self._ensure_tokenizer()
        scores: list[float] = []
        for start in range(0, len(candidates), self.batch_size):
            chunk = candidates[start : start + self.batch_size]
            scores.extend(self._score_chunk(session, tokenizer, query, chunk))
        if len(scores) != len(candidates):
            raise RerankShapeError(
                "重排分条数与候选条数不符",
                detail={"candidates": len(candidates), "scores": len(scores)},
            )
        ordered = sorted(
            (RerankedHit(doc_id=doc_id, score=score) for (doc_id, _), score in zip(candidates, scores, strict=True)),
            key=lambda hit: (-hit.score, hit.doc_id),
        )
        return ordered

    def _score_chunk(self, session: SessionLike, tokenizer: TokenizerLike, query: str, chunk: Sequence[tuple[str, str]]) -> list[float]:
        pairs: list[tuple[str, str]] = [(query, text if text.strip() else " ") for _, text in chunk]
        batch = encode(tokenizer, pairs, max_length=self.max_length, pad_token_id=self._pad_token_id)
        feeds = build_feeds(batch, input_names=session_input_names(session))
        outputs = session.run(None, feeds)
        if not outputs:
            raise RerankShapeError("重排会话没有输出张量")
        rows = _first_column(outputs[0])
        if len(rows) != len(pairs):
            raise RerankShapeError(
                "重排输出行数与批次不符",
                detail={"rows": len(rows), "pairs": len(pairs)},
            )
        return [sigmoid(value) for value in rows] if self.squash else rows

    def _ensure_session(self) -> SessionLike:
        if self._session is None:
            self._session = self._session_factory()
        return self._session

    def _ensure_tokenizer(self) -> TokenizerLike:
        if self._tokenizer is None:
            tokenizer = self._tokenizer_factory()
            if self._pad_token_id is None:
                self._pad_token_id = pad_token_id_of(tokenizer)
            self._tokenizer = tokenizer
        return self._tokenizer


class NoopReranker:
    """恒等序重排器：原样返回候选顺序，分全给 0。

    它代表"这台盒子没有重排能力"（权重未预置、或预算已耗尽），
    存在的意义是让装配处始终拿到一个 Reranker，而 `enabled=False` 让服务
    不把这份假分数写进凭证。
    """

    def __init__(self, *, name: str = "none") -> None:
        self.name = name
        self.enabled = False

    def rerank(self, query: str, candidates: Sequence[tuple[str, str]]) -> list[RerankedHit]:
        return [RerankedHit(doc_id=doc_id, score=0.0) for doc_id, _ in candidates]


def _first_column(output: object) -> list[float]:
    """取 logits 的首列：XLMRobertaForSequenceClassification 单标签时形状为 [batch, 1]。"""
    np = numpy()
    array = output if isinstance(output, np.ndarray) else np.asarray(output)
    if array.ndim == 1:
        return [float(value) for value in array]
    if array.ndim == 2:
        return [float(row[0]) for row in array]
    raise RerankShapeError("重排输出秩不为 1 或 2", detail={"ndim": int(array.ndim), "shape": list(array.shape)})


__all__ = [
    "DEFAULT_MAX_LENGTH",
    "NoopReranker",
    "OnnxReranker",
    "RerankedHit",
    "Reranker",
]
