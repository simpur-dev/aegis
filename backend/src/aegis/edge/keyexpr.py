"""NATS subject ↔ Zenoh key expression 的可逆映射（纯逻辑：不导入 zenoh，单测无需运行时）。

字符规则为 eclipse-zenoh 1.10.1 实测结论（交叉校验见 tests/integration/test_zenoh_edge_live.py）：
key expression 以 '/' 分块，禁止字符 '#'、'?'、'$'，禁止空块与首尾分隔符；
'*' 匹配单个分块，'**' 匹配跨分块。映射只做分隔符替换 + 通配符翻译，
其余一切字符（含大小写、'_'、'-'、非 ASCII）由 encode_chunk 做转义，保证可逆。
"""

from __future__ import annotations

import string

from aegis.edge.errors import KeyExprMappingError

SUBJECT_SEPARATOR = "."
KEYEXPR_SEPARATOR = "/"

NATS_SINGLE_WILDCARD = "*"
NATS_MULTI_WILDCARD = ">"
ZENOH_SINGLE_WILDCARD = "*"
ZENOH_MULTI_WILDCARD = "**"

# 实测非法字符：'#'/'?' 直接被拒，'$' 仅允许出现在 '$*' 宏形式中，'/' 是分隔符。
ILLEGAL_CHUNK_CHARS = frozenset("#?$")
# 动态值转义后的安全字符集：'_' 为转义引导符，因此不在安全集内。
SAFE_CHUNK_CHARS = frozenset(string.ascii_letters + string.digits + "-")
ESCAPE_CHAR = "_"

_MAX_CHUNK_BYTES = 255


def _chunks(value: str, separator: str, kind: str) -> list[str]:
    if not value:
        raise KeyExprMappingError(f"空 {kind}", detail={"value": value, "kind": kind})
    if value.startswith(separator) or value.endswith(separator):
        raise KeyExprMappingError(
            f"{kind} 首尾不得为分隔符 {separator!r}",
            detail={"value": value, "kind": kind},
        )
    chunks = value.split(separator)
    if any(not chunk for chunk in chunks):
        raise KeyExprMappingError(f"{kind} 含空分块", detail={"value": value, "kind": kind})
    return chunks


def _reject_illegal(chunk: str, *, source: str, value: str, extra: frozenset[str] = frozenset()) -> None:
    bad = sorted(char for char in chunk if char in ILLEGAL_CHUNK_CHARS or char in extra)
    if bad:
        raise KeyExprMappingError(
            f"分块含 key expression 非法字符 {bad}",
            detail={"chunk": chunk, "source": source, "value": value, "illegal": "".join(bad)},
        )


def _translate(value: str, *, src_sep: str, dst_sep: str, wildcards: bool, source: str) -> str:
    chunks = _chunks(value, src_sep, source)
    out: list[str] = []
    for index, chunk in enumerate(chunks):
        is_last = index == len(chunks) - 1
        # subject 侧多禁一个 '/'：否则单个 subject 分块会塌成两个 key expression 分块，破坏层数对齐。
        _reject_illegal(chunk, source=source, value=value, extra=frozenset({KEYEXPR_SEPARATOR}))
        if chunk in {NATS_SINGLE_WILDCARD, NATS_MULTI_WILDCARD}:
            if not wildcards:
                raise KeyExprMappingError(
                    "具体值不得使用裸通配符，动态值请走 encode_chunk",
                    detail={"value": value, "chunk": chunk},
                )
            if chunk == NATS_MULTI_WILDCARD and not is_last:
                raise KeyExprMappingError(
                    "'>' 只允许作为末位分块",
                    detail={"value": value, "position": index},
                )
            out.append(ZENOH_MULTI_WILDCARD if chunk == NATS_MULTI_WILDCARD else ZENOH_SINGLE_WILDCARD)
        else:
            if "*" in chunk:
                raise KeyExprMappingError(
                    "'*' 只能整块使用，块内混用星号在 zenoh 中非法",
                    detail={"value": value, "chunk": chunk},
                )
            out.append(chunk)
    return dst_sep.join(out)


def _back_translate(value: str, *, src_sep: str, dst_sep: str, wildcards: bool, source: str) -> str:
    chunks = _chunks(value, src_sep, source)
    out: list[str] = []
    for index, chunk in enumerate(chunks):
        is_last = index == len(chunks) - 1
        _reject_illegal(chunk, source=source, value=value)
        if SUBJECT_SEPARATOR in chunk:
            # 反向不可逆：'a.b/c' 与 'a/b.c' 都会回映成 'a.b.c'，故直接拒绝含点分块。
            raise KeyExprMappingError(
                "key expression 分块含 subject 分隔符，映射不可逆",
                detail={"value": value, "chunk": chunk},
            )
        if chunk in {ZENOH_SINGLE_WILDCARD, ZENOH_MULTI_WILDCARD}:
            if not wildcards:
                raise KeyExprMappingError("具体值不得含 zenoh 通配块", detail={"value": value})
            if chunk == ZENOH_MULTI_WILDCARD and not is_last:
                raise KeyExprMappingError("'**' 非末位时无 NATS 等价物", detail={"value": value})
            out.append(NATS_MULTI_WILDCARD if chunk == ZENOH_MULTI_WILDCARD else NATS_SINGLE_WILDCARD)
        elif "*" in chunk:
            raise KeyExprMappingError(
                "星号必须整块出现，块内混用星号无法回映为 NATS 语义",
                detail={"value": value, "chunk": chunk},
            )
        else:
            out.append(chunk)
    return dst_sep.join(out)


