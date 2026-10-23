"""草稿层：唯一的可变对象；写入用 `base_revision` 做乐观并发，不做 last-write-wins。"""

from __future__ import annotations

import asyncio

from aegis.workflow.documents import WorkflowDocument, WorkflowDraft, WorkflowLayout
from aegis.workflow.errors import WorkflowAppError, WorkflowErrorCode
from aegis.workflow.model import NodeDef
from aegis.workflow.ports import (
    Clock,
    DraftStorePort,
    IdFactory,
    SystemClock,
    UuidIdFactory,
    WorkflowRepositoryPort,
)


class LineageLocks:
    """同一条 lineage 的草稿写入、发布、恢复与重命名共用一把锁。

    仓储自己的锁只保护索引；发布要先比对 revision/哈希再写版本，这两个动作必须
    在同一把应用锁里完成，否则两路并发发布都会在各自的比对里看到"还没人发布"。
    """

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}

    def for_lineage(self, lineage_id: str) -> asyncio.Lock:
        return self._locks.setdefault(lineage_id, asyncio.Lock())


class InMemoryDraftStore:
    def __init__(self) -> None:
        self._drafts: dict[str, WorkflowDraft] = {}

    def get(self, lineage_id: str) -> WorkflowDraft | None:
        return self._drafts.get(lineage_id)

    def save(self, draft: WorkflowDraft) -> WorkflowDraft:
        self._drafts[draft.lineage_id] = draft
        return draft


class DraftService:
    def __init__(
        self,
        repository: WorkflowRepositoryPort,
        store: DraftStorePort | None = None,
        *,
        locks: LineageLocks | None = None,
        clock: Clock | None = None,
        ids: IdFactory | None = None,
    ) -> None:
        self._repository = repository
        self._store = store if store is not None else InMemoryDraftStore()
        self._locks = locks if locks is not None else LineageLocks()
        self._clock = clock if clock is not None else SystemClock()
        self._ids = ids if ids is not None else UuidIdFactory()

    @property
    def locks(self) -> LineageLocks:
        return self._locks

    def peek(self, lineage_id: str) -> WorkflowDraft | None:
        return self._store.get(lineage_id)

    async def create(self, name: str) -> WorkflowDraft:
        """新建空白草稿：分配 lineage，只带一个 Start 节点。

        此时还没有任何已发布版本，名字的唯一性只在这里做一次早校验：等发布时才报重名，
        用户已经在画布上改了半天，代价太大。
        """
        async with self._locks.for_lineage(name):
            if self._repository.lineage_of(name) is not None:
                raise WorkflowAppError(WorkflowErrorCode.NAME_CONFLICT, f"工作流名称已存在: {name}")
            lineage_id = self._ids.workflow_id()
            document = WorkflowDocument(
                lineage_id=lineage_id,
                name=name,
                nodes=[NodeDef(node_id="start", type="start", config={"variables": []})],
                layout=WorkflowLayout(),
            )
            draft = WorkflowDraft(
                lineage_id=lineage_id,
                base_workflow_id=None,
                revision=1,
                document=document,
                updated_at=self._clock.now_iso(),
            )
            return self._store.save(draft)

    async def get(self, lineage_id: str) -> WorkflowDraft | None:
        """读到草稿；没有草稿但 lineage 已有发布版本时，从最新版本投影出一份 revision=1 的草稿。"""
        async with self._locks.for_lineage(lineage_id):
            existing = self._store.get(lineage_id)
            if existing is not None:
                return existing
            latest = self._repository.latest_by_lineage(lineage_id)
            if latest is None:
                return None
            document = self._repository.document_of(latest.workflow_id)
            if document is None:
                return None
            draft = WorkflowDraft(
                lineage_id=lineage_id,
                base_workflow_id=latest.workflow_id,
                revision=1,
                document=document,
                updated_at=self._clock.now_iso(),
            )
            return self._store.save(draft)

    async def put(self, lineage_id: str, document: WorkflowDocument, *, base_revision: int) -> WorkflowDraft:
        async with self._locks.for_lineage(lineage_id):
            if document.lineage_id != lineage_id:
                raise WorkflowAppError(
                    WorkflowErrorCode.VALIDATION_FAILED,
                    f"document.lineage_id 与路径不一致: {document.lineage_id} != {lineage_id}",
                )
            current = self._store.get(lineage_id)
            if current is None:
                if base_revision != 0:
                    raise WorkflowAppError(
                        WorkflowErrorCode.REVISION_CONFLICT,
                        "草稿尚不存在，base_revision 应为 0",
                        details={"server_revision": 0},
                    )
                latest = self._repository.latest_by_lineage(lineage_id)
                revision = 1
                base_workflow_id = latest.workflow_id if latest is not None else None
            else:
                if base_revision != current.revision:
                    raise WorkflowAppError(
                        WorkflowErrorCode.REVISION_CONFLICT,
                        f"草稿已被其他会话修改（服务器 revision={current.revision}）",
                        details={"server_revision": current.revision},
                    )
                revision = current.revision + 1
                base_workflow_id = current.base_workflow_id
            draft = WorkflowDraft(
                lineage_id=lineage_id,
                base_workflow_id=base_workflow_id,
                revision=revision,
                document=document,
                updated_at=self._clock.now_iso(),
            )
            return self._store.save(draft)

    def commit(self, draft: WorkflowDraft) -> WorkflowDraft:
        """发布/恢复/重命名在已持有 lineage 锁时直接落草稿，不再走 CAS。"""
        return self._store.save(draft)


__all__ = ["DraftService", "InMemoryDraftStore", "LineageLocks"]
