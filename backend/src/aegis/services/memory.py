"""记忆服务（§4.3 / D5）：四类记忆统一门面 + 智能体共享记忆命名空间（群体认知对齐的实体载体）。

四类记忆的对应与承载（不自建新存储，统一门面）：
- working  工作记忆：单次调用的对话/上下文——由调用方持有，本服务不落库（§4.3：会话态）。
- episodic 情景记忆：场景/案例——委托注入的 `episodic` 提供者（案例库）。
- semantic 语义记忆：向量检索——委托注入的 `semantic` 提供者（pgvector）。
- knowledge 知识记忆：图谱/程序知识——委托注入的 `knowledge` 提供者（Graphiti）。

`agent_memory` 共享命名空间的实体是 `aegis.domain.messages.AgentMemoryRecord`，
落地有两个后端：**装配点注入的 Postgres 实现**（`persistence/agent_memory.py`，建表脚本
`sql/007_agent_memory.sql`）与没接库时的进程内参考实现 `InMemoryAgentMemory`。两者对外行为同口径
（同一套过滤参数、同一套排序、向量缺位时同样退到"过滤 + 时序"），所以"换存储"不改变调用方看到的事实。
语义列可为 NULL：没配嵌入腿时向量就是没算，读侧退到过滤+时序，不拿随机向量冒充语义命中。

这是"共同记忆/群体认知对齐"的实体：assess 写研判、feedback 写效果教训、assess 下次检索引用——
跨事件、跨智能体的经验闭环（§3.2 memory.search 同区域历史研判）。

边界（铁律 3）：内核 services 不 import 存储腿/可选腿；提供者、共享存储与嵌入器都是注入的窄 Protocol，
缺位即如实降级（`read_categories` 把"这条腿没接""接了但没命中""读了但出错"分成三种事实返回，
嵌入失败计入 `stats`），绝不把"没接上"读成"没有相关记忆"。
删除/修正走显式接口（记忆可治理）。
"""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from aegis.domain.messages import AgentMemoryRecord, SharedKind, new_memory_record_id

log = logging.getLogger("aegis.services.memory")

__all__ = ["AgentMemoryRecord", "AgentMemoryStore", "InMemoryAgentMemory", "MemoryService", "SharedKind", "new_memory_record_id"]


@runtime_checkable
class AgentMemoryStore(Protocol):
    """共享命名空间的存储面。`embedding=None` 是事实（没算/没配嵌入腿），不是"没有相似项"。"""

    async def write(self, record: AgentMemoryRecord, *, embedding: Sequence[float] | None = None, embedded_model: str = "") -> str: ...

    async def search(
        self,
        *,
        kind: SharedKind | None = None,
        agent_id: str | None = None,
        region_code: str | None = None,
        limit: int = 5,
        query_embedding: Sequence[float] | None = None,
    ) -> list[AgentMemoryRecord]: ...

    async def delete(self, record_id: str) -> bool: ...


@runtime_checkable
class MemoryEmbedder(Protocol):
    """嵌入腿的窄接口（与 `retrieval.embedder.Embedder` 同形，按 duck-typing 注入，内核不 import 检索腿）。"""

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    """本仓嵌入按 L2 归一交付（见 `retrieval/embedder.py`），故点积即余弦；长度不符按最短板算并如实降级。"""
    n = min(len(left), len(right))
    return sum(float(left[i]) * float(right[i]) for i in range(n))


