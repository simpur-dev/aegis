"""工作流存储：定义（带版本、不可变）与实例运行态。

定义与实例分离是"柔性"的落点：修订模板产生新版本，在途实例继续按创建时快照执行。

lineage 按 name 回填：同一名字最早版本的 `workflow_id` 就是整条版本族的 `lineage_id`。
旧定义、内置模板与人工核签模板在第一次 `save()` 时自动补齐，不需要数据迁移（本就内存）。
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Sequence
from typing import Any

from aegis.workflow.documents import WorkflowDocument, document_from_definition, document_hash
from aegis.workflow.model import EdgeDef, NodeDef, WorkflowDef


def new_workflow_id() -> str:
    return f"wf_{uuid.uuid4().hex[:12]}"


def new_instance_id() -> str:
    return f"wfi_{uuid.uuid4().hex[:12]}"


class DuplicateWorkflowName(Exception):
    pass


class UnknownWorkflow(Exception):
    pass


class WorkflowRepository:
    """内存仓储（与 ADR-0003 一致：先内存、持久化后置），按 name → 版本链组织。

    三个索引各司其职：
    - `_versions/_latest/_index` 按 name 保持旧口径（引擎与旧端点仍走这里）；
    - `_lineage_versions` 按 lineage 聚合全部版本，归档版本不删除；
    - `_documents` 保存发布时的完整文档快照——版式不属于 `WorkflowDef`，但必须可回溯。
    """

    def __init__(self, capacity: int = 2_000) -> None:
        if capacity <= 0:
            raise ValueError("capacity 必须为正")
        self._by_id: dict[str, WorkflowDef] = {}
        self._versions: dict[str, list[int]] = {}
        self._index: dict[tuple[str, int], str] = {}  # (name, version) → workflow_id
        self._latest: dict[str, str] = {}  # name → 最新版本的 workflow_id
        self._lineage_versions: dict[str, list[str]] = {}
        self._lineage_by_name: dict[str, str] = {}
        self._documents: dict[str, WorkflowDocument] = {}
        self._capacity = capacity
        self._lock = asyncio.Lock()

    async def save(self, definition: WorkflowDef) -> WorkflowDef:
        async with self._lock:
            lineage = self._resolve_lineage(definition)
            content_hash = definition.content_hash or document_hash(document_from_definition(definition))
            normalized = definition.model_copy(update={"lineage_id": lineage, "content_hash": content_hash})
            return self._insert_locked(normalized)

    async def save_next(self, definition: WorkflowDef, *, document: WorkflowDocument) -> WorkflowDef:
        """锁内分配下一个版本号并把文档快照一起落账。

        版本号从**整条 lineage** 取最大值加一，而不是只看同名未归档记录：归档不删除历史，
        恢复后继续追加也不会撞号。lineage 必须与名称的当前归属一致：发布/修订不允许
        把内容追加到别人的族谱里。
        """
        async with self._lock:
            lineage = definition.lineage_id or definition.workflow_id
            owner = self._lineage_by_name.get(definition.name)
            if owner is not None and owner != lineage:
                raise DuplicateWorkflowName(f"名称已被另一条工作流占用: {definition.name}")
            return self._append_version_locked(definition, document=document, lineage=lineage)

    async def save_next_named(self, definition: WorkflowDef, *, document: WorkflowDocument) -> WorkflowDef:
        """旧 `create` 的锁内版本分配：同名归入已有 lineage，首次建族用新 ID 当 lineage。

        旧端点允许“同名再建一份 = 追加新版本”，模板 `force` 重注册也依赖这一点；
        与 `save_next` 的区别只在 lineage 取自名称归属，而不是要求调用方先选中一族。
        """
        async with self._lock:
            lineage = self._lineage_by_name.get(definition.name) or definition.lineage_id or definition.workflow_id
            return self._append_version_locked(definition, document=document, lineage=lineage)

    def _append_version_locked(self, definition: WorkflowDef, *, document: WorkflowDocument, lineage: str) -> WorkflowDef:
        next_version = max((self._by_id[wid].version for wid in self._lineage_versions.get(lineage, [])), default=0) + 1
        stored = definition.model_copy(
            update={
                "lineage_id": lineage,
                "version": next_version,
                "content_hash": document_hash(document),
            }
        )
        inserted = self._insert_locked(stored)
        self._documents[inserted.workflow_id] = document
        return inserted

    def _insert_locked(self, definition: WorkflowDef) -> WorkflowDef:
        existing = self._by_id.get(definition.workflow_id)
        if existing is not None and (existing.name, existing.version) != (definition.name, definition.version):
            # 同一 ID 只能改状态/描述这类同版本修补；改名或换版本必须走 rename_lineage/save_next，
            # 否则 `_index` 会留下指向该 ID 的旧键，按名字查到的版本链与 ID 内容对不上。
            raise DuplicateWorkflowName(
                f"workflow_id {definition.workflow_id} 已属于 {existing.name} v{existing.version}，"
                f"不能改挂到 {definition.name} v{definition.version}"
            )
        key = (definition.name, definition.version)
        holder = self._index.get(key)
        if holder is not None and holder != definition.workflow_id:
            raise DuplicateWorkflowName(f"同名同版本冲突: {definition.name} v{definition.version}")
        if len(self._by_id) >= self._capacity and definition.workflow_id not in self._by_id:
            raise RuntimeError(f"定义仓储已达容量上限 {self._capacity}")

        lineage = definition.lineage_id
        previous_owner = self._lineage_by_name.get(definition.name)
        if previous_owner is not None and previous_owner != lineage:
            raise DuplicateWorkflowName(f"名称已被另一条工作流占用: {definition.name}")

        self._by_id[definition.workflow_id] = definition
        self._index[key] = definition.workflow_id
        versions = self._versions.setdefault(definition.name, [])
        if definition.version not in versions:
            versions.append(definition.version)
            versions.sort()
        if definition.version == versions[-1]:
            self._latest[definition.name] = definition.workflow_id
        self._lineage_by_name[definition.name] = lineage

        lineage_ids = self._lineage_versions.setdefault(lineage, [])
        if definition.workflow_id not in lineage_ids:
            lineage_ids.append(definition.workflow_id)
            lineage_ids.sort(key=lambda wid: self._by_id[wid].version)
        return definition

    def _resolve_lineage(self, definition: WorkflowDef) -> str:
        """同名优先：旧 `revise_definition` 每次换新 workflow_id，不能让它另立一族。"""
        existing = self._lineage_by_name.get(definition.name)
        if existing is not None:
            return existing
        if definition.lineage_id:
            return definition.lineage_id
        return definition.workflow_id

    def get(self, workflow_id: str) -> WorkflowDef | None:
        return self._by_id.get(workflow_id)

    def latest(self, name: str) -> WorkflowDef | None:
        workflow_id = self._latest.get(name)
        if workflow_id is None:
            return None
        return self._by_id.get(workflow_id)

    def latest_by_lineage(self, lineage_id: str) -> WorkflowDef | None:
        versions = self.versions_of(lineage_id)
        return versions[-1] if versions else None

    def versions(self, name: str) -> list[int]:
        return list(self._versions.get(name, []))

    def versions_of(self, lineage_id: str) -> list[WorkflowDef]:
        ids = self._lineage_versions.get(lineage_id, [])
        return [self._by_id[wid] for wid in ids if wid in self._by_id]

    def lineage_of(self, name: str) -> str | None:
        return self._lineage_by_name.get(name)

    def document_of(self, workflow_id: str) -> WorkflowDocument | None:
        definition = self._by_id.get(workflow_id)
        if definition is None:
            return None
        stored = self._documents.get(workflow_id)
        return stored if stored is not None else document_from_definition(definition)

    def list(self, *, include_archived: bool = False) -> list[WorkflowDef]:
        rows = sorted(self._by_id.values(), key=lambda d: (d.name, d.version))
        return rows if include_archived else [d for d in rows if d.status == "active"]

    def next_version(self, name: str) -> int:
        versions = self._versions.get(name) or [0]
        return max(versions) + 1

    async def rename_lineage(self, lineage_id: str, name: str) -> WorkflowDef:
        """锁内原子重命名整条 lineage；归档状态、版本号与运行历史都不动。"""
        async with self._lock:
            ids = list(self._lineage_versions.get(lineage_id, []))
            if not ids:
                raise UnknownWorkflow(f"工作流 lineage 不存在: {lineage_id}")
            owner = self._lineage_by_name.get(name)
            if owner is not None and owner != lineage_id:
                raise DuplicateWorkflowName(f"名称已被另一条工作流占用: {name}")
            old_name = self._by_id[ids[0]].name
            if old_name == name:
                return self._by_id[ids[-1]]

            old_versions = self._versions.pop(old_name, [])
            self._latest.pop(old_name, None)
            self._lineage_by_name.pop(old_name, None)
            for workflow_id in ids:
                old = self._by_id[workflow_id]
                self._index.pop((old_name, old.version), None)
                renamed = old.model_copy(update={"name": name})
                document = self._documents.get(workflow_id)
                if document is not None:
                    renamed_document = document.model_copy(update={"name": name})
                    self._documents[workflow_id] = renamed_document
                else:
                    # 旧版本没有存过文档快照：按改名后的定义投影一份来算哈希，
                    # 否则版本列表里的 content_hash 还带着旧名字，和读出来的文档对不上。
                    renamed_document = document_from_definition(renamed)
                renamed = renamed.model_copy(update={"content_hash": document_hash(renamed_document)})
                self._by_id[workflow_id] = renamed
                self._index[(name, renamed.version)] = workflow_id
            self._versions[name] = sorted(old_versions)
            self._latest[name] = ids[-1]
            self._lineage_by_name[name] = lineage_id
            return self._by_id[ids[-1]]

    def __len__(self) -> int:
        return len(self._by_id)


def build_definition(
    *,
    name: str,
    nodes: Sequence[dict[str, Any] | NodeDef],
    edges: Sequence[dict[str, Any] | EdgeDef],
    description: str = "",
    workflow_id: str | None = None,
    version: int = 1,
    created_by: str = "platform.workflow",
    lineage_id: str = "",
    content_hash: str = "",
) -> WorkflowDef:
    return WorkflowDef(
        workflow_id=workflow_id or new_workflow_id(),
        lineage_id=lineage_id,
        name=name,
        description=description,
        version=version,
        nodes=[node if isinstance(node, NodeDef) else NodeDef.model_validate(node) for node in nodes],
        edges=[edge if isinstance(edge, EdgeDef) else EdgeDef.model_validate(edge) for edge in edges],
        created_by=created_by,
        content_hash=content_hash,
    )
