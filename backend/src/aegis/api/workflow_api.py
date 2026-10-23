"""工作流引擎 HTTP 接口（编排台与自动化调度的入口）。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

from aegis.api.deps import get_container
from aegis.container import PlatformContainer
from aegis.domain.messages import new_trace_id
from aegis.services.memory.label_flywheel import CAPTURE_MARKER
from aegis.workflow.documents import WorkflowDocument, WorkflowDraft
from aegis.workflow.engine import WorkflowConflictError, WorkflowEngine, WorkflowValidationError
from aegis.workflow.errors import WorkflowAppError, WorkflowErrorCode
from aegis.workflow.model import RetryPolicy
from aegis.workflow.publishing import version_summary


class _TrimsText:
    """写接口共用：先裁掉首尾空白，再走长度/模式校验。

    真机在画布上把节点名改成 `数据接入 `（尾巴多一个空格）保存后重新打开，
    列表里就出现了"看着同名、其实不同名"的两行；定义名与说明同理。
    人在名称里多敲的空格不是内容，不该进定义——存进去那一刻就该抹掉。

    `check_fields=False` 是因为这是混入：哪个子类有哪些字段由子类决定。
    代价是"字段改名后校验器悄悄失效"，所以 `tests/api/test_workflow_api.py` 里
    对三个写模型各钉了一条行为断言（不是只测这里写了代码）。
    """

    @field_validator("name", "description", mode="before", check_fields=False)
    @classmethod
    def _trim_text(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class NodeInput(_TrimsText, BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,31}$")
    type: str = Field(pattern=r"^[a-z][a-z0-9_]{1,31}$")
    name: str = Field(default="", max_length=64)
    config: dict[str, Any] = Field(default_factory=dict)
    sla_ms: int = Field(default=5_000, ge=100, le=3_600_000)
    timeout_ms: int = Field(default=5_000, ge=100, le=3_600_000)
    on_failure: str = Field(default="retry", pattern=r"^(retry|degrade|escalate|skip|abort)$")
    #: 读接口（`NodeDef`）会带出 `retry`，写接口若不认它，"取一份定义再原样存回去"
    #: 就会吃 422——画布的"打开→保存"走的正是这条路，症状是"我没改任何东西却存不回去"。
    #: 读写两侧必须同一形状；这里补齐，顺带让重试策略真的可以在创建时指定。
    retry: RetryPolicy = Field(default_factory=RetryPolicy)


class EdgeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str
    target: str
    condition: str = ""


class DefinitionInput(_TrimsText, BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=2, max_length=64)
    description: str = Field(default="", max_length=512)
    nodes: list[NodeInput] = Field(min_length=1, max_length=64)
    edges: list[EdgeInput] = Field(default_factory=list, max_length=256)


class ReviseInput(_TrimsText, BaseModel):
    model_config = ConfigDict(extra="forbid")

    nodes: list[NodeInput] | None = None
    edges: list[EdgeInput] | None = None
    description: str | None = Field(default=None, max_length=512)


class StartInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workflow_id: str | None = None
    workflow_name: str | None = Field(default=None, max_length=64)
    payload: dict[str, Any] = Field(default_factory=dict)
    trace_id: str | None = None


class DecisionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    choice: str = Field(min_length=1, max_length=32)
    by: str = Field(default="unknown", max_length=64)
    comment: str = Field(default="", max_length=512)
    #: 改判(adjust)时给出改写后的等级；缺它就不能生成标注样本——不替人编真值等级
    level: int | None = Field(default=None, ge=1, le=5)


class ConfigPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    config: dict[str, Any]


class InsertNodeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    after: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,31}$")
    node: NodeInput


class DraftPutInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_revision: int = Field(ge=0)
    document: WorkflowDocument


class PublishInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_revision: int = Field(ge=1)


class RevertInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workflow_id: str = Field(pattern=r"^wf_[0-9a-f]{12}$")


class RenameInput(_TrimsText, BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=64)


class CreateLineageInput(_TrimsText, BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=64)


class VersionSummaryOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workflow_id: str
    lineage_id: str
    version: int
    name: str
    description: str
    status: str
    node_count: int
    edge_count: int
    created_at: str
    content_hash: str
    is_current: bool


class VersionListOut(BaseModel):
    count: int
    items: list[VersionSummaryOut]


class DraftOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lineage_id: str
    base_workflow_id: str | None
    revision: int
    document: WorkflowDocument
    updated_at: str

    @classmethod
    def of(cls, draft: WorkflowDraft) -> DraftOut:
        return cls(**draft.model_dump())


class RenameOut(BaseModel):
    workflow_id: str
    lineage_id: str
    name: str
    version: int
    draft: DraftOut | None


def build_router() -> APIRouter:
    router = APIRouter(prefix="/api/v1/workflow", tags=["workflow"])

    def engine_of(container: PlatformContainer) -> WorkflowEngine:
        return container.workflow

    @router.get("/node-types")
    async def node_types(container: PlatformContainer = Depends(get_container)) -> dict[str, Any]:
        engine = engine_of(container)
        # 面板口径，不是注册表口径：内部原语（`chain_stage`）拖出来跑不起来，所以不给画布看。
        specs = engine.node_registry.panel_specs()
        return {
            "count": len(specs),
            "items": [
                {
                    "type": spec.type_name,
                    "description": spec.description,
                    "required_config": list(spec.required_config),
                    "optional_config": list(spec.optional_config),
                }
                for spec in specs
            ],
        }

    @router.get("/definitions")
    async def list_definitions(container: PlatformContainer = Depends(get_container)) -> dict[str, Any]:
        rows = engine_of(container).repository.list(include_archived=True)
        return {
            "count": len(rows),
            "items": [
                {
                    "workflow_id": row.workflow_id,
                    "lineage_id": row.lineage_id,
                    "name": row.name,
                    "version": row.version,
                    "status": row.status,
                    "description": row.description,
                    "node_count": len(row.nodes),
                    "edge_count": len(row.edges),
                    "created_at": row.created_at,
                    "content_hash": row.content_hash,
                }
                for row in rows
            ],
        }

    @router.post("/definitions", status_code=201)
    async def create_definition(
        payload: DefinitionInput,
        container: PlatformContainer = Depends(get_container),
    ) -> dict[str, Any]:
        try:
            definition = await engine_of(container).create_definition(
                name=payload.name,
                description=payload.description,
                nodes=[node.model_dump() for node in payload.nodes],
                edges=[edge.model_dump() for edge in payload.edges],
            )
        except WorkflowConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except WorkflowValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"workflow_id": definition.workflow_id, "name": definition.name, "version": definition.version}

    @router.post("/definitions/{workflow_id}/revise")
    async def revise_definition(
        workflow_id: str,
        payload: ReviseInput,
        container: PlatformContainer = Depends(get_container),
    ) -> dict[str, Any]:
        try:
            revised = await engine_of(container).revise_definition(
                workflow_id,
                nodes=None if payload.nodes is None else [node.model_dump() for node in payload.nodes],
                edges=None if payload.edges is None else [edge.model_dump() for edge in payload.edges],
                description=payload.description,
            )
        except WorkflowConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except WorkflowValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"workflow_id": revised.workflow_id, "name": revised.name, "version": revised.version}

    @router.post("/definitions/{workflow_id}/archive")
    async def archive_definition(workflow_id: str, container: PlatformContainer = Depends(get_container)) -> dict[str, Any]:
        try:
            archived = await engine_of(container).archive(workflow_id)
        except WorkflowValidationError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"workflow_id": archived.workflow_id, "status": archived.status}

    @router.post("/definitions/{workflow_id}/restore")
    async def restore_definition(workflow_id: str, container: PlatformContainer = Depends(get_container)) -> dict[str, Any]:
        engine = engine_of(container)
        if engine.definition_by_id(workflow_id) is None:
            raise HTTPException(status_code=404, detail=f"工作流定义不存在: {workflow_id}")
        try:
            restored = await engine.restore(workflow_id)
        except WorkflowValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"workflow_id": restored.workflow_id, "status": restored.status}

    @router.post("/instances")
    async def start_instance(payload: StartInput, container: PlatformContainer = Depends(get_container)) -> dict[str, Any]:
        engine = engine_of(container)
        trace_id = payload.trace_id or new_trace_id()
        try:
            if payload.workflow_id:
                detail = await engine.start(payload.workflow_id, trace_id=trace_id, payload=payload.payload)
            elif payload.workflow_name:
                detail = await engine.start_from_name(payload.workflow_name, trace_id=trace_id, payload=payload.payload)
            else:
                raise HTTPException(status_code=422, detail="需要提供 workflow_id 或 workflow_name")
        except WorkflowValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"trace_id": trace_id, **detail}

    @router.get("/definitions/{workflow_id}")
    async def get_definition(workflow_id: str, container: PlatformContainer = Depends(get_container)) -> dict[str, Any]:
        """一份定义的完整内容（节点、连线、参数）——画布"打开已有定义"靠它。

        列表接口只有计数，够用来挑一条，不够把它改出来。
        """
        definition = engine_of(container).definition_by_id(workflow_id)
        if definition is None:
            raise HTTPException(status_code=404, detail="工作流定义不存在")
        return definition.model_dump(mode="json")

    @router.post("/lineages", response_model=DraftOut, status_code=201)
    async def create_lineage(payload: CreateLineageInput, container: PlatformContainer = Depends(get_container)) -> DraftOut:
        """新建空白草稿（自动带 Start）；首次发布时复用这条 lineage 作为 v1。"""
        return DraftOut.of(await container.drafts.create(payload.name))

    @router.get("/lineages/{lineage_id}/versions", response_model=VersionListOut, responses={404: {"description": "lineage 不存在"}})
    async def list_versions(lineage_id: str, container: PlatformContainer = Depends(get_container)) -> VersionListOut:
        rows = container.publishing.list_versions(lineage_id)
        return VersionListOut(count=len(rows), items=[VersionSummaryOut(**row) for row in rows])

    @router.get("/versions/{workflow_id}", response_model=WorkflowDocument, responses={404: {"description": "版本不存在"}})
    async def get_version_document(workflow_id: str, container: PlatformContainer = Depends(get_container)) -> WorkflowDocument:
        """某个已发布版本的完整文档（节点/连线/版式）——版本只读查看与恢复为草稿都靠它。"""
        document = container.workflow.repository.document_of(workflow_id)
        if document is None:
            raise WorkflowAppError(WorkflowErrorCode.NOT_FOUND, f"版本不存在: {workflow_id}")
        return document

    @router.get("/lineages/{lineage_id}/draft", response_model=DraftOut)
    async def get_draft(lineage_id: str, container: PlatformContainer = Depends(get_container)) -> DraftOut:
        draft = await container.drafts.get(lineage_id)
        if draft is None:
            raise WorkflowAppError(WorkflowErrorCode.NOT_FOUND, f"草稿不存在: {lineage_id}")
        return DraftOut.of(draft)

    @router.put("/lineages/{lineage_id}/draft", response_model=DraftOut)
    async def put_draft(
        lineage_id: str,
        payload: DraftPutInput,
        container: PlatformContainer = Depends(get_container),
    ) -> DraftOut:
        draft = await container.drafts.put(lineage_id, payload.document, base_revision=payload.base_revision)
        return DraftOut.of(draft)

    @router.post("/lineages/{lineage_id}/publish", response_model=VersionSummaryOut)
    async def publish(lineage_id: str, payload: PublishInput, container: PlatformContainer = Depends(get_container)) -> VersionSummaryOut:
        stored = await container.publishing.publish(lineage_id, base_revision=payload.base_revision)
        return VersionSummaryOut(**version_summary(stored, is_current=True))

    @router.post("/lineages/{lineage_id}/revert-to-draft", response_model=DraftOut)
    async def revert_to_draft(
        lineage_id: str,
        payload: RevertInput,
        container: PlatformContainer = Depends(get_container),
    ) -> DraftOut:
        draft = await container.publishing.revert_to_draft(lineage_id, payload.workflow_id)
        return DraftOut.of(draft)

    @router.patch("/lineages/{lineage_id}", response_model=RenameOut)
    async def rename_lineage(
        lineage_id: str,
        payload: RenameInput,
        container: PlatformContainer = Depends(get_container),
    ) -> RenameOut:
        definition, draft = await container.publishing.rename(lineage_id, payload.name)
        return RenameOut(
            workflow_id=definition.workflow_id,
            lineage_id=definition.lineage_id,
            name=definition.name,
            version=definition.version,
            draft=DraftOut.of(draft) if draft is not None else None,
        )

    @router.get("/instances")
    async def list_instances(container: PlatformContainer = Depends(get_container)) -> dict[str, Any]:
        items = engine_of(container).instances()
        return {"count": len(items), "items": items}

    @router.get("/instances/{instance_id}")
    async def get_instance(instance_id: str, container: PlatformContainer = Depends(get_container)) -> dict[str, Any]:
        detail = engine_of(container).instance_detail(instance_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="工作流实例不存在")
        return detail

    @router.post("/instances/{instance_id}/nodes/{node_id}/decision")
    async def submit_decision(
        instance_id: str,
        node_id: str,
        payload: DecisionInput,
        container: PlatformContainer = Depends(get_container),
    ) -> dict[str, Any]:
        decision = {"choice": payload.choice, "by": payload.by, "comment": payload.comment, "level": payload.level}
        try:
            detail = await engine_of(container).resume(instance_id, node_id=node_id, decision=decision)
        except WorkflowValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        # 数据飞轮（D9）：这一单裁决的是"某条结论成不成立"，签完就地沉淀成标注样本；
        # 落没落地直接回给签字的人——静默丢弃就是"飞轮在转"的假象。
        instance_payload = detail.get("payload") or {}
        if instance_payload.get("capture_label") == CAPTURE_MARKER and container.labels is not None:
            detail["标注沉淀"] = await container.labels.record_review(instance_payload=instance_payload, decision=decision)
        return detail

    @router.patch("/instances/{instance_id}/nodes/{node_id}")
    async def patch_node_config(
        instance_id: str,
        node_id: str,
        payload: ConfigPatch,
        container: PlatformContainer = Depends(get_container),
    ) -> dict[str, Any]:
        try:
            return await engine_of(container).update_node_config(instance_id, node_id, payload.config)
        except WorkflowValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/instances/{instance_id}/nodes")
    async def insert_node(
        instance_id: str,
        payload: InsertNodeInput,
        container: PlatformContainer = Depends(get_container),
    ) -> dict[str, Any]:
        try:
            return await engine_of(container).insert_node(instance_id, after=payload.after, node=payload.node.model_dump())
        except WorkflowValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/instances/{instance_id}/nodes/{node_id}/bypass")
    async def bypass_node(
        instance_id: str,
        node_id: str,
        container: PlatformContainer = Depends(get_container),
        reason: str = "",
    ) -> dict[str, Any]:
        try:
            return await engine_of(container).bypass_node(instance_id, node_id, reason=reason)
        except WorkflowValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/instances/{instance_id}/abort")
    async def abort_instance(instance_id: str, container: PlatformContainer = Depends(get_container), reason: str = "") -> dict[str, Any]:
        try:
            return await engine_of(container).abort(instance_id, reason=reason)
        except WorkflowValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    return router
