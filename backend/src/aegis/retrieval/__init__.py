"""检索层（retrieval）：预警生成的混合召回——pgvector 密集腿 + 进程内 BM25 词法腿 + RRF 融合 + 可选交叉编码重排。

分层（依赖只向下，任何文件都不导入 aegis.api / aegis.services）：

    docs.py       语料记录与召回产物的数据形状（纯数据）
    onnx_io.py    ONNX 运行时窄 I/O 面 + 本包类型化错误（叶子模块，可选依赖在此延迟导入）
    embedder.py   Embedder 协议 + OnnxEmbedder（bge-m3 int8/CPU）+ HashingEmbedder（降级件）
    lexical.py    中文 BM25（字 bigram + ASCII 词），纯函数、无 I/O、无新 Postgres 扩展
    fusion.py     Reciprocal Rank Fusion 的数学（名次进、凭证出）
    reranker.py   Reranker 协议 + OnnxReranker（bge-reranker-v2-m3 int8）+ NoopReranker
    service.py    HybridRetrievalService：并发双腿 -> 融合 -> 重排 -> 带预算与降级的 outcome

服务口径：`budget_ms` 到点交卷，运行故障一律记成 degradation 而不是抛进预警路径；
LLM 不出现在本包的 import 图上——检索必须在 3 分钟预警生成预算内自主完成，
把语言模型放进召回回路会让时延与失败面都不可预算。

权重装载：`scripts/fetch_retrieval_models.py` 离线预置，运行期不做隐式下载。
本包在没有 onnxruntime / tokenizers / 权重的环境里依然可以 import 并跑词法腿与降级腿。
"""

from __future__ import annotations

from aegis.retrieval.docs import (
    LEG_DENSE,
    LEG_LEXICAL,
    LEG_RERANK,
    KnowledgeDoc,
    LegName,
    Provenance,
    RetrievedDoc,
    to_embedding_input,
)
from aegis.retrieval.embedder import Embedder, HashingEmbedder, OnnxEmbedder, embed_one
from aegis.retrieval.fusion import RRF_K, Contribution, FusedHit, Ranking, fuse
from aegis.retrieval.lexical import LexicalHit, LexicalIndex, build_lexical_index, rank, tokenize
from aegis.retrieval.onnx_io import (
    EmbeddingShapeError,
    ModelUnavailableError,
    RerankShapeError,
    RetrievalArgumentError,
    RetrievalError,
    SessionFactory,
    SessionLike,
    TokenizerFactory,
    TokenizerLike,
)
from aegis.retrieval.reranker import NoopReranker, OnnxReranker, RerankedHit, Reranker
from aegis.retrieval.service import (
    DEFAULT_BUDGET_MS,
    Degradation,
    HybridRetrievalService,
    RetrievalOutcome,
    RetrievalQuery,
    render_context,
)

__all__ = [
    "DEFAULT_BUDGET_MS",
    "LEG_DENSE",
    "LEG_LEXICAL",
    "LEG_RERANK",
    "RRF_K",
    "Contribution",
    "Degradation",
    "Embedder",
    "EmbeddingShapeError",
    "FusedHit",
    "HashingEmbedder",
    "HybridRetrievalService",
    "KnowledgeDoc",
    "LegName",
    "LexicalHit",
    "LexicalIndex",
    "ModelUnavailableError",
    "NoopReranker",
    "OnnxEmbedder",
    "OnnxReranker",
    "Provenance",
    "Ranking",
    "RerankShapeError",
    "RerankedHit",
    "Reranker",
    "RetrievalArgumentError",
    "RetrievalError",
    "RetrievalOutcome",
    "RetrievalQuery",
    "RetrievedDoc",
    "SessionFactory",
    "SessionLike",
    "TokenizerFactory",
    "TokenizerLike",
    "build_lexical_index",
    "embed_one",
    "fuse",
    "rank",
    "render_context",
    "to_embedding_input",
    "tokenize",
]
