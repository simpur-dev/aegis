"""ONNX 运行时的窄 I/O 面 + 本包类型化错误的收口点。

为什么错误类型放在这里：本模块是包内最底层的叶子（不 import 同包任何其它模块），
对齐 `persistence/errors.py` 的既有做法——底层异常不外泄成 onnxruntime/驱动的原生类型，
在这一层收口后带上可安全落日志的上下文重抛。

为什么 onnxruntime / tokenizers / numpy 全部延迟导入：
- 边缘盒可能只有 CPU 版 onnxruntime，也可能压根没装（`[retrieval]` extra 是可选的）。
  `aegis.retrieval` 必须在这两种机器上都能 import——词法腿与 HashingEmbedder 不需要它们。
- 单元测试因此可以用假 session/假 tokenizer 驱动 `OnnxEmbedder` 的真实代码路径，
  不需要权重文件（见 tests/unit/test_retrieval_embedder.py）。

int8 + CPU 的线程口径也集中在 `load_session`：这是唯一一处需要知道 ORT 会话调优细节的地方。
"""

from __future__ import annotations

import json
import math
import os
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import Any, Final, Protocol, TypeAlias, runtime_checkable

from aegis.errors import AegisError, ErrorCode

# --------------------------------------------------------------------------- 模型卷布局
# 子目录名与文件名是装配层与取件脚本之间唯一的约定面，因此在这里定义一次、两边引用：
# `scripts/fetch_retrieval_models.py` 落盘的目录必须与此相同（tests/unit/test_retrieval_wiring.py 守着）。
# 两个模型的权重同名（model_int8.onnx + tokenizer.json），所以必须分目录存放。
# 根目录本身由 `Settings.retrieval_model_dir` 决定（脚本默认写 backend/data/models）。

EMBEDDER_MODEL_DIR: Final = "bge-m3-int8"
RERANKER_MODEL_DIR: Final = "bge-reranker-v2-m3-int8"
MODEL_WEIGHTS_FILE: Final = "model_int8.onnx"
MODEL_TOKENIZER_FILE: Final = "tokenizer.json"


# --------------------------------------------------------------------------- 类型化错误


class RetrievalError(AegisError):
    """检索层错误基类。子类按"改调用点"还是"降级继续"分岔，与持久层同一判据。"""

    code = ErrorCode.INTERNAL


class ModelUnavailableError(RetrievalError):
    """权重或分词器缺失/装载失败：边缘站点的常态，调用方应据此切到降级腿。"""

    code = ErrorCode.NOT_READY


class EmbeddingShapeError(RetrievalError):
    """模型输出不是 [batch, seq, hidden] 或 hidden != 声明维度：权重与代码假设不符。"""

    code = ErrorCode.INTERNAL


class RerankShapeError(RetrievalError):
    """交叉编码器输出条数与候选数不一致：宁可报错也不要静默按顺序配错分。"""

    code = ErrorCode.INTERNAL


class RetrievalArgumentError(RetrievalError):
    """调用方参数被拒绝（尚未开始推理）：属于要修代码的错误，绝不当成降级吞掉。"""

    code = ErrorCode.SCHEMA_INVALID


# --------------------------------------------------------------------------- 注入用的窄协议


@runtime_checkable
class SessionLike(Protocol):
    """onnxruntime.InferenceSession 的用到面（假会话据此替入）。"""

    def run(self, output_names: Sequence[str] | None, input_feed: Mapping[str, Any], run_options: Any = None) -> list[Any]: ...

    def get_inputs(self) -> list[Any]: ...

    def get_outputs(self) -> list[Any]: ...


class TokenizerLike(Protocol):
    """huggingface_tokenizers.Tokenizer 的用到面（假分词器据此替入）。"""

    def encode_batch(self, inputs: Any) -> list[Any]: ...

    def enable_truncation(self, max_length: int) -> None: ...

    def enable_padding(self, **kwargs: Any) -> None: ...

    def token_to_id(self, token: str) -> int | None: ...


SessionFactory: TypeAlias = Callable[[], SessionLike]
TokenizerFactory: TypeAlias = Callable[[], TokenizerLike]

REQUIRED_INPUTS: Final = ("input_ids",)
BGE_M3_MAX_SEQ: Final = 8_192  # 与 bge-m3 config.json 的 max_position_embeddings 对齐


