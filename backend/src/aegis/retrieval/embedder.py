"""嵌入腿：文本 -> 定长密集向量（本仓口径 1024 维，与 pgvector 列定义同源）。

协议只有一个同步方法 `embed(texts) -> list[vector]`。刻意不做 async：
ONNX 是 CPU 阻塞推理，放到事件循环里会把整台盒子的协同链路卡住；
正确的投递方式是工作线程（`asyncio.to_thread`），由 service.py 负责，
所以这里保持纯同步、可脱离事件循环单测。

实现两把：
- `OnnxEmbedder`：bge-m3 int8 / CPU。会话与分词器**注入**（factory），
  因此假对象能驱动真实代码路径，单测不需要权重文件。
- `HashingEmbedder`：确定性哈希向量，语义为零的降级件（见其 docstring 的诚实声明）。
"""

from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Sequence
from math import log
from pathlib import Path
from typing import Protocol, runtime_checkable

from aegis.persistence.rows import EMBEDDING_DIM
from aegis.retrieval.lexical import tokenize
from aegis.retrieval.onnx_io import (
    EmbeddingShapeError,
    RetrievalArgumentError,
    SessionFactory,
    SessionLike,
    TokenizerFactory,
    TokenizerLike,
    build_feeds,
    encode,
    l2_normalize,
    load_model_config,
    load_session,
    load_tokenizer,
    pad_token_id_of,
    pick_embedding_output,
    pool_and_normalize,
    session_input_names,
)

# 与 bge-m3 的 8194 位上下文相比刻意保守：预警 chunk 都在几十至两百字，
# 序列越长 int8 CPU 推理越慢（近似二次方），检索预算里买不起这个长度。
DEFAULT_MAX_LENGTH = 512


@runtime_checkable
class Embedder(Protocol):
    """嵌入面的全部要求：报出自己的身份与维度，能把一批文本变成等长向量。"""

    model_id: str
    dim: int

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class OnnxEmbedder:
    """bge-m3 密集嵌入（ONNX int8，CPU ExecutionProvider）。

    `session_factory` / `tokenizer_factory` 惰性调用并缓存：装载一次 568MB 权重
    要几秒到几十秒，绝不能发生在第一次检索的请求路径上（那会让首条预警直接吃满预算）。
    装载失败抛 `ModelUnavailableError`，由调用方决定切降级腿——本类自己不做降级决策。
    """

    def __init__(
        self,
        *,
        session_factory: SessionFactory,
        tokenizer_factory: TokenizerFactory,
        model_id: str = "bge-m3-int8",
        dim: int = EMBEDDING_DIM,
        max_length: int = DEFAULT_MAX_LENGTH,
        pool: str = "cls",
        batch_size: int = 8,
        pad_token_id: int | None = None,
    ) -> None:
        if dim <= 0:
            raise RetrievalArgumentError("dim 必须为正", detail={"dim": dim})
        if batch_size <= 0:
            raise RetrievalArgumentError("batch_size 必须为正", detail={"batch_size": batch_size})
        self.model_id = model_id
        self.dim = dim
        self.max_length = max_length
        self.pool = pool
        self.batch_size = batch_size
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
        model_id: str = "bge-m3-int8",
        max_length: int = DEFAULT_MAX_LENGTH,
        intra_op_threads: int | None = None,
        dim: int = EMBEDDING_DIM,
    ) -> OnnxEmbedder:
        """按 `scripts/fetch_retrieval_models.py` 落地的目录约定装配。

        config.json 在场时先对一次 hidden_size：pgvector 的列宽是 1024，
        指向一个 768 维模型属于装配错误，必须在启动时就响，而不是等第一条预警超时。
        """
        root = Path(model_dir)
        config = load_model_config(root)
        hidden = config.get("hidden_size")
        if isinstance(hidden, int) and hidden != dim:
            raise EmbeddingShapeError(
                "模型 hidden_size 与向量列宽不符",
                detail={"model_dir": str(root), "hidden_size": hidden, "column_dim": dim},
            )
        pad_token_id = config.get("pad_token_id")
        return cls(
            session_factory=lambda: load_session(root / model_name, intra_op_threads=intra_op_threads),
            tokenizer_factory=lambda: load_tokenizer(root / tokenizer_name),
            model_id=model_id,
            dim=dim,
            max_length=max_length,
            pad_token_id=int(pad_token_id) if isinstance(pad_token_id, int) else None,
        )

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        session = self._ensure_session()
        tokenizer = self._ensure_tokenizer()
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self.batch_size):
            chunk = [text if text.strip() else " " for text in texts[start : start + self.batch_size]]
            vectors.extend(self._embed_chunk(session, tokenizer, chunk))
        if len(vectors) != len(texts):
            raise EmbeddingShapeError(
                "嵌入条数与输入条数不符",
                detail={"inputs": len(texts), "vectors": len(vectors)},
            )
        return vectors

    def _embed_chunk(self, session: SessionLike, tokenizer: TokenizerLike, texts: Sequence[str]) -> list[list[float]]:
        batch = encode(tokenizer, list(texts), max_length=self.max_length, pad_token_id=self._pad_token_id)
        feeds = build_feeds(batch, input_names=session_input_names(session))
        outputs = session.run(None, feeds)
        tensor = pick_embedding_output(outputs, [str(node.name) for node in session.get_outputs()])
        return pool_and_normalize(
            tensor,
            attention_mask=batch.attention_mask,
            pool=self.pool,
            dim=self.dim,
        )

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


