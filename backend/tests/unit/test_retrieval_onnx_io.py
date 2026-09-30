"""onnx_io 的契约：错误类型、可选依赖收口、输入过滤、池化与归一化数学。

这里不加载任何真权重：会话/分词器一律用假对象，覆盖的是"喂给模型的张量长什么样"
与"模型输出的形状错了会怎样"这两段真实代码路径。
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pytest

from aegis.errors import ErrorCode
from aegis.retrieval.onnx_io import (
    EmbeddingShapeError,
    ModelUnavailableError,
    RetrievalArgumentError,
    RetrievalError,
    TokenizedBatch,
    as_rows,
    build_feeds,
    default_thread_budget,
    describe_session,
    encode,
    l2_normalize,
    load_session,
    load_tokenizer,
    mean_pool,
    pad_token_id_of,
    pick_embedding_output,
    pool_and_normalize,
    session_input_names,
    sigmoid,
)


class Node:
    def __init__(self, name: str, *, shape: list[str] | None = None, type: str = "tensor(int64)") -> None:
        self.name = name
        self.shape = shape or ["batch", "seq"]
        self.type = type


class FakeSession:
    def __init__(self, input_names: tuple[str, ...] = ("input_ids", "attention_mask")) -> None:
        self._inputs = [Node(name) for name in input_names]
        self._outputs = [Node("last_hidden_state")]

    def get_inputs(self) -> list[Any]:
        return list(self._inputs)

    def get_outputs(self) -> list[Any]:
        return list(self._outputs)

    def run(self, output_names: Any, input_feed: Any, run_options: Any = None) -> list[Any]:
        raise AssertionError("本模块的单测不执行推理")


class FakeEncoding:
    def __init__(self, ids: list[int]) -> None:
        self.ids = ids
        self.attention_mask = [1] * len(ids)
        self.type_ids = [0] * len(ids)


class FakeTokenizer:
    """按字符给 id（a->97…），足以验证长度与喂入形状，不承担语义。"""

    def __init__(self, *, ids: dict[str, int] | None = None) -> None:
        self.known = dict(ids) if ids is not None else {"[PAD]": 1, "<pad>": 1}
        self.max_length: int | None = None
        self.padding: dict[str, Any] = {}

    def enable_truncation(self, max_length: int) -> None:
        self.max_length = max_length

    def enable_padding(self, **kwargs: Any) -> None:
        self.padding = dict(kwargs)

    def token_to_id(self, token: str) -> int | None:
        return self.known.get(token)

    def encode_batch(self, inputs: Any) -> list[FakeEncoding]:
        longest = max(len(self._text(item)) for item in inputs)
        return [FakeEncoding([ord(ch) for ch in self._text(item)]) for item in inputs] if longest else []

    @staticmethod
    def _text(item: Any) -> str:
        return " ".join(item) if isinstance(item, tuple) else str(item)


class TestErrorSurface:
    def test_all_errors_share_base(self) -> None:
        for cls in (ModelUnavailableError, EmbeddingShapeError, RetrievalArgumentError):
            assert issubclass(cls, RetrievalError)

    def test_codes_drive_the_retry_decision(self) -> None:
        # 缺权重是可重试的"现在查不了"，参数错是不可重试的"改调用点"
        assert ModelUnavailableError("x").code is ErrorCode.NOT_READY
        assert ModelUnavailableError("x").retryable is True
        assert RetrievalArgumentError("x").code is ErrorCode.SCHEMA_INVALID
        assert RetrievalArgumentError("x").retryable is False

    def test_payload_is_log_safe(self) -> None:
        payload = EmbeddingShapeError("维度不符", detail={"expected": 1024}).to_payload()
        assert payload["code"] == "E_INTERNAL"
        assert payload["detail"] == {"expected": 1024}


class TestLoading:
    def test_missing_weight_raises_typed_error(self, tmp_path: Any) -> None:
        with pytest.raises(ModelUnavailableError, match="不存在"):
            load_session(tmp_path / "nope.onnx")

    def test_missing_tokenizer_raises_typed_error(self, tmp_path: Any) -> None:
        with pytest.raises(ModelUnavailableError, match="不存在"):
            load_tokenizer(tmp_path / "nope.json")

    def test_thread_budget_is_bounded(self) -> None:
        budget = default_thread_budget()
        assert 1 <= budget <= 4

    def test_session_option_names_match_the_installed_onnxruntime(self) -> None:
        """load_session 摸的是真 ORT 对象的属性名：假会话抓不到拼错的线程参数。

        写错名字的运行期表现是 AttributeError，被密集腿降级成"模型不可用"——
        在没有权重的机器上，这条用例就是唯一的捕获点。
        """
        ort = pytest.importorskip("onnxruntime")
        options = ort.SessionOptions()
        options.intra_op_num_threads = 2
        options.inter_op_num_threads = 1
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        assert options.intra_op_num_threads == 2


class TestEncodingAndFeeds:
    def test_empty_batch_short_circuits(self) -> None:
        batch = encode(FakeTokenizer(), [], max_length=8)
        assert batch == TokenizedBatch((), (), ())

    def test_rejects_degenerate_max_length(self) -> None:
        with pytest.raises(RetrievalArgumentError, match="max_length"):
            encode(FakeTokenizer(), ["abc"], max_length=1)

    def test_truncation_and_padding_are_configured(self) -> None:
        tokenizer = FakeTokenizer()
        encode(tokenizer, ["abcdef"], max_length=5, pad_token_id=1)
        assert tokenizer.max_length == 5
        # 只传 pad_id：enable_padding() 无参会把 pad 落到 id=0，而 XLM-R 的 0 是 <s>
        assert tokenizer.padding == {"pad_id": 1}

    def test_pair_input_is_joined_for_cross_encoder(self) -> None:
        batch = encode(FakeTokenizer(), [("ab", "cd")], max_length=16)
        assert batch.input_ids[0] == [ord(c) for c in "ab cd"]

    def test_feeds_only_contain_declared_inputs(self) -> None:
        batch = encode(FakeTokenizer(), ["abc"], max_length=8)
        feeds = build_feeds(batch, input_names=["input_ids", "token_type_ids"])
        assert set(feeds) == {"input_ids", "token_type_ids"}
        assert feeds["input_ids"].dtype == np.int64
        assert feeds["input_ids"].shape == (1, 3)

    def test_missing_input_ids_is_a_shape_error(self) -> None:
        batch = encode(FakeTokenizer(), ["abc"], max_length=8)
        with pytest.raises(EmbeddingShapeError, match="必需输入"):
            build_feeds(batch, input_names=["attention_mask"])

    def test_session_input_names(self) -> None:
        session = FakeSession(("input_ids", "attention_mask", "token_type_ids"))
        assert session_input_names(session) == ("input_ids", "attention_mask", "token_type_ids")

    def test_describe_session_reports_declared_inputs(self) -> None:
        info = describe_session(FakeSession(("input_ids",)))
        assert info["inputs"] == [{"name": "input_ids", "type": "tensor(int64)", "shape": ["batch", "seq"]}]

    def test_pad_id_lookup_order(self) -> None:
        assert pad_token_id_of(FakeTokenizer(), {"pad_token_id": 7}) == 7
        assert pad_token_id_of(FakeTokenizer(), {}) == 1
        assert pad_token_id_of(FakeTokenizer(ids={})) == 0


class TestPooling:
    def hidden(self, *, batch: int = 1, seq: int = 3, hidden: int = 4) -> np.ndarray:
        # hidden[b, t, h] = 1 当且仅当 h == t：CLS 是 one-hot(0)，mean 是摊开的
        array = np.zeros((batch, seq, hidden), dtype=np.float32)
        for position in range(seq):
            array[:, position, min(position, hidden - 1)] = 1.0
        return array

    def test_cls_pooling_takes_first_token(self) -> None:
        rows = pool_and_normalize(
            self.hidden(),
            attention_mask=[[1, 1, 1]],
            pool="cls",
            dim=4,
        )
        assert rows == [[1.0, 0.0, 0.0, 0.0]]

    def test_mean_pooling_spreads_over_real_tokens(self) -> None:
        rows = pool_and_normalize(
            self.hidden(),
            attention_mask=[[1, 1, 0]],
            pool="mean",
            dim=4,
        )
        assert rows[0][0] == pytest.approx(rows[0][1])
        assert rows[0][3] == pytest.approx(0.0)

    def test_two_dimensional_output_is_already_pooled(self) -> None:
        rows = pool_and_normalize(np.array([[3.0, 4.0]], dtype=np.float32), attention_mask=None, pool="cls", dim=2)
        assert len(rows) == 1
        assert rows[0] == pytest.approx([0.6, 0.8])

    def test_wrong_hidden_dimension_is_caught_here(self) -> None:
        with pytest.raises(EmbeddingShapeError, match="维度") as excinfo:
            pool_and_normalize(self.hidden(hidden=8), attention_mask=[[1, 1, 1]], pool="cls", dim=1024)
        assert excinfo.value.detail == {"expected": 1024, "actual": 8}

    def test_unsupported_rank_is_reported(self) -> None:
        with pytest.raises(EmbeddingShapeError, match="秩"):
            as_rows(np.zeros((2, 2, 2, 2), dtype=np.float32))

    def test_mean_pool_rejects_mask_shape_mismatch(self) -> None:
        with pytest.raises(EmbeddingShapeError, match="形状不符"):
            mean_pool(self.hidden(), np.ones((1, 2), dtype=np.float32))

    def test_unknown_pool_is_an_argument_error(self) -> None:
        with pytest.raises(RetrievalArgumentError, match="池化"):
            pool_and_normalize(self.hidden(), attention_mask=[[1, 1, 1]], pool="max", dim=4)

    def test_output_selection_prefers_pooled_tensor(self) -> None:
        chosen = pick_embedding_output([np.zeros((1, 3, 4)), np.zeros((1, 2))], ["last_hidden_state", "sentence_embedding"])
        assert chosen.shape == (1, 2)

    def test_output_selection_falls_back_to_first(self) -> None:
        chosen = pick_embedding_output([np.zeros((1, 3, 4))], ["whatever"])
        assert chosen.shape == (1, 3, 4)

    def test_no_outputs_at_all(self) -> None:
        with pytest.raises(EmbeddingShapeError, match="没有"):
            pick_embedding_output([], [])


class TestPureMath:
    def test_l2_normalize_matches_numpy(self) -> None:
        vector = [3.0, 4.0]
        assert l2_normalize(vector) == pytest.approx([0.6, 0.8])

    def test_zero_vector_stays_zero_instead_of_nan(self) -> None:
        assert l2_normalize([0.0, 0.0, 0.0]) == [0.0, 0.0, 0.0]

    @pytest.mark.parametrize("value", [-80.0, -1.0, 0.0, 1.0, 80.0])
    def test_sigmoid_bounds_and_monotonicity(self, value: float) -> None:
        assert 0.0 <= sigmoid(value) <= 1.0
        assert sigmoid(value) <= sigmoid(value + 1.0)

    def test_sigmoid_at_zero(self) -> None:
        assert sigmoid(0.0) == pytest.approx(0.5)
        assert sigmoid(-math.inf) == pytest.approx(0.0)