def _module(name: str, purpose: str) -> Any:
    """可选依赖的统一取法：缺包时抛 ModelUnavailableError，而不是 ImportError。"""
    try:
        return import_module(name)
    except ImportError as exc:  # pragma: no cover - 取决于装没装 extra
        raise ModelUnavailableError(
            f"缺少依赖 {name}（{purpose}）：安装 backend 的 [retrieval] extra，或改用降级腿",
            detail={"missing": name, "cause": str(exc)},
        ) from exc


def numpy() -> Any:
    """本包唯一接触 numpy 的入口：数组只在喂推理与池化时才需要。"""
    return _module("numpy", "张量构造与池化")


# --------------------------------------------------------------------------- 装载


def default_thread_budget() -> int:
    """单进程内 CPU 推理线程的封顶值。

    ORT 默认按逻辑核数起 intra-op 线程；高原边缘盒是与智能体进程共享的 vCPU，
    不封顶会让检索把协同链路的 CPU 抢光，换来的是检索自身也变慢。
    """
    cores = os.cpu_count() or 2
    return max(1, min(4, cores // 2 if cores >= 8 else 1))


def load_session(
    model_path: str | Path,
    *,
    intra_op_threads: int | None = None,
    inter_op_threads: int | None = None,
    providers: Sequence[str] | None = None,
) -> SessionLike:
    """装载 CPU int8 会话。

    provider 只列 CPU：int8 动态量化在 CUDA EP 上通常没有对应 kernel，
    而且一旦静默回退到 GPU 之外的设备，压测出来的时延数字就不属于这台机器了。
    """
    path = Path(model_path)
    if not path.is_file():
        raise ModelUnavailableError(f"ONNX 权重不存在：{path}", detail={"path": str(path)})
    ort = _module("onnxruntime", "ONNX 推理会话")
    options = ort.SessionOptions()
    # ORT 的 Python 面是 *_num_threads（不是 *_thread_count）：名字写错会在第一次装载时
    # AttributeError，被降级逻辑吞成"模型不可用"——所以这条路径要用真 SessionOptions 兜一遍。
    options.intra_op_num_threads = intra_op_threads if intra_op_threads is not None else default_thread_budget()
    options.inter_op_num_threads = inter_op_threads if inter_op_threads is not None else 1
    options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    try:
        return ort.InferenceSession(str(path), sess_options=options, providers=list(providers or ["CPUExecutionProvider"]))
    except Exception as exc:  # ORT 抛的是原生 RuntimeError，类型不稳定，按消息收口
        raise ModelUnavailableError(f"ONNX 会话装载失败：{path}", detail={"path": str(path), "cause": str(exc)[:512]}) from exc


def load_tokenizer(tokenizer_path: str | Path) -> TokenizerLike:
    """装载 tokenizer.json（单文件、免 vocab 拼装，因此不需要 transformers）。"""
    path = Path(tokenizer_path)
    if not path.is_file():
        raise ModelUnavailableError(f"分词器文件不存在：{path}", detail={"path": str(path)})
    tokenizers = _module("tokenizers", "BPE 分词")
    try:
        return tokenizers.Tokenizer.from_file(str(path))
    except Exception as exc:
        raise ModelUnavailableError(f"分词器装载失败：{path}", detail={"path": str(path), "cause": str(exc)[:512]}) from exc


def load_model_config(model_dir: str | Path, *, name: str = "config.json") -> dict[str, Any]:
    """读模型目录里的 config.json；缺文件返回空表（装载路径不因此失败）。"""
    path = Path(model_dir) / name
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ModelUnavailableError(f"模型配置不可读：{path}", detail={"path": str(path), "cause": str(exc)[:256]}) from exc
    return raw if isinstance(raw, dict) else {}


def pad_token_id_of(tokenizer: TokenizerLike, config: Mapping[str, Any] | None = None) -> int:
    """pad 位的 id：优先 config.json，其次 <pad>/[PAD] 字面，最后落到 0。

    动态量化导出的 batch 必须 padding，pad 位错一个就会让整批向量偏掉，
    所以这个查找顺序要写在一处并可测，而不是散在两个实现里。
    （bge-m3 的实际 pad token 是 `<pad>`=1，`[PAD]` 并不在词表里。）
    """
    value = (config or {}).get("pad_token_id")
    if isinstance(value, int):
        return value
    for token in ("<pad>", "[PAD]", "[pad]"):
        found = tokenizer.token_to_id(token)
        if found is not None:
            return int(found)
    return 0


# --------------------------------------------------------------------------- 编码与喂入


@dataclass(frozen=True, slots=True)
class TokenizedBatch:
    """与模型无关的编码结果：三条等长 id 序列 + 声明到的输入名。"""

    input_ids: tuple[list[int], ...]
    attention_mask: tuple[list[int], ...]
    token_type_ids: tuple[list[int], ...]

    @property
    def size(self) -> int:
        return len(self.input_ids)

    def column(self, name: str) -> tuple[list[int], ...]:
        return {"input_ids": self.input_ids, "attention_mask": self.attention_mask, "token_type_ids": self.token_type_ids}[name]


def session_input_names(session: SessionLike) -> tuple[str, ...]:
    return tuple(str(node.name) for node in session.get_inputs())


def encode(
    tokenizer: TokenizerLike,
    items: Sequence[str] | Sequence[tuple[str, str]],
    *,
    max_length: int,
    pad_token_id: int | None = None,
) -> TokenizedBatch:
    """批量编码；items 元素是 str（嵌入腿）或 (query, passage) 元组（交叉编码腿）。

    tokenizers 的 encode_batch 两种都吃，因此两条腿共用这一个函数——
    截断/padding 口径必须一致，否则"同一句话在两条腿里长度不同"会很难查。
    """
    if not items:
        return TokenizedBatch((), (), ())
    if max_length < 2:
        raise RetrievalArgumentError("max_length 至少为 2（要留 cls/sep）", detail={"max_length": max_length})
    tokenizer.enable_truncation(max_length)
    # 只给 pad_id：tokenizers 的 enable_padding() 无参时默认 pad_id=0，而 0 在 XLM-R 里是 <s>，
    # 用它 padding 会让每个批次的最后一个真实 token 之后都多出一个"句首"信号。
    # pad_token 字面也不传，避免张量里的 id 与元数据字符串不一致（排障时会误导）。
    if pad_token_id is None:
        tokenizer.enable_padding()
    else:
        tokenizer.enable_padding(pad_id=pad_token_id)
    encodings = tokenizer.encode_batch(list(items))
    ids = [list(enc.ids) for enc in encodings]
    mask = [list(enc.attention_mask) for enc in encodings]
    types = [list(getattr(enc, "type_ids", None) or [0] * len(enc.ids)) for enc in encodings]
    return TokenizedBatch(tuple(ids), tuple(mask), tuple(types))


def build_feeds(
    batch: TokenizedBatch,
    *,
    input_names: Iterable[str],
    dtype: str = "int64",
) -> dict[str, Any]:
    """只喂会话声明要的输入。

    optimum 的 XLM-RoBERTa 导出对 token_type_ids 给不给不一致：多喂一个输入名，
    ORT 直接报 InvalidInputName；少喂则模型自己补零。按 get_inputs() 过滤是唯一稳的做法。
    """
    np = numpy()
    wanted = [name for name in input_names if name in {"input_ids", "attention_mask", "token_type_ids"}]
    missing = set(REQUIRED_INPUTS) - set(wanted)
    if missing:
        raise EmbeddingShapeError("会话缺少必需输入", detail={"missing": sorted(missing), "inputs": list(input_names)})
    return {name: np.array(batch.column(name), dtype=dtype) for name in wanted}


# --------------------------------------------------------------------------- 池化与归一化


def as_rows(output: Any) -> tuple[Any, int, int]:
    """把模型张量按秩拆开：3 秩 [batch, seq, hidden]，2 秩 [batch, hidden]（已池化的导出）。"""
    np = numpy()
    array = output if isinstance(output, np.ndarray) else np.asarray(output)
    if array.ndim == 3:
        return array, int(array.shape[1]), int(array.shape[2])
    if array.ndim == 2:
        return array, 1, int(array.shape[1])
    raise EmbeddingShapeError(
        "模型输出秩不为 2 或 3",
        detail={"ndim": int(array.ndim), "shape": list(array.shape)},
    )


def pick_embedding_output(outputs: Sequence[Any], output_names: Sequence[str]) -> Any:
    """在多个输出里挑嵌入：优先 sentence_embedding（optimum 的池化输出），否则 last_hidden_state。"""
    named = dict(zip(output_names, outputs, strict=False))
    for key in ("sentence_embedding", "last_hidden_state", "token_embedding", "embedding"):
        if key in named:
            return named[key]
    if not outputs:
        raise EmbeddingShapeError("会话没有任何输出张量")
    return outputs[0]


def pool_and_normalize(
    model_output: Any,
    *,
    attention_mask: Sequence[Sequence[int]] | None,
    pool: str = "cls",
    dim: int,
) -> list[list[float]]:
    """池化 + L2 归一化，输出 list[list[float]]（persistence.rows 的向量契约就是它）。

    默认 cls：BAAI 对 bge 系列给的口径是 CLS + normalize，用 mean 会把指令式短文本拉近，
    在 5 灾种的小标签空间里表现为"什么都像泥石流"。
    """
    np = numpy()
    array, _seq_len, hidden = as_rows(model_output)
    if hidden != dim:
        raise EmbeddingShapeError("嵌入维度与列定义不符", detail={"expected": dim, "actual": hidden})
    if array.ndim == 2:
        # 已池化的导出（sentence_embedding）：直接就是 [batch, hidden]
        rows = array
    elif pool == "mean":
        rows = mean_pool(array, np.asarray(attention_mask or [], dtype=np.float32))
    elif pool == "cls":
        rows = array[:, 0, :]
    else:
        raise RetrievalArgumentError(f"未知池化方式：{pool}", detail={"pool": pool, "allowed": ["cls", "mean"]})
    dense = rows.astype(np.float32)
    if dense.ndim != 2:
        raise EmbeddingShapeError("池化后不是 [batch, hidden] 二维", detail={"shape": list(dense.shape)})
    norms = np.linalg.norm(dense, axis=-1, keepdims=True)
    norms = np.where(norms == 0, 1.0, norms)
    return [[float(value) for value in row] for row in (dense / norms)]


def mean_pool(tokens: Any, mask: Any) -> Any:
    """attention-mask 加权均值；mask 全 0 时分母兜到 1e-9，避免整行 NaN。"""
    np = numpy()
    if mask.ndim != 2 or mask.shape != tokens.shape[:2]:
        raise EmbeddingShapeError(
            "attention_mask 与 hidden 状态形状不符",
            detail={"mask": list(mask.shape), "tokens": list(tokens.shape)},
        )
    expanded = mask.astype(tokens.dtype)[:, :, None]
    summed = (tokens * expanded).sum(axis=1)
    counts = np.clip(mask.sum(axis=1, keepdims=True).astype(tokens.dtype), 1e-9, None)
    return summed / counts


def cls_pool(tokens: Any) -> Any:
    return tokens[:, 0, :]


def l2_normalize(vector: Sequence[float]) -> list[float]:
    """纯 Python 版 L2 归一化：降级腿与测试替身用它，不牵 numpy。"""
    total = sum(value * value for value in vector)
    if total <= 0.0:
        return [0.0] * len(vector)
    root = total**0.5
    return [float(value) / root for value in vector]


def sigmoid(value: float) -> float:
    """数值稳定版 sigmoid：reranker 的分要落进 [0,1] 才好与阈值比较。"""
    if value >= 0:
        return 1.0 / (1.0 + math.exp(-value))
    pivot = math.exp(value)
    return pivot / (1.0 + pivot)


def describe_session(session: SessionLike) -> dict[str, object]:
    """会话自述（输入名/类型）：排障与"这台机器上的模型到底长什么样"的取证入口。"""
    info: list[dict[str, object]] = []
    for node in session.get_inputs():
        info.append({"name": str(node.name), "type": str(getattr(node, "type", "")), "shape": [str(s) for s in getattr(node, "shape", [])]})
    return {"inputs": info}


__all__ = [
    "BGE_M3_MAX_SEQ",
    "REQUIRED_INPUTS",
    "EmbeddingShapeError",
    "ModelUnavailableError",
    "RerankShapeError",
    "RetrievalArgumentError",
    "RetrievalError",
    "SessionFactory",
    "SessionLike",
    "TokenizedBatch",
    "TokenizerFactory",
    "TokenizerLike",
    "as_rows",
    "build_feeds",
    "cls_pool",
    "default_thread_budget",
    "describe_session",
    "encode",
    "l2_normalize",
    "load_model_config",
    "load_session",
    "load_tokenizer",
    "mean_pool",
    "numpy",
    "pad_token_id_of",
    "pick_embedding_output",
    "pool_and_normalize",
    "session_input_names",
    "sigmoid",
]
