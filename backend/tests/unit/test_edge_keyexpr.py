"""subject ↔ key expression 映射的单测：全部纯逻辑，不需要 zenoh 运行时。

字符规则取自 eclipse-zenoh 1.10.1 的实测边界（live 测试再做一次库侧交叉校验）：
'#'、'?'、'$' 非法，空块与首尾分隔符非法，'*' 单块通配、'**' 跨块通配。
"""

from __future__ import annotations

from typing import ClassVar

import pytest

from aegis.bus import subjects
from aegis.domain.enums import AgentType
from aegis.edge.errors import KeyExprMappingError
from aegis.edge.keyexpr import (
    ILLEGAL_CHUNK_CHARS,
    SAFE_CHUNK_CHARS,
    chunk_needs_encoding,
    data_keyexpr,
    decode_chunk,
    encode_chunk,
    is_pattern,
    keyexpr_to_pattern,
    keyexpr_to_subject,
    pattern_to_keyexpr,
    subject_to_keyexpr,
)

CORPUS = [
    subjects.agent_in(AgentType.PERCEIVE),
    subjects.agent_out(AgentType.ASSESS),
    subjects.agent_hb(AgentType.PLAN),
    subjects.data("station01", "rain_10min"),
    subjects.data("rg_02", "debris_level"),
    subjects.workflow("wfi_deadbeefdeadbeef", "started"),
    subjects.alert(3, "540121"),
    subjects.ops("gateway01", "link_down"),
    subjects.reply("gateway"),
]

PATTERNS = [
    "data.>",
    "agent.*.in",
    "agent.*.out",
    "platform.alert.>",
    "ops.>",
    "reply.>",
    "workflow.wfi_1.>",
]


class TestForwardMapping:
    @pytest.mark.parametrize("subject", CORPUS)
    def test_separator_translation(self, subject: str) -> None:
        keyexpr = subject_to_keyexpr(subject)
        assert "." not in keyexpr
        assert keyexpr == subject.replace(".", "/")

    @pytest.mark.parametrize("subject", CORPUS)
    def test_round_trip_is_identity(self, subject: str) -> None:
        assert keyexpr_to_subject(subject_to_keyexpr(subject)) == subject

    def test_concrete_example(self) -> None:
        assert subject_to_keyexpr("data.station01.rain_10min") == "data/station01/rain_10min"

    def test_all_valid_subjects_map_without_error(self) -> None:
        generated = [f"{a}.{b}.{c}" for a in "abc" for b in "def" for c in "ghi"]
        for subject in generated:
            assert subjects.is_valid_subject(subject)
            assert subject_to_keyexpr(subject) == subject.replace(".", "/")

    def test_mapping_is_injective(self) -> None:
        mapped = {subject: subject_to_keyexpr(subject) for subject in CORPUS}
        assert len(set(mapped.values())) == len(mapped)
        other = {subject: subject_to_keyexpr(subject) for subject in generated_big()}
        assert len(set(other.values())) == len(other)

    def test_underscore_and_dash_survive(self) -> None:
        assert subject_to_keyexpr("ops.gw_01.link-down") == "ops/gw_01/link-down"


def generated_big() -> list[str]:
    tokens = ["a", "bb", "c_1", "d-2", "333"]
    return [".".join((t1, t2, t3)) for t1 in tokens for t2 in tokens for t3 in tokens]


class TestIllegalCharacters:
    @pytest.mark.parametrize("char", sorted(ILLEGAL_CHUNK_CHARS))
    def test_every_documented_illegal_char_is_rejected(self, char: str) -> None:
        with pytest.raises(KeyExprMappingError) as excinfo:
            subject_to_keyexpr(f"data.station01.rain{char}")
        assert char in str(excinfo.value.detail["illegal"])

    @pytest.mark.parametrize(
        "value",
        ["", ".", "a.", ".a", "a..b", "a...", "data..station", "/a/b", "a/b"],
    )
    def test_empty_and_boundary_shapes_rejected(self, value: str) -> None:
        with pytest.raises(KeyExprMappingError):
            subject_to_keyexpr(value)

    def test_illegal_set_contains_nothing_we_need(self) -> None:
        """aegis subject 字母表与 zenoh 非法集必须完全不相交，否则映射面不可用。"""
        alphabet = set(subjects.TOKEN_RE_CHARS)
        assert alphabet & ILLEGAL_CHUNK_CHARS == set()
        assert "." not in ILLEGAL_CHUNK_CHARS

    def test_wildcard_in_concrete_subject_rejected(self) -> None:
        with pytest.raises(KeyExprMappingError):
            subject_to_keyexpr("agent.*.in")
        with pytest.raises(KeyExprMappingError):
            subject_to_keyexpr("data.>")

    def test_mixed_star_chunk_rejected(self) -> None:
        with pytest.raises(KeyExprMappingError):
            subject_to_keyexpr("data.sta*ion.rain")

    def test_typed_error_carries_schema_code(self) -> None:
        with pytest.raises(KeyExprMappingError) as excinfo:
            subject_to_keyexpr("a#b.c")
        payload = excinfo.value.to_payload()
        assert payload["code"] == "E_SCHEMA_INVALID"
        assert payload["detail"]["value"] == "a#b.c"


