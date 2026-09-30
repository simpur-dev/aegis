"""嵌入腿契约：OnnxEmbedder 在假会话/假分词器下走完真实代码路径，HashingEmbedder 保持诚实的降级语义。

不加载任何权重：会话与分词器是注入的工厂，因此这里覆盖的是
"编码 -> 喂入 -> 取输出 -> 池化 -> 归一化 -> 1024 维 list[float]" 这条真链路。
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest

from aegis.persistence.rows import EMBEDDING_DIM
from aegis.retrieval.embedder import Embedder, HashingEmbedder, OnnxEmbedder, embed_one
from aegis.retrieval.onnx_io import EmbeddingShapeError, ModelUnavailableError, RetrievalArgumentError


class Node:
    def __init__(self, name: str) -> None:
        self.name = name
        self.shape = ["batch", "seq"]
        self.type = "tensor(int64)"


class FakeTokenizer:
    """按字符给 id，并把整批 pad 到批内最长——真实 tokenizers 的 padding 口径。"""

    def __init__(self) -> None:
        self.truncation_at: int | None = None
        self.pad_id: int | None = None
        self.encode_calls = 0

    def enable_truncation(self, max_length: int) -> None:
        self.truncation_at = max_length

    def enable_padding(self, **kwargs: Any) -> None:
        self.pad_id = kwargs.get("pad_id", 0)

    def token_to_id(self, token: str) -> int | None:
        return 1 if token == "[PAD]" else None

    def encode_batch(self, inputs: Any) -> list[Any]:
        self.encode_calls += 1
        texts = [" ".join(item) if isinstance(item, tuple) else str(item) for item in inputs]
        longest = max(len(text) for text in texts)
        encodings = []
        for text in texts:
            ids = [ord(ch) for ch in text]
            pad = longest - len(ids)
            encodings.append(
                _Encoding(
                    ids=ids + [self.pad_id or 0] * pad,
                    mask=[1] * len(ids) + [0] * pad,
                )
            )
        return encodings


class _Encoding:
    def __init__(self, *, ids: list[int], mask: list[int]) -> None:
        self.ids = ids
        self.attention_mask = mask
        self.type_ids = [0] * len(ids)


class FakeSession:
    """输出 hidden[b, t, h] = 1 当且仅当 h == t：CLS 是 one-hot(0)，mean 是摊开的。"""

    def __init__(
        self, *, hidden: int = 8, input_names: tuple[str, ...] = ("input_ids", "attention_mask"), output_name: str = "last_hidden_state"
    ) -> None:
        self.hidden = hidden
        self.input_names = input_names
        self.output_name = output_name
        self.feeds: list[dict[str, Any]] = []

    def get_inputs(self) -> list[Node]:
        return [Node(name) for name in self.input_names]

    def get_outputs(self) -> list[Node]:
        return [Node(self.output_name)]

    def run(self, output_names: Any, input_feed: Any, run_options: Any = None) -> list[Any]:
        self.feeds.append(dict(input_feed))
        batch, seq = input_feed["input_ids"].shape
        seq = min(seq, self.hidden)
        tensor = np.zeros((batch, seq, self.hidden), dtype=np.float32)
        for position in range(seq):
            tensor[:, position, position] = 1.0
        return [tensor]


def build_embedder(session: FakeSession, *, dim: int = 8, **kwargs: Any) -> tuple[OnnxEmbedder, FakeTokenizer]:
    tokenizer = FakeTokenizer()
    embedder = OnnxEmbedder(
        session_factory=lambda: session,
        tokenizer_factory=lambda: tokenizer,
        dim=dim,
        **kwargs,
    )
    return embedder, tokenizer


class TestProtocolConformance:
    def test_both_implementations_satisfy_the_embedder_protocol(self) -> None:
        embedder, _ = build_embedder(FakeSession())
        assert isinstance(embedder, Embedder)
        assert isinstance(HashingEmbedder(), Embedder)
        assert embedder.dim == 8
        assert HashingEmbedder().dim == EMBEDDING_DIM == 1024

    def test_a_vector_source_without_dim_is_not_an_embedder(self) -> None:
        class NotAnEmbedder:
            model_id = "nope"

            def embed(self, texts: Any) -> list[list[float]]:
                return []

        assert not isinstance(NotAnEmbedder(), Embedder)


class TestHashingEmbedder:
    def test_dim_and_unit_norm(self) -> None:
        vector = HashingEmbedder().embed(["林周县短时强降水触发泥石流"])[0]
        assert len(vector) == 1024
        assert sum(value * value for value in vector) == pytest.approx(1.0)

    def test_deterministic_across_instances(self) -> None:
        text = "滑坡体后缘裂缝 12mm，持续扩张"
        assert HashingEmbedder().embed([text])[0] == HashingEmbedder().embed([text])[0]

    def test_same_words_same_vector(self) -> None:
        left = HashingEmbedder().embed(["泥石流 预警 疏散"])[0]
        right = HashingEmbedder().embed(["疏散 预警 泥石流"])[0]
        assert left == right

    def test_different_text_is_not_identical(self) -> None:
        left = HashingEmbedder().embed(["泥石流预警"])[0]
        right = HashingEmbedder().embed(["雪崩封闭通道"])[0]
        assert left != right

    def test_overlapping_text_scores_closer_than_disjoint(self) -> None:
        embedder = HashingEmbedder()
        a, b, c = embedder.embed(["坡体裂缝扩张需要撤离", "坡体裂缝持续扩张", "雪崩通道封闭管制"])
        near = sum(x * y for x, y in zip(a, b, strict=True))
        far = sum(x * y for x, y in zip(a, c, strict=True))
        assert near > far

    def test_empty_text_yields_zero_vector_not_nan(self) -> None:
        vector = HashingEmbedder().embed([""])[0]
        assert len(vector) == 1024
        assert all(value == 0.0 for value in vector)

    def test_rejects_non_positive_dim(self) -> None:
        with pytest.raises(RetrievalArgumentError, match="dim"):
            HashingEmbedder(dim=0)


class TestOnnxEmbedder:
    def test_session_and_tokenizer_are_loaded_lazily_and_once(self) -> None:
        session = FakeSession()
        calls = {"session": 0, "tokenizer": 0}

        def make_session() -> FakeSession:
            calls["session"] += 1
            return session

        embedder, _ = build_embedder(session)
        embedder._session_factory = make_session  # type: ignore[method-assign]
        assert calls["session"] == 0, "构造期不得装载会话"
        embedder.embed(["a"])
        embedder.embed(["b"])
        assert calls["session"] == 1

    def test_feed_contains_only_declared_inputs(self) -> None:
        session = FakeSession(input_names=("input_ids", "attention_mask"))
        embedder, _ = build_embedder(session)
        embedder.embed(["泥石"])
        assert set(session.feeds[0]) == {"input_ids", "attention_mask"}

    def test_token_type_ids_are_fed_when_the_model_declares_them(self) -> None:
        session = FakeSession(input_names=("input_ids", "attention_mask", "token_type_ids"))
        embedder, _ = build_embedder(session)
        embedder.embed(["泥石"])
        assert set(session.feeds[0]) == {"input_ids", "attention_mask", "token_type_ids"}

    def test_cls_pooling_is_the_default(self) -> None:
        embedder, _ = build_embedder(FakeSession())
        vector = embedder.embed(["abcd"])[0]
        assert vector == pytest.approx([1.0] + [0.0] * 7)

    def test_mean_pooling_spreads_over_real_tokens_only(self) -> None:
        session = FakeSession(hidden=8)
        embedder, _ = build_embedder(session, pool="mean")
        # "ab" 两个真实 token，padded 到位宽 2（批内只有一条）：mean = (e0+e1)/2 -> 归一后各 1/sqrt(2)
        vector = embedder.embed(["ab"])[0]
        assert vector[0] == pytest.approx(1 / (2**0.5))
        assert vector[1] == pytest.approx(1 / (2**0.5))
        assert vector[2:] == pytest.approx([0.0] * 6)

    def test_batch_size_chunks_the_runs(self) -> None:
        session = FakeSession()
        embedder, _ = build_embedder(session, batch_size=2)
        vectors = embedder.embed(["ab", "cd", "ef"])
        assert len(vectors) == 3
        assert [feed["input_ids"].shape[0] for feed in session.feeds] == [2, 1]

    def test_pure_whitespace_does_not_break_the_batch(self) -> None:
        session = FakeSession()
        embedder, _ = build_embedder(session)
        vectors = embedder.embed(["", "  "])
        assert len(vectors) == 2
        assert all(len(vector) == 8 for vector in vectors)

    def test_empty_input_short_circuits_without_inference(self) -> None:
        session = FakeSession()
        embedder, _ = build_embedder(session)
        assert embedder.embed([]) == []
        assert session.feeds == []

    def test_dimension_mismatch_surfaces_as_shape_error(self) -> None:
        # 会话给 8 维，列定义 1024 维：必须在本地就拦住，而不是把坏向量送到 pgvector
        embedder, _ = build_embedder(FakeSession(hidden=8), dim=EMBEDDING_DIM)
        with pytest.raises(EmbeddingShapeError, match="维度") as excinfo:
            embedder.embed(["泥石"])
        assert excinfo.value.detail == {"expected": EMBEDDING_DIM, "actual": 8}

    def test_output_count_mismatch_is_reported(self, monkeypatch: pytest.MonkeyPatch) -> None:
        session = FakeSession()
        embedder, _ = build_embedder(session)
        monkeypatch.setattr(embedder, "_embed_chunk", lambda *args, **kwargs: [[0.0] * 8])
        with pytest.raises(EmbeddingShapeError, match="条数"):
            embedder.embed(["a", "b"])

    def test_constructor_validates_knobs(self) -> None:
        with pytest.raises(RetrievalArgumentError):
            OnnxEmbedder(session_factory=lambda: FakeSession(), tokenizer_factory=FakeTokenizer, batch_size=0)
        with pytest.raises(RetrievalArgumentError):
            OnnxEmbedder(session_factory=lambda: FakeSession(), tokenizer_factory=FakeTokenizer, dim=-1)

    def test_from_dir_without_weights_fails_with_a_clear_message(self, tmp_path: Any) -> None:
        embedder = OnnxEmbedder.from_dir(tmp_path / "missing")
        with pytest.raises(ModelUnavailableError, match="不存在"):
            embedder.embed(["泥石"])


class TestEmbedOne:
    def test_returns_single_vector_of_declared_dim(self) -> None:
        vector = embed_one(HashingEmbedder(), "危岩崩塌")
        assert len(vector) == 1024

    def test_lying_embedder_is_caught(self) -> None:
        class Lying:
            model_id = "lying"
            dim = 1024

            def embed(self, texts: Any) -> list[list[float]]:
                return [[0.1] * 4 for _ in texts]

        with pytest.raises(EmbeddingShapeError, match="维度"):
            embed_one(Lying(), "x")

    def test_empty_batch_from_embedder_is_caught(self) -> None:
        class Silent:
            model_id = "silent"
            dim = 1024

            def embed(self, texts: Any) -> list[list[float]]:
                return []

        with pytest.raises(EmbeddingShapeError, match="空批次"):
            embed_one(Silent(), "x")
