"""工作流生命周期端口：应用层只依赖这些协议，持久化实现可替换（#53）。"""

from __future__ import annotations

import uuid
from typing import Any, Protocol

from aegis.domain.messages import now_iso
from aegis.workflow.documents import WorkflowDocument, WorkflowDraft
from aegis.workflow.model import WorkflowDef


class Clock(Protocol):
    def now_iso(self) -> str: ...


class IdFactory(Protocol):
    def workflow_id(self) -> str: ...

    def instance_id(self) -> str: ...


class SystemClock:
    def now_iso(self) -> str:
        return now_iso()


class UuidIdFactory:
    def workflow_id(self) -> str:
        return f"wf_{uuid.uuid4().hex[:12]}"

    def instance_id(self) -> str:
        return f"wfi_{uuid.uuid4().hex[:12]}"


class WorkflowRepositoryPort(Protocol):
    def get(self, workflow_id: str) -> WorkflowDef | None: ...

    def latest(self, name: str) -> WorkflowDef | None: ...

    def latest_by_lineage(self, lineage_id: str) -> WorkflowDef | None: ...

    def lineage_of(self, name: str) -> str | None: ...

    def versions_of(self, lineage_id: str) -> list[WorkflowDef]: ...

    def document_of(self, workflow_id: str) -> WorkflowDocument | None: ...

    async def save(self, definition: WorkflowDef) -> WorkflowDef: ...

    async def save_next(self, definition: WorkflowDef, *, document: WorkflowDocument) -> WorkflowDef: ...

    async def rename_lineage(self, lineage_id: str, name: str) -> WorkflowDef: ...


class DraftStorePort(Protocol):
    def get(self, lineage_id: str) -> WorkflowDraft | None: ...

    def save(self, draft: WorkflowDraft) -> WorkflowDraft: ...


class PreviewStorePort(Protocol):
    """批次 E 的试运行账本；先占住端口，实现不参与批次 A 的发布路径。"""

    def put(self, preview_id: str, payload: dict[str, Any]) -> None: ...

    def get(self, preview_id: str) -> dict[str, Any] | None: ...

    def prune(self, now_monotonic: float) -> int: ...


__all__ = [
    "Clock",
    "DraftStorePort",
    "IdFactory",
    "PreviewStorePort",
    "SystemClock",
    "UuidIdFactory",
    "WorkflowRepositoryPort",
]
