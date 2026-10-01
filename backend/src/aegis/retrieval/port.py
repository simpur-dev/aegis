"""检索索引端口：把"两条腿去哪儿取候选"这件事收成一个接口。

为什么要有这一层：`service.py` 原本假设密集腿在 pgvector、词法腿在进程内 BM25，
两条腿各自有各自的取数方式。当整个索引可以交给一个外部引擎（本机 seekdb 同时有
向量 ANN 与 ngram 全文）时，服务侧要的仍然是同两件事——"按向量给 top-k""按词面给 top-k"，
差别只在去哪儿取。端口放在检索层，内核与装配层都不许反向依赖具体引擎。

`IndexedRow` 带整条 `KnowledgeDoc` 而不是只带 id：外部引擎里存着正文与结构化列，
回读一次就够，不该再为了拼正文回查案例库（那会让召回依赖一份可能没预置的本地数据）。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from aegis.retrieval.docs import KnowledgeDoc


@dataclass(frozen=True, slots=True)
class IndexFilter:
    """结构化过滤面：与 `KnowledgeDoc.matches()` / 密集腿 SQL 的 WHERE 同一语义。

    `None` 一律表示"这个维度不设限"，不是"匹配空值"——把两者混起来会让按灾种过滤
    静默变成"只要没写灾种都能命中"。
    """

    hazard_type: str | None = None
    region_code: str | None = None
    since: datetime | None = None
    until: datetime | None = None
    score_threshold: float | None = None


@dataclass(frozen=True, slots=True)
class IndexedRow:
    """一条候选：引擎认为它相关的程度 + 语料本体。"""

    doc: KnowledgeDoc
    score: float


@runtime_checkable
class HybridIndex(Protocol):
    """一个能同时提供密集腿与词法腿候选的索引。

    `status()` 属于端口的一部分：装配面要如实回答"这台引擎建成了什么形态"
    （解析器、维度、已灌条数、最近一次失败），而这些只有在启动期之后才成立。
    """

    async def dense_topk(
        self,
        vector: Sequence[float],
        *,
        limit: int,
        filter: IndexFilter,
    ) -> Sequence[IndexedRow]: ...

    async def lexical_topk(
        self,
        text: str,
        *,
        limit: int,
        filter: IndexFilter,
    ) -> Sequence[IndexedRow]: ...

    def status(self) -> Mapping[str, object]: ...


@runtime_checkable
class IndexWriter(Protocol):
    """可写入的索引：启动期需要建表并把语料与向量灌进去。

    读面和写面故意分成两个协议——只读代理（比如一个只暴露检索 API 的服务）应当能当
    `HybridIndex` 用，而不必假装自己能被灌数据。
    """

    async def prepare_schema(self) -> Mapping[str, object]: ...

    async def upsert(self, docs: Sequence[KnowledgeDoc], vectors: Sequence[Sequence[float]]) -> int: ...
