"""发布、重命名、版本恢复：所有"先比对再写"的动作都在同一把 lineage 锁里完成。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from aegis.workflow.documents import WorkflowDraft, definition_from_document, document_hash
from aegis.workflow.drafts import DraftService, LineageLocks
from aegis.workflow.errors import WorkflowAppError, WorkflowErrorCode
from aegis.workflow.model import WorkflowDef
from aegis.workflow.ports import Clock, IdFactory, SystemClock, UuidIdFactory, WorkflowRepositoryPort
from aegis.workflow.store import DuplicateWorkflowName, UnknownWorkflow


def version_summary(definition: WorkflowDef, *, is_current: bool) -> dict[str, Any]:
    return {
        "workflow_id": definition.workflow_id,
        "lineage_id": definition.lineage_id,
        "version": definition.version,
        "name": definition.name,
        "description": definition.description,
        "status": definition.status,
        "node_count": len(definition.nodes),
        "edge_count": len(definition.edges),
        "created_at": definition.created_at,
        "content_hash": definition.content_hash,
        "is_current": is_current,
    }


class PublishingService:
    def __init__(
        self,
        repository: WorkflowRepositoryPort,
        drafts: DraftService,
        *,
        locks: LineageLocks | None = None,
        ids: IdFactory | None = None,
        clock: Clock | None = None,
        validate_definition: Callable[[WorkflowDef], None] | None = None,
    ) -> None:
        self._repository = repository
        self._drafts = drafts
        self._locks = locks if locks is not None else drafts.locks
        self._ids = ids if ids is not None else UuidIdFactory()
        self._clock = clock if clock is not None else SystemClock()
        self._validate = validate_definition

    async def publish(self, lineage_id: str, *, base_revision: int) -> WorkflowDef:
        async with self._locks.for_lineage(lineage_id):
            draft = self._drafts.peek(lineage_id)
            if draft is None:
                raise WorkflowAppError(WorkflowErrorCode.NOT_FOUND, f"草稿不存在: {lineage_id}")
            if draft.revision != base_revision:
                raise WorkflowAppError(
                    WorkflowErrorCode.REVISION_CONFLICT,
                    f"草稿已被其他会话修改（服务器 revision={draft.revision}）",
                    details={"server_revision": draft.revision},
                )

            latest = self._repository.latest_by_lineage(lineage_id)
            if latest is not None and latest.status == "archived":
                raise WorkflowAppError(
                    WorkflowErrorCode.ARCHIVED,
                    "该工作流已归档，先取消归档再发布",
                    details={"workflow_id": latest.workflow_id},
                )

            content_hash = document_hash(draft.document)
            latest_hash = ""
            if latest is not None:
                latest_document = self._repository.document_of(latest.workflow_id)
                latest_hash = latest.content_hash or (document_hash(latest_document) if latest_document is not None else "")
            if latest_hash and latest_hash == content_hash:
                raise WorkflowAppError(
                    WorkflowErrorCode.NO_CHANGES,
                    "内容与当前发布版本一致，不制造空版本",
                    details={"content_hash": content_hash},
                )

            definition = definition_from_document(
                draft.document,
                workflow_id=self._ids.workflow_id(),
                created_by="platform.workflow",
                created_at=self._clock.now_iso(),
            )
            if self._validate is not None:
                try:
                    self._validate(definition)
                except Exception as exc:
                    raise WorkflowAppError(WorkflowErrorCode.VALIDATION_FAILED, str(exc)) from exc

            if latest is not None and latest.name != draft.document.name:
                try:
                    await self._repository.rename_lineage(lineage_id, draft.document.name)
                except DuplicateWorkflowName as exc:
                    raise WorkflowAppError(WorkflowErrorCode.NAME_CONFLICT, str(exc)) from exc
                except UnknownWorkflow as exc:
                    raise WorkflowAppError(WorkflowErrorCode.NOT_FOUND, str(exc)) from exc

            try:
                stored = await self._repository.save_next(definition, document=draft.document)
            except DuplicateWorkflowName as exc:
                raise WorkflowAppError(WorkflowErrorCode.NAME_CONFLICT, str(exc)) from exc
            except UnknownWorkflow as exc:
                raise WorkflowAppError(WorkflowErrorCode.NOT_FOUND, str(exc)) from exc

            self._drafts.commit(
                WorkflowDraft(
                    lineage_id=lineage_id,
                    base_workflow_id=stored.workflow_id,
                    revision=draft.revision + 1,
                    document=draft.document,
                    updated_at=self._clock.now_iso(),
                )
            )
            return stored

    async def revert_to_draft(self, lineage_id: str, workflow_id: str) -> WorkflowDraft:
        async with self._locks.for_lineage(lineage_id):
            definition = self._repository.get(workflow_id)
            if definition is None or definition.lineage_id != lineage_id:
                raise WorkflowAppError(WorkflowErrorCode.NOT_FOUND, f"版本不存在: {workflow_id}")
            document = self._repository.document_of(workflow_id)
            if document is None:
                raise WorkflowAppError(WorkflowErrorCode.NOT_FOUND, f"版本文档不存在: {workflow_id}")
            current = self._drafts.peek(lineage_id)
            draft = WorkflowDraft(
                lineage_id=lineage_id,
                base_workflow_id=workflow_id,
                revision=(current.revision + 1) if current is not None else 1,
                document=document,
                updated_at=self._clock.now_iso(),
            )
            return self._drafts.commit(draft)

    async def rename(self, lineage_id: str, name: str) -> tuple[WorkflowDef, WorkflowDraft | None]:
        async with self._locks.for_lineage(lineage_id):
            try:
                definition = await self._repository.rename_lineage(lineage_id, name)
            except DuplicateWorkflowName as exc:
                raise WorkflowAppError(WorkflowErrorCode.NAME_CONFLICT, str(exc)) from exc
            except UnknownWorkflow as exc:
                raise WorkflowAppError(WorkflowErrorCode.NOT_FOUND, str(exc)) from exc
            current = self._drafts.peek(lineage_id)
            if current is None or current.document.name == name:
                return definition, current
            draft = WorkflowDraft(
                lineage_id=lineage_id,
                base_workflow_id=current.base_workflow_id,
                revision=current.revision + 1,
                document=current.document.model_copy(update={"name": name}),
                updated_at=self._clock.now_iso(),
            )
            return definition, self._drafts.commit(draft)

    def list_versions(self, lineage_id: str) -> list[dict[str, Any]]:
        versions = self._repository.versions_of(lineage_id)
        if not versions:
            raise WorkflowAppError(WorkflowErrorCode.NOT_FOUND, f"工作流 lineage 不存在: {lineage_id}")
        current = versions[-1].workflow_id
        return [version_summary(definition, is_current=definition.workflow_id == current) for definition in reversed(versions)]


__all__ = ["PublishingService", "version_summary"]