def subject_to_keyexpr(subject: str) -> str:
    """具体 subject（无通配）→ key expression：'.' 换 '/'。"""
    return _translate(
        subject,
        src_sep=SUBJECT_SEPARATOR,
        dst_sep=KEYEXPR_SEPARATOR,
        wildcards=False,
        source="subject",
    )


def keyexpr_to_subject(keyexpr: str) -> str:
    """具体 key expression → subject：'/' 换 '.'，对合法 subject 双向可逆。"""
    return _back_translate(
        keyexpr,
        src_sep=KEYEXPR_SEPARATOR,
        dst_sep=SUBJECT_SEPARATOR,
        wildcards=False,
        source="key_expr",
    )


def pattern_to_keyexpr(pattern: str) -> str:
    """订阅侧 NATS 通配模式 → zenoh 选择子：'*'→'*'，末位 '>'→'**'。"""
    return _translate(
        pattern,
        src_sep=SUBJECT_SEPARATOR,
        dst_sep=KEYEXPR_SEPARATOR,
        wildcards=True,
        source="pattern",
    )


def keyexpr_to_pattern(keyexpr: str) -> str:
    """订阅侧 key expression → NATS 通配模式（pattern_to_keyexpr 的逆）。"""
    return _back_translate(
        keyexpr,
        src_sep=KEYEXPR_SEPARATOR,
        dst_sep=SUBJECT_SEPARATOR,
        wildcards=True,
        source="key_expr",
    )


def is_pattern(value: str) -> bool:
    """按 NATS 语义判定是否为订阅模式（含 '*' 或 '>' 整块）。"""
    return any(chunk in {NATS_SINGLE_WILDCARD, NATS_MULTI_WILDCARD} for chunk in value.split(SUBJECT_SEPARATOR))


def chunk_needs_encoding(chunk: str) -> bool:
    return not chunk or any(char not in SAFE_CHUNK_CHARS for char in chunk)


def encode_chunk(text: str) -> str:
    """把任意动态值压成合法且不含通配语义的分块；与 decode_chunk 严格互逆。"""
    if not text:
        raise KeyExprMappingError("空值无法编码为 key expression 分块", detail={"value": text})
    raw = text.encode("utf-8")
    if len(raw) > _MAX_CHUNK_BYTES:
        raise KeyExprMappingError(
            "动态值字节长度超分块上限",
            detail={"value_bytes": len(raw), "limit": _MAX_CHUNK_BYTES},
        )
    out: list[str] = []
    for byte in raw:
        char = chr(byte)
        if char in SAFE_CHUNK_CHARS:
            out.append(char)
        elif char == ESCAPE_CHAR:
            out.append(ESCAPE_CHAR + ESCAPE_CHAR)
        else:
            out.append(f"{ESCAPE_CHAR}{byte:02x}")
    return "".join(out)


def decode_chunk(chunk: str) -> str:
    """encode_chunk 的逆；转义序列不完整时抛 KeyExprMappingError。"""
    if not chunk:
        raise KeyExprMappingError("空分块无法解码", detail={"chunk": chunk})
    if chunk in {NATS_SINGLE_WILDCARD, ZENOH_MULTI_WILDCARD}:
        raise KeyExprMappingError("通配块不是编码后的动态值", detail={"chunk": chunk})
    buffer = bytearray()
    index = 0
    while index < len(chunk):
        char = chunk[index]
        if char != ESCAPE_CHAR:
            if char not in SAFE_CHUNK_CHARS:
                raise KeyExprMappingError("分块含未转义字符", detail={"chunk": chunk, "char": char})
            buffer.extend(char.encode("ascii"))
            index += 1
            continue
        if index + 1 >= len(chunk):
            raise KeyExprMappingError("分块以悬空转义符结尾", detail={"chunk": chunk})
        if chunk[index + 1] == ESCAPE_CHAR:
            buffer.extend(ESCAPE_CHAR.encode("ascii"))
            index += 2
            continue
        hex_part = chunk[index + 1 : index + 3]
        if len(hex_part) < 2:
            raise KeyExprMappingError("转义序列不完整", detail={"chunk": chunk, "tail": hex_part})
        try:
            byte = int(hex_part, 16)
        except ValueError as exc:
            raise KeyExprMappingError(
                "转义序列非十六进制",
                detail={"chunk": chunk, "escape": hex_part},
            ) from exc
        buffer.append(byte)
        index += 3
    try:
        return buffer.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise KeyExprMappingError(
            "转义后的字节序列不是合法 UTF-8",
            detail={"chunk": chunk, "reason": str(exc)},
        ) from exc


def encode_subject(value: str) -> str:
    """单分块 subject 片段：动态值 → 转义后可直接进 subject 或 key expression。"""
    return encode_chunk(value)


def data_keyexpr(source: str, metric: str) -> str:
    """遥测 subject 的等价 key expression：动态字段自动转义。"""
    return subject_to_keyexpr(f"data.{encode_chunk(source)}.{encode_chunk(metric)}")


__all__ = [
    "ESCAPE_CHAR",
    "ILLEGAL_CHUNK_CHARS",
    "KEYEXPR_SEPARATOR",
    "NATS_MULTI_WILDCARD",
    "NATS_SINGLE_WILDCARD",
    "SAFE_CHUNK_CHARS",
    "SUBJECT_SEPARATOR",
    "ZENOH_MULTI_WILDCARD",
    "ZENOH_SINGLE_WILDCARD",
    "chunk_needs_encoding",
    "data_keyexpr",
    "decode_chunk",
    "encode_chunk",
    "encode_subject",
    "is_pattern",
    "keyexpr_to_pattern",
    "keyexpr_to_subject",
    "pattern_to_keyexpr",
    "subject_to_keyexpr",
]