class TestReverseMapping:
    def test_dot_in_chunk_is_not_reversible(self) -> None:
        """'a/b.c' 与 'a.b/c' 都会回映成 'a.b.c'：库允许、我们拒绝。"""
        assert keyexpr_to_subject("a/b") == "a.b"
        with pytest.raises(KeyExprMappingError):
            keyexpr_to_subject("a/b.c")

    @pytest.mark.parametrize("keyexpr", ["a/b", "data/station01/rain_10min", "platform/alert/3/540121"])
    def test_round_trip(self, keyexpr: str) -> None:
        assert subject_to_keyexpr(keyexpr_to_subject(keyexpr)) == keyexpr

    def test_globs_rejected_for_concrete_reverse(self) -> None:
        with pytest.raises(KeyExprMappingError):
            keyexpr_to_subject("a/*/c")
        with pytest.raises(KeyExprMappingError):
            keyexpr_to_subject("a/**")


class TestWildcardTranslation:
    @pytest.mark.parametrize(
        ("pattern", "keyexpr"),
        [
            ("data.>", "data/**"),
            ("agent.*.in", "agent/*/in"),
            ("platform.alert.>", "platform/alert/**"),
            ("reply.gateway.inbox", "reply/gateway/inbox"),
            ("data.*.>", "data/*/**"),
        ],
    )
    def test_forward(self, pattern: str, keyexpr: str) -> None:
        assert pattern_to_keyexpr(pattern) == keyexpr

    @pytest.mark.parametrize("pattern", PATTERNS)
    def test_pattern_round_trip(self, pattern: str) -> None:
        assert keyexpr_to_pattern(pattern_to_keyexpr(pattern)) == pattern

    def test_multi_wildcard_must_be_last(self) -> None:
        with pytest.raises(KeyExprMappingError):
            pattern_to_keyexpr("data.>.rain")

    def test_mid_double_star_has_no_nats_equivalent(self) -> None:
        with pytest.raises(KeyExprMappingError):
            keyexpr_to_pattern("data/**/rain")

    def test_single_star_matches_one_chunk_only(self) -> None:
        """语义等价性前提：NATS '*' 与 zenoh '*' 都只吃一层。"""
        assert subjects.subject_matches("agent.*.in", "agent.perceive.in")
        assert not subjects.subject_matches("agent.*.in", "agent.perceive.deep.in")

    def test_wildcard_sets_disjoint_from_concrete_alphabet(self) -> None:
        assert "*" not in SAFE_CHUNK_CHARS
        assert ">" not in SAFE_CHUNK_CHARS


class TestChunkEscaping:
    NASTY: ClassVar[list[str]] = [
        "*",
        "**",
        ">",
        "#",
        "?",
        "$*$",
        ".",
        "/",
        "_",
        "__",
        "_2f",
        "a b",
        "a\tb",
        "a\nb",
        "泥石流",
        "🛰️",
        "rain_10min",
        "540121",
        "a/b?c#d$e",
        "%20",
        "a&b=c",
        "-x-",
        "UPPER_case-1",
    ]

    @pytest.mark.parametrize("text", NASTY)
    def test_round_trip(self, text: str) -> None:
        encoded = encode_chunk(text)
        assert decode_chunk(encoded) == text

    @pytest.mark.parametrize("text", NASTY)
    def test_output_is_legal_keyexpr_chunk(self, text: str) -> None:
        encoded = encode_chunk(text)
        assert encoded
        assert not set(encoded) & set(ILLEGAL_CHUNK_CHARS)
        assert "/" not in encoded
        assert encoded not in {"*", "**"}
        assert subject_to_keyexpr(f"data.{encoded}.rx") == f"data/{encoded}/rx"

    @pytest.mark.parametrize("text", NASTY)
    def test_injective(self, text: str) -> None:
        assert decode_chunk(encode_chunk(text)) == text

    def test_no_collisions_within_corpus(self) -> None:
        encoded = {text: encode_chunk(text) for text in self.NASTY}
        assert len(set(encoded.values())) == len(encoded)

    def test_star_cannot_become_wildcard_by_accident(self) -> None:
        assert encode_chunk("*") != "*"
        assert "*" not in encode_chunk("a*b")

    def test_empty_values_rejected(self) -> None:
        with pytest.raises(KeyExprMappingError):
            encode_chunk("")
        with pytest.raises(KeyExprMappingError):
            decode_chunk("")
        with pytest.raises(KeyExprMappingError):
            decode_chunk("*")

    @pytest.mark.parametrize("chunk", ["_", "a_", "_z", "_4", "_ff", "x_2"])
    def test_malformed_escape_sequences(self, chunk: str) -> None:
        with pytest.raises(KeyExprMappingError):
            decode_chunk(chunk)

    def test_half_byte_escape_is_not_silently_accepted(self) -> None:
        """'_e4' 是 3 字节 UTF-8 序列的残段：必须显式失败而不是产出乱码。"""
        with pytest.raises(KeyExprMappingError):
            decode_chunk("_e4")

    def test_overlong_value_rejected(self) -> None:
        with pytest.raises(KeyExprMappingError):
            encode_chunk("x" * 300)

    def test_needs_encoding(self) -> None:
        assert not chunk_needs_encoding("rain10min")
        assert chunk_needs_encoding("rain_10min")
        assert chunk_needs_encoding("a.b")
        assert chunk_needs_encoding("")

    def test_data_keyexpr_helper(self) -> None:
        assert data_keyexpr("station01", "rain_10min") == "data/station01/rain__10min"
        assert keyexpr_to_subject(data_keyexpr("station01", "rain_10min")) == "data.station01.rain__10min"


class TestIsPattern:
    @pytest.mark.parametrize("pattern", PATTERNS)
    def test_patterns(self, pattern: str) -> None:
        assert is_pattern(pattern)

    @pytest.mark.parametrize("subject", CORPUS)
    def test_concrete(self, subject: str) -> None:
        assert not is_pattern(subject)