class InMemoryAgentMemory:
    """共享记忆命名空间的进程内参考实现（有界、新在前）。接了 Postgres 时由 `PostgresAgentMemory` 顶替。"""

    def __init__(self, *, capacity: int = 1_000) -> None:
        self._capacity = max(1, capacity)
        self._records: deque[tuple[AgentMemoryRecord, tuple[float, ...] | None]] = deque(maxlen=self._capacity)
        self.evicted = 0

    @property
    def capacity(self) -> int:
        """状态面要报这一档后端的真实上限，不去读私有字段。"""
        return self._capacity

    async def write(self, record: AgentMemoryRecord, *, embedding: Sequence[float] | None = None, embedded_model: str = "") -> str:
        vector = tuple(float(v) for v in embedding) if embedding is not None else None
        for index, (existing, _) in enumerate(self._records):
            if existing.record_id == record.record_id:
                # 与 Postgres 侧 `ON CONFLICT (record_id) DO UPDATE` 同口径：同一 id 重写就是修正这条记忆，
                # 原位覆盖、不追加第二条。否则"治理"在这一档后端会变成"攒出两句互相矛盾的经验"。
                self._records[index] = (record, vector)
                return record.record_id
        if len(self._records) == self._capacity:
            self.evicted += 1  # 满了丢最旧并计数，绝不无界吃内存（与全平台同一口径）。
        self._records.append((record, vector))
        return record.record_id

    async def search(
        self,
        *,
        kind: SharedKind | None = None,
        agent_id: str | None = None,
        region_code: str | None = None,
        limit: int = 5,
        query_embedding: Sequence[float] | None = None,
    ) -> list[AgentMemoryRecord]:
        matched = [
            (record, vector)
            for record, vector in self._records
            if (kind is None or record.kind == kind)
            and (agent_id is None or record.agent_id == agent_id)
            and (region_code is None or record.region_code == region_code)
        ]
        if query_embedding is not None:
            # 与 Postgres 侧同一条口径：向量相似度排序，**没向量的行不参与**（宁可少给，不混排两种距离）。
            scored = [(_cosine(query_embedding, vector or ()), record) for record, vector in matched if vector is not None]
            scored.sort(key=lambda item: item[0], reverse=True)
            return [record for _, record in scored[: max(1, int(limit))]]
        return [record for record, _ in reversed(matched)][: max(1, int(limit))]  # 新在前

    async def delete(self, record_id: str) -> bool:
        for index, (record, _) in enumerate(self._records):
            if record.record_id == record_id:
                del self._records[index]
                return True
        return False


@runtime_checkable
class CategoryProvider(Protocol):
    """情景/语义/知识三类记忆的读取面（各由既有服务实现，装配点注入）。"""

    async def recall(
        self, query: str, *, region_code: str | None = None, hazard_type: str | None = None, limit: int = 3
    ) -> list[dict[str, object]]: ...


