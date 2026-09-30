"""词法腿：进程内 BM25，中文按"字 unigram + 字 bigram + ASCII 词"切分。

为什么不用 Postgres 扩展做 BM25：pg_search/ParadeDB 与 VectorChord 这类方案要给生产库加
新扩展（其中 ParadeDB 侧是 AGPL），边缘站点要在每台盒子上装同样的扩展、还要跟镜像一起升级版本；
而我们真正需要词法召回的语料只有千级 chunk（预警知识 + 历史处置要点），
一次进程内索引（几 MB）就能覆盖，起库、装扩展、建索引这三件事都不必发生。

为什么不用 jieba（本仓未安装，也不是"找个分词器补上"那么简单）：
1. 多一个运行时依赖与词典版本，离线镜像要一起钉；
2. 分词器对本领域未登录词（沟谷名、设备型号、灾种俗称）会切成错词，切错即不可召回；
3. 字级 bigram 对短文档中文检索与分词方案的差距在小语料上很小，而它的失败模式是"多召回一点噪声"，
   分词的失败模式是"漏掉关键实体"——前者可以被 RRF 与重排吸收，后者不行。

因此本模块只做纯函数：切分 → 建索引 → 打分排序。没有 I/O、没有全局状态，
索引由调用方在启动时（离线）构建并注入检索服务。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from math import log
from typing import Final

from aegis.retrieval.docs import KnowledgeDoc
from aegis.retrieval.onnx_io import RetrievalArgumentError

# BM25 调参默认值：k1 控制词频饱和，b 控制文档长度归一化强度。
# 短 chunk 场景（数十至数百字）经验上 b 取 0.5~0.75；这里保留论文默认 0.75，交由调用方按指标调。
DEFAULT_K1: Final = 1.2
DEFAULT_B: Final = 0.75

_ASCII_WORD = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")


def _is_cjk(char: str) -> bool:
    """中日韩统一表意文字（含扩展 A）与兼容表意区。"""
    code = ord(char)
    return 0x3400 <= code <= 0x4DBF or 0x4E00 <= code <= 0x9FFF or 0xF900 <= code <= 0xFAFF


@dataclass(frozen=True, slots=True)
class LexicalHit:
    """词法腿的一条结果：score 是 BM25 分值（无上界，不与余弦同尺度，故只用于排名）。"""

    doc_id: str
    score: float


@dataclass(frozen=True, slots=True)
class LexicalIndex:
    """倒排索引快照：term -> {doc_id: 词频}，加上文档长度与语料统计量。

    字段全是不可变映射（构造后不再改），因此可以被多个协程并发读，
    不需要锁——检索服务在事件循环里跑，任何锁都会变成隐性串行点。
    """

    docs: Mapping[str, KnowledgeDoc]
    postings: Mapping[str, Mapping[str, int]]
    doc_lengths: Mapping[str, int]
    total_length: int

    def __len__(self) -> int:
        return len(self.docs)

    def get(self, doc_id: str) -> KnowledgeDoc | None:
        return self.docs.get(doc_id)

    @property
    def is_empty(self) -> bool:
        return not self.docs


def tokenize(text: str) -> list[str]:
    """切分：CJK 段产出字 unigram + bigram，ASCII 段产出小写词。

    保留 unigram 是为了单字查询（"雨""震"）不至于零召回；
    bigram 承担主要区分度（"泥石流"→ 泥石/流石 + 三个单字）。
    """
    tokens: list[str] = []
    for run, is_cjk_run in _runs(text):
        if is_cjk_run:
            tokens.extend(_cjk_tokens(run))
        else:
            tokens.append(run.lower())
    return tokens


def _runs(text: str) -> list[tuple[str, bool]]:
    """把文本切成 (片段, 是否 CJK) 的交替序列；标点/空白作为分隔被丢弃。"""
    runs: list[tuple[str, bool]] = []
    buffer = ""
    kind = False
    for char in text:
        if char in _ASCII_WORD:
            current = False
        elif _is_cjk(char):
            current = True
        else:
            if buffer:
                runs.append((buffer, kind))
                buffer = ""
            continue
        if buffer and kind == current:
            buffer += char
        else:
            if buffer:
                runs.append((buffer, kind))
            buffer, kind = char, current
    if buffer:
        runs.append((buffer, kind))
    return runs


def _cjk_tokens(run: str) -> list[str]:
    if len(run) == 1:
        return [run]
    unigrams = list(run)
    bigrams = [run[index : index + 2] for index in range(len(run) - 1)]
    return unigrams + bigrams


def build_lexical_index(docs: Iterable[KnowledgeDoc]) -> LexicalIndex:
    """由语料建 BM25 索引；空输入返回可查询的空索引（打分恒为空表，不作为异常）。

    同 doc_id 后写覆盖前写：索引以 doc_id 为主键，重复 id 说明上游去重没做，
    这里不报错也不双计（双计会让该文档长度与词频同时失真）。
    """
    stored: dict[str, KnowledgeDoc] = {}
    postings: dict[str, dict[str, int]] = {}
    lengths: dict[str, int] = {}
    for doc in docs:
        stored[doc.doc_id] = doc
        term_counts: dict[str, int] = {}
        for token in tokenize(doc.text):
            term_counts[token] = term_counts.get(token, 0) + 1
        lengths[doc.doc_id] = sum(term_counts.values())
        for token, count in term_counts.items():
            postings.setdefault(token, {})[doc.doc_id] = count
    return LexicalIndex(
        docs=stored,
        postings=postings,
        doc_lengths=lengths,
        total_length=sum(lengths.values()),
    )


def rank(
    index: LexicalIndex,
    query: str,
    *,
    k: int = 10,
    k1: float = DEFAULT_K1,
    b: float = DEFAULT_B,
    where: Callable[[KnowledgeDoc], bool] | None = None,
) -> list[LexicalHit]:
    """BM25 打分并返回 (分数降序, doc_id 升序) 的前 k 条；分数为 0 的文档不出现。

    `where` 是结构化过滤谓词（灾种/区域），与 dense 腿的 SQL WHERE 同语义，
    这样两条腿送进 RRF 的候选集才可比。
    """
    if k <= 0:
        raise RetrievalArgumentError("k 必须为正", detail={"k": k})
    doc_count = len(index.docs)
    if not doc_count:
        return []
    query_tokens = tokenize(query)
    if not query_tokens:
        return []

    avg_length = index.total_length / doc_count
    if avg_length <= 0:
        return []

    scores: dict[str, float] = {}
    for term in set(query_tokens):
        postings = index.postings.get(term)
        if not postings:
            continue
        # df 用全语料统计，不因 where 而变：否则同一 query 在不同灾种过滤器下分数不可比，
        # 也没法把索引常驻复用。过滤只决定"谁参与累加"。
        idf = log(1.0 + (doc_count - len(postings) + 0.5) / (len(postings) + 0.5))
        if idf <= 0.0:
            continue
        for doc_id, term_freq in postings.items():
            if where is not None and not _matches(index, doc_id, where):
                continue
            length = index.doc_lengths.get(doc_id, 0)
            denominator = term_freq + k1 * (1.0 - b + b * length / avg_length)
            if denominator <= 0:
                continue
            scores[doc_id] = scores.get(doc_id, 0.0) + idf * (term_freq * (k1 + 1.0)) / denominator

    ordered = sorted(((doc_id, value) for doc_id, value in scores.items() if value > 0.0), key=lambda item: (-item[1], item[0]))
    return [LexicalHit(doc_id=doc_id, score=value) for doc_id, value in ordered[:k]]


def _matches(index: LexicalIndex, doc_id: str, where: Callable[[KnowledgeDoc], bool]) -> bool:
    doc = index.docs.get(doc_id)
    return doc is not None and bool(where(doc))
