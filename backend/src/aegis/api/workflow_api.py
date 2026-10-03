"""工作流引擎 HTTP 接口（编排台与自动化调度的入口）。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from aegis.container import PlatformContainer
from aegis.domain.messages import new_trace_id
from aegis.workflow.engine import WorkflowEngine, WorkflowValidationError
from aegis.workflow.model import RetryPolicy


def get_container(request: Request) -> PlatformContainer:
    container: PlatformContainer | None = getattr(request.app.state, "container", None)
    if container is None:
        raise HTTPException(status_code=503, detail="平台尚未就绪")
    return container


class NodeInput(BaseModel):
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


class DefinitionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=2, max_length=64)
    description: str = Field(default="", max_length=512)
    nodes: list[NodeInput] = Field(min_length=1, max_length=64)
    edges: list[EdgeInput] = Field(default_factory=list, max_length=256)


class ReviseInput(BaseModel):
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


class ConfigPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    config: dict[str, Any]


class InsertNodeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    after: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,31}$")
    node: NodeInput


def build_router() -> APIRouter:
    router = APIRouter(prefix="/api/v1/workflow", tags=["workflow"])

    def engine_of(container: PlatformContainer) -> WorkflowEngine:
        return container.workflow

    @router.get("/node-types")
    async def node_types(container: PlatformContainer = Depends(get_container)) -> dict[str, Any]:
        engine = engine_of(container)
        registry = engine.node_registry
        return {
            "count": len(registry.names()),
            "items": [
                {
                    "type": spec.type_name,
                    "description": spec.description,
                    "required_config": list(spec.required_config),
                    "optional_config": list(spec.optional_config),
                }
                for spec in (registry.get(name) for name in registry.names())
                if spec is not None
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
                    "name": row.name,
                    "version": row.version,
                    "status": row.status,
                    "description": row.description,
                    "node_count": len(row.nodes),
                    "edge_count": len(row.edges),
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
        try:
            return await engine_of(container).resume(
                instance_id,
                node_id=node_id,
                decision={"choice": payload.choice, "by": payload.by, "comment": payload.comment},
            )
        except WorkflowValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

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
