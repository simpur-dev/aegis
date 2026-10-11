"""记忆服务（§4.3 / D5）：四类记忆统一门面 + 智能体共享记忆命名空间（群体认知对齐的实体载体）。

四类记忆的对应与承载（不自建新存储，统一门面）：
- working  工作记忆：单次调用的对话/上下文——由调用方持有，本服务不落库（§4.3：会话态）。
- episodic 情景记忆：场景/案例——委托注入的 `episodic` 提供者（案例库）。
- semantic 语义记忆：向量检索——委托注入的 `semantic` 提供者（pgvector）。
- knowledge 知识记忆：图谱/程序知识——委托注入的 `knowledge` 提供者（Graphiti）。

新增 `agent_memory` 共享命名空间（本模块内建 In-Memory 参考实现；生产由装配点注入 pgvector 存储）：
`{agent_id, kind(assessment|disagreement|lesson), content, region_code, refs, valid_at}`。
这是"共同记忆/群体认知对齐"的实体：assess 写研判、feedback 写效果教训、assess 下次检索引用——
跨事件、跨智能体的经验闭环（§3.2 memory.search 同区域历史研判）。

边界（铁律 3）：内核 services 不 import 存储腿/可选腿；提供者与共享存储都是注入的窄 Protocol，
缺位即如实降级（该记忆类未接入返回空），绝不把"没接上"读成"没有相关记忆"。删除/修正走显式接口（记忆可治理）。
"""

from __future__ import annotations

import logging
import secrets
from collections import deque
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field

log = logging.getLogger("aegis.services.memory")

SharedKind = Literal["assessment", "disagreement", "lesson"]


def new_memory_record_id() -> str:
    return "mem_" + secrets.token_hex(8)


class AgentMemoryRecord(BaseModel):
    """共享命名空间里的一条记忆。字段即 §4.3 定义的群体认知实体。"""

    model_config = {"extra": "forbid"}

    record_id: str = Field(min_length=1, max_length=64)
    agent_id: str = Field(max_length=64)
    kind: SharedKind
    content: str = Field(min_length=1, max_length=2_000)
    region_code: str | None = Field(default=None, max_length=24)
    refs: list[str] = Field(default_factory=list)
    disagreement: bool = False
    valid_at: str = Field(min_length=4, max_length=64)


@runtime_checkable
class AgentMemoryStore(Protocol):
    async def write(self, record: AgentMemoryRecord) -> str: ...
    async def search(
        self, *, kind: SharedKind | None = None, agent_id: str | None = None, region_code: str | None = None, limit: int = 5
    ) -> list[AgentMemoryRecord]: ...
    async def delete(self, record_id: str) -> bool: ...


class InMemoryAgentMemory:
    """共享记忆命名空间的参考实现（有界、新在前）。生产的 pgvector 后端由装配点换入。"""

    def __init__(self, *, capacity: int = 1_000) -> None:
        self._capacity = max(1, capacity)
        self._records: deque[AgentMemoryRecord] = deque(maxlen=self._capacity)
        self._seq = 0
        self.evicted = 0

    async def write(self, record: AgentMemoryRecord) -> str:
        if len(self._records) == self._capacity:
            self.evicted += 1  # 满了丢最旧并计数，绝不无界吃内存（与全平台同一口径）。
        self._records.append(record)
        return record.record_id

    async def search(
        self, *, kind: SharedKind | None = None, agent_id: str | None = None, region_code: str | None = None, limit: int = 5
    ) -> list[AgentMemoryRecord]:
        out: list[AgentMemoryRecord] = []
        for record in reversed(self._records):  # 新在前
            if kind is not None and record.kind != kind:
                continue
            if agent_id is not None and record.agent_id != agent_id:
                continue
            if region_code is not None and record.region_code != region_code:
                continue
            out.append(record)
            if len(out) >= max(1, int(limit)):
                break
        return out

    async def delete(self, record_id: str) -> bool:
        for index, record in enumerate(self._records):
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
    def __init__(
        self,
        store: AgentMemoryStore,
        *,
        episodic: CategoryProvider | None = None,
        semantic: CategoryProvider | None = None,
        knowledge: CategoryProvider | None = None,
    ) -> None:
        self._store = store
        self._providers = {"episodic": episodic, "semantic": semantic, "knowledge": knowledge}

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
        return await self._store.write(record)

    async def read_shared(
        self, *, kind: SharedKind | None = None, agent_id: str | None = None, region_code: str | None = None, limit: int = 5
    ) -> list[AgentMemoryRecord]:
        return await self._store.search(kind=kind, agent_id=agent_id, region_code=region_code, limit=limit)

    async def forget(self, record_id: str) -> bool:
        """记忆可治理：删除/修正在管理端显式调用（§4.3）。"""
        return await self._store.delete(record_id)

    # ---------- 四类记忆读取门面（情景/语义/知识；缺位如实为空） ----------

    async def read_categories(
        self, query: str, *, region_code: str | None = None, hazard_type: str | None = None, limit: int = 3
    ) -> dict[str, list[dict[str, object]]]:
        result: dict[str, list[dict[str, object]]] = {}
        for name, provider in self._providers.items():
            if provider is None:
                result[name] = []  # 该记忆类未接入：如实空，不伪装成"没有相关内容"。
                continue
            try:
                result[name] = await provider.recall(query, region_code=region_code, hazard_type=hazard_type, limit=limit)
            except Exception as exc:
                log.warning("记忆门面某一类降级", extra={"category": name, "err": type(exc).__name__})
                result[name] = []
        return result