class MemoryService:
    """四类记忆的门面 + 共享命名空间的读写治理入口。`store` 必给，其余都按可用性子集注入。"""

    def __init__(
        self,
        store: AgentMemoryStore,
        *,
        episodic: CategoryProvider | None = None,
        semantic: CategoryProvider | None = None,
        knowledge: CategoryProvider | None = None,
        embedder: MemoryEmbedder | None = None,
        embedded_model: str = "",
        backend: str = "memory",
    ) -> None:
        self._store = store
        self._providers = {"episodic": episodic, "semantic": semantic, "knowledge": knowledge}
        self._embedder = embedder
        self.embedded_model = embedded_model
        #: 这一门面底下接的是哪种存储：状态面与"记忆为什么是空的"这类问题要先答得出来。
        self.backend = backend
        self.stats: dict[str, int] = {"嵌入失败": 0, "写入": 0, "读取": 0}

    # ---------- 共享命名空间（群体认知） ----------

    async def write_shared(
        self,
        *,
        kind: SharedKind,
        agent_id: str,
        content: str,
        record_id: str,
        region_code: str | None = None,
        refs: list[str] | None = None,
        disagreement: bool = False,
        valid_at: str,
    ) -> str:
        record = AgentMemoryRecord(
            record_id=record_id,
            agent_id=agent_id,
            kind=kind,
            content=content[:2_000],
            region_code=region_code,
            refs=list(refs or [])[:32],
            disagreement=disagreement,
            valid_at=valid_at,
        )
        embedding = self._embed_one(record.content)
        self.stats["写入"] += 1
        return await self._store.write(record, embedding=embedding, embedded_model=self.embedded_model if embedding else "")

    async def read_shared(
        self,
        *,
        kind: SharedKind | None = None,
        agent_id: str | None = None,
        region_code: str | None = None,
        limit: int = 5,
        query_embedding: Sequence[float] | None = None,
    ) -> list[AgentMemoryRecord]:
        self.stats["读取"] += 1
        return await self._store.search(kind=kind, agent_id=agent_id, region_code=region_code, limit=limit, query_embedding=query_embedding)

    async def forget(self, record_id: str) -> bool:
        """记忆可治理：删除/修正在管理端显式调用（§4.3）。"""
        return await self._store.delete(record_id)

    @property
    def evicted(self) -> int:
        """进程内后端的淘汰计数；Postgres 侧没有"满了丢最旧"这一说，如实回 0。"""
        return int(getattr(self._store, "evicted", 0))

    @property
    def store(self) -> AgentMemoryStore:
        """门面底下的那一档后端：状态面与用例要能问出"这是库还是进程内存"。"""
        return self._store

    def set_embedder(self, embedder: MemoryEmbedder | None) -> None:
        """装配点注入/换出嵌入腿的入口。

        给 setter 而不是让外面拨 `_embedder`：换腿必须连带把 `embedded_model` 一起换掉，
        两者分开的后果是行上写着 A 模型、实际按 B 模型算，向量口径就查不回来了。
        """
        self._embedder = embedder
        self.embedded_model = str(getattr(embedder, "model_id", "") or "") if embedder is not None else ""

    @property
    def embedder_ready(self) -> bool:
        """这一档门面底下有没有嵌入腿：决定 `query=` 到底是语义检索，还是被静默退化成时序。"""
        return self._embedder is not None

    def embed_query(self, text: str) -> list[float] | None:
        """把查询词算成向量；算不出就是 None，由调用方决定怎么说这一档序。

        故意返回 None 而不是退化成"按时间"再声称按语义：调用方拿到 None 才知道自己退档了。
        """
        return self._embed_one(text)

    def _embed_one(self, text: str) -> list[float] | None:
        """尽力取向量：嵌入腿缺位或失败都返回 None，并把失败计数带出来（铁律 7）。"""
        if self._embedder is None or not text:
            return None
        try:
            vectors = list(self._embedder.embed([text]))
        except Exception as exc:
            self.stats["嵌入失败"] += 1
            log.warning("共享记忆嵌入失败（该条按无向量存）", extra={"err": type(exc).__name__})
            return None
        if not vectors or not vectors[0]:
            self.stats["嵌入失败"] += 1
            return None
        return list(vectors[0])

    # ---------- 四类记忆读取门面（情景/语义/知识；缺位、空命中、降级是三种事实） ----------

    async def read_categories(
        self, query: str, *, region_code: str | None = None, hazard_type: str | None = None, limit: int = 3
    ) -> dict[str, dict[str, object]]:
        """每一类各自回一个**带状态**的结果，而不是三张可能为空的列表。

        为什么要单独说这件事：门面原本对三种完全不同的现场都回 `[]`——
        ① 这条记忆类压根没接线、② 接了但这次没命中、③ 接了且读它时出错了。
        §4.3 自己定的纪律就是"绝不把'没接上'读成'没有相关记忆'"，
        而一个空列表恰好把这两件事说成了同一件。现在 `connected` 与 `note` 是分开的字段，
        读者（`/api/v1/memory/categories`）第一次有可能回答"这台为什么读不出图谱"。
        """
        result: dict[str, dict[str, object]] = {}
        for name, provider in self._providers.items():
            if provider is None:
                result[name] = {"connected": False, "items": [], "note": "该记忆类未接入（装配点没给这条腿）"}
                continue
            try:
                items = await provider.recall(query, region_code=region_code, hazard_type=hazard_type, limit=limit)
            except Exception as exc:
                log.warning("记忆门面某一类降级", extra={"category": name, "err": type(exc).__name__})
                result[name] = {"connected": True, "items": [], "note": f"读取降级: {type(exc).__name__}"}
            else:
                result[name] = {"connected": True, "items": list(items), "note": "" if items else "接了，但这句查询没命中"}
        return result
