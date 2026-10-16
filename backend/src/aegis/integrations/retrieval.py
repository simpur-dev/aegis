"""混合检索三条腿（词法/pgvector/重排）的装配与预热；权重缺失不报错，切哈希并在状态行标 degraded。（P2 纯搬运：函数体逐字未改。）"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aegis.config import Settings, get_settings
from aegis.integrations.shared import log
from aegis.integrations.state import IntegrationState
from aegis.knowledge.cases import HazardCase, load_builtin_cases
from aegis.observability.tracer import Tracer

if TYPE_CHECKING:
    from aegis.retrieval.embedder import Embedder
    from aegis.retrieval.reranker import Reranker
    from aegis.retrieval.service import HybridRetrievalService


# 重排候选上限：与链路实际取用的上下文块数（_CONTEXT_DOC_LIMIT=5）对齐。
# 本机实测真实语料正文（~305 字）5 对 4201ms、10 对 8361ms——多排的候选会被 k 截断丢掉，
# 等于花一倍时延去给"注定不注入的块"排序。改这个数字必须同步改 retrieval_budget_ms。
_RERANK_TOP_N = 5


def build_retrieval(
    settings: Settings | None = None,
    tracer: Tracer | None = None,
    *,
    store: object | None = None,
    cases: Sequence[HazardCase] | None = None,
) -> tuple[HybridRetrievalService | None, IntegrationState]:
    """混合检索装配：词法腿零外部依赖，密集腿要 pgvector 连接，重排腿要 ONNX 权重。

    三条腿的可用性在这里一次说清并写进状态，而不是等到第一次检索时才在 degradation 里露头：
    现场调"≤3min 预警生成"的人需要知道这台盒子上检索到底是全功能还是降级件。
    权重缺失时**不报错**：切 `HashingEmbedder`（确定性词面近似）并在状态里标 degraded——
    它的分数不得用于任何准确率口径，这一点由 `driver`（模型身份）与 `degraded`（降级原因）共同守门。
    """
    from aegis.retrieval.corpus import docs_from_cases
    from aegis.retrieval.lexical import build_lexical_index
    from aegis.retrieval.service import HybridRetrievalService

    cfg = settings or get_settings()
    if not cfg.retrieval_enabled:
        return None, IntegrationState(name="retrieval", enabled=False, driver="off")

    corpus = docs_from_cases(cases if cases is not None else load_builtin_cases())
    embedder, embedder_note = _select_embedder(cfg)
    reranker, reranker_note = _select_reranker(cfg)
    index, index_note = _select_index(cfg)
    acquire = getattr(store, "acquire", None)
    notes = [note for note in (embedder_note, reranker_note, index_note) if note]

    service = HybridRetrievalService(
        embedder=embedder,
        lexicon=build_lexical_index(corpus),
        acquire=acquire,
        index=index,
        # 只有外部索引才需要启动期灌数；本地腿直接吃 lexicon，白存一份语料只会让人以为它在被用。
        corpus=corpus if index is not None else (),
        reranker=reranker,
        tracer=tracer,
        default_budget_ms=cfg.retrieval_budget_ms,
        # 重排候选上限由实测反推：10 对中位 1514ms、20 对 3005ms（本机 int8/CPU）。
        # 买不起的候选数量不会让结果更对，只会让重排腿在预算内必然超时——那等于白装这根腿。
        rerank_top_n=_RERANK_TOP_N,
    )
    return service, IntegrationState(
        name="retrieval",
        enabled=True,
        driver=embedder.model_id,
        detail={
            "budget_ms": cfg.retrieval_budget_ms,
            "corpus_docs": len(corpus),
            # 密集腿是 pgvector 上的历史任务单元，没有连接时服务会把它记成"跳过"而不是失败
            "dense_leg": bool(acquire is not None) or index is not None,
            "rerank_leg": reranker.enabled,
            "model_dir": cfg.retrieval_model_dir,
            "index_backend": cfg.retrieval_index_backend,
            **({"index": index.status()} if index is not None else {}),
            "degraded": ";".join(notes),
        },
    )


def _select_index(cfg: Settings) -> tuple[Any | None, str | None]:
    """检索索引引擎选择：`local` 不建外部索引（两腿留在 pgvector + 进程内 BM25）。

    选 seekdb 时这里只构造对象、不碰网络：建表与灌数留在启动期的 `prepare_index()`，
    连接失败会作为降级原因出现在装配行上，而不是让进程起不来。
    """
    if cfg.retrieval_index_backend != "seekdb":
        return None, None
    try:
        from aegis.retrieval.seekdb import SeekdbConfig, SeekdbKnowledgeIndex
    except ImportError as exc:  # pragma: no cover - 可选依赖缺失
        return None, f"seekdb 适配层不可导入（{type(exc).__name__}），两腿回落本地实现"

    index = SeekdbKnowledgeIndex(
        SeekdbConfig(
            host=cfg.seekdb_host,
            port=cfg.seekdb_port,
            user=cfg.seekdb_user,
            password=cfg.seekdb_password,
            database=cfg.seekdb_database,
            table=cfg.seekdb_table,
        )
    )
    return index, None


async def warm_retrieval(service: HybridRetrievalService | None) -> str | None:
    """把 ONNX 会话装载挪出第一条预警的路径：本机冷启动 embed 2102ms、rerank 4732ms。

    惰性装载是必要的（装配阶段不许阻塞事件循环），但它把装载费转给了第一次检索——
    对"≤3min 预警生成"来说，让第一条预警替后面所有请求付这笔钱不划算，所以放到启动期。
    装载失败不抛异常：返回失败原因交给调用方记账，运行期该腿会自己记降级。
    """
    if service is None:
        return None
    try:
        # 外部索引（seekdb）先建表灌数：这一步没做成，两腿检索会全部记降级而不是"能跑但很慢"。
        await service.prepare_index()
    except Exception as exc:
        log.warning("检索索引预热失败，两腿将在运行期降级", extra={"err": type(exc).__name__})
        return f"索引预热失败: {type(exc).__name__}"
    try:
        await asyncio.to_thread(service.embedder.embed, ["预警检索预热"])
        reranker = service.reranker
        if reranker is not None and reranker.enabled:
            await asyncio.to_thread(reranker.rerank, "预热", [("warm", "预警检索预热")])
    except Exception as exc:
        log.warning("检索预热失败，对应腿将在运行期降级", extra={"err": type(exc).__name__})
        return type(exc).__name__
    return None


def _select_embedder(cfg: Settings) -> tuple[Embedder, str | None]:
    from aegis.retrieval.embedder import HashingEmbedder, OnnxEmbedder
    from aegis.retrieval.onnx_io import EMBEDDER_MODEL_DIR

    root = Path(cfg.retrieval_model_dir) / EMBEDDER_MODEL_DIR
    if _weights_present(root):
        return OnnxEmbedder.from_dir(root), None
    return HashingEmbedder(), f"bge-m3 权重缺失（{root}），密集嵌入降级为确定性哈希（分数不得用于准确率口径）"


def _select_reranker(cfg: Settings) -> tuple[Reranker, str | None]:
    from aegis.retrieval.onnx_io import RERANKER_MODEL_DIR
    from aegis.retrieval.reranker import NoopReranker, OnnxReranker

    root = Path(cfg.retrieval_model_dir) / RERANKER_MODEL_DIR
    if _weights_present(root):
        return OnnxReranker.from_dir(root), None
    return NoopReranker(), f"bge-reranker 权重缺失（{root}），重排腿关闭"


def _weights_present(root: Path) -> bool:
    """只查文件在不在：装载 568MB 权重必须留在第一次检索之前、启动线程里完成。"""
    from aegis.retrieval.onnx_io import MODEL_TOKENIZER_FILE, MODEL_WEIGHTS_FILE

    return (root / MODEL_WEIGHTS_FILE).is_file() and (root / MODEL_TOKENIZER_FILE).is_file()