class HashingEmbedder:
    """确定性哈希嵌入（1024 维）：字 n-gram 哈希 + 符号位 + 词频饱和 + L2 归一。

    **这是降级件，不是语义模型。** 它没有任何跨词面泛化能力：
    "滑坡" 与 "塌方"、"冰湖溃决" 与 "湖盆突水" 在它眼里毫不相似；同义、指代、上下文一律不建模。
    它只保证两件事——确定性（同输入同向量，逐位相同）与方向可分（词面重叠越多，余弦越接近 1）。

    存在意义有二：
    1. bge-m3 权重缺失/装载失败时，dense 腿仍能给出"词面接近"的召回，
       与词法腿合起来不至于退化成空结果（预警路径宁可有凭证可用的次优解，也不要 500）；
    2. 单测里作为可预期的嵌入替身，把 OnnxEmbedder 之外的代码路径钉死。

    它的分数不得用于任何准确率口径的汇报（见 REPORT：需要指标看板把它标成 degraded 源）。
    无第三方依赖：只用 hashlib/math/pathlib，因此不 import numpy、tokenizers、onnxruntime。
    """

    SIGN_SALT = "aegis-retrieval-sign"

    def __init__(self, *, dim: int = EMBEDDING_DIM, model_id: str = "hashing-ngram-1024") -> None:
        if dim <= 0:
            raise RetrievalArgumentError("dim 必须为正", detail={"dim": dim})
        self.dim = dim
        self.model_id = model_id

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._embed_one(text) for text in texts]

    def _embed_one(self, text: str) -> list[float]:
        counts = Counter(tokenize(text))
        vector = [0.0] * self.dim
        for token, freq in counts.items():
            index = self._bucket(token)
            vector[index] += self._sign(token) * (1.0 + log(freq))
        return l2_normalize(vector)

    def _bucket(self, token: str) -> int:
        return int.from_bytes(self._digest(token, salt=""), "big") % self.dim

    def _sign(self, token: str) -> float:
        # 符号位取自另一次哈希：与桶位共用摘要会让符号与桶相关，
        # 使同一批 token 在少数维度上系统性抵消（实测表现为向量偏短）。
        digest = int.from_bytes(self._digest(token, salt=self.SIGN_SALT), "big")
        return 1.0 if digest & 1 else -1.0

    def _digest(self, token: str, *, salt: str) -> bytes:
        return hashlib.blake2b((salt + token).encode("utf-8"), digest_size=8).digest()


def embed_one(embedder: Embedder, text: str) -> list[float]:
    """单条便捷封装（含维度护栏）。

    维度错误在嵌入腿就地拦住：让它流到 pgvector 只会变成一次无谓的往返，
    而 persistence 的 `VectorEmbeddingError` 仍会在真正下发 SQL 前兜住（两处判据同源于 EMBEDDING_DIM）。
    """
    vectors = embedder.embed([text])
    if not vectors:
        raise EmbeddingShapeError("嵌入器返回空批次", detail={"model_id": embedder.model_id})
    vector = vectors[0]
    if len(vector) != embedder.dim:
        raise EmbeddingShapeError(
            "向量维度与声明不符",
            detail={"model_id": embedder.model_id, "expected": embedder.dim, "actual": len(vector)},
        )
    return vector


__all__ = [
    "DEFAULT_MAX_LENGTH",
    "Embedder",
    "HashingEmbedder",
    "OnnxEmbedder",
    "embed_one",
]
