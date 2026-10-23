"""工作流文档（草稿/预览/发布的统一载荷）与静态校验。

`WorkflowDef` 是引擎输入，只描述图身份；`WorkflowDocument` 是编辑器载荷，额外带
lineage、名称、版式和 Start 变量。版式**不塞进** `WorkflowDef`：拖动一个节点不应该
改变引擎眼中的模板，但版式又要随版本可回溯，所以它属于文档快照（ADR-0009 §1）。

旧定义没有 Start 依然合法；只有存在 Start 时才要求它是唯一根且全图可达。
"""

from __future__ import annotations

import hashlib
from typing import Any, Literal

import orjson
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_core import PydanticCustomError

from aegis.domain.messages import now_iso
from aegis.workflow.model import EdgeDef, NodeDef, WorkflowDef, find_cycle

DOCUMENT_SCHEMA_VERSION = 1
MAX_DOCUMENT_NODES = 200
MAX_DOCUMENT_EDGES = 500
MAX_STICKY_NOTES = 32
MAX_START_VARIABLES = 32
MAX_NODE_CONFIG_BYTES = 64 * 1024
MAX_DOCUMENT_BYTES = 2 * 1024 * 1024
MAX_VARIABLE_REFS_PER_NODE = 32
MAX_VARIABLE_PATH_DEPTH = 8

_COORD = 1_000_000.0
_PROTOTYPE_SEGMENTS = frozenset({"__proto__", "prototype", "constructor"})
_NODE_ID_RE = r"^[a-z][a-z0-9_-]{0,31}$"
_LINEAGE_ID_RE = r"^wf_[0-9a-f]{12}$"


class StickyNote(BaseModel):
    """画布备注：不参与校验与执行，只随 layout 保存。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    note_id: str = Field(pattern=_NODE_ID_RE)
    x: float = Field(ge=-_COORD, le=_COORD)
    y: float = Field(ge=-_COORD, le=_COORD)
    width: float = Field(default=220, ge=120, le=800)
    height: float = Field(default=140, ge=80, le=800)
    text: str = Field(default="", max_length=4_000)
    color: Literal["amber", "blue", "green", "violet"] = "amber"


class Position(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    x: float = Field(ge=-_COORD, le=_COORD)
    y: float = Field(ge=-_COORD, le=_COORD)


class Viewport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    x: float = Field(ge=-_COORD, le=_COORD)
    y: float = Field(ge=-_COORD, le=_COORD)
    zoom: float = Field(default=1.0, ge=0.1, le=4.0)


class WorkflowLayout(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    positions: dict[str, Position] = Field(default_factory=dict)
    viewport: Viewport | None = None
    notes: list[StickyNote] = Field(default_factory=list, max_length=MAX_STICKY_NOTES)

    @field_validator("positions")
    @classmethod
    def _position_keys(cls, value: dict[str, Position]) -> dict[str, Position]:
        for node_id in value:
            if not node_id or len(node_id) > 32:
                raise ValueError(f"position 的 node_id 不合法: {node_id!r}")
        return value

    @model_validator(mode="after")
    def _unique_notes(self) -> WorkflowLayout:
        note_ids = [note.note_id for note in self.notes]
        if len(note_ids) != len(set(note_ids)):
            raise ValueError("备注 note_id 重复")
        return self


class StartVariable(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,31}$")
    label: str = Field(default="", max_length=64)
    type: Literal["string", "number", "boolean", "json"] = "string"
    required: bool = False
    default: Any = None

    @field_validator("name")
    @classmethod
    def _no_prototype_segments(cls, value: str) -> str:
        if value in _PROTOTYPE_SEGMENTS:
            raise ValueError(f"变量名不允许使用 {value!r}")
        return value

    @model_validator(mode="after")
    def _default_matches_type(self) -> StartVariable:
        default = self.default
        if default is None:
            return self
        if self.type == "string" and not isinstance(default, str):
            raise ValueError(f"变量 {self.name} 的 default 必须是字符串")
        if self.type == "number" and (not isinstance(default, (int, float)) or isinstance(default, bool)):
            raise ValueError(f"变量 {self.name} 的 default 必须是数字")
        if self.type == "boolean" and not isinstance(default, bool):
            raise ValueError(f"变量 {self.name} 的 default 必须是布尔值")
        if self.type == "json":
            try:
                orjson.dumps(default)
            except TypeError as exc:
                raise ValueError(f"变量 {self.name} 的 default 不是合法 JSON") from exc
        return self


def _json_size(value: Any) -> int:
    try:
        return len(orjson.dumps(value))
    except TypeError as exc:
        raise ValueError(f"内容必须是可 JSON 序列化的值: {exc}") from exc


def _reachable(start: str, edges: list[EdgeDef]) -> set[str]:
    adjacency: dict[str, list[str]] = {}
    for edge in edges:
        adjacency.setdefault(edge.source, []).append(edge.target)
    seen = {start}
    stack = [start]
    while stack:
        node = stack.pop()
        for child in adjacency.get(node, []):
            if child not in seen:
                seen.add(child)
                stack.append(child)
    return seen


def _start_variables(nodes: list[NodeDef]) -> list[StartVariable]:
    variables: list[StartVariable] = []
    for node in nodes:
        if node.type != "start":
            continue
        raw = node.config.get("variables", [])
        if not isinstance(raw, list):
            raise ValueError("start.variables 必须是数组")
        if len(raw) > MAX_START_VARIABLES:
            raise ValueError(f"start.variables 不得超过 {MAX_START_VARIABLES} 项，实际 {len(raw)}")
        for index, item in enumerate(raw):
            try:
                variables.append(StartVariable.model_validate(item))
            except ValidationError as exc:
                reason = exc.errors()[0].get("msg", str(exc)) if exc.errors() else str(exc)
                raise ValueError(f"start.variables[{index}] 不合法: {reason}") from exc
    names = [variable.name for variable in variables]
    if len(names) != len(set(names)):
        raise ValueError("start.variables 变量名重复")
    return variables


class WorkflowDocument(BaseModel):
    """草稿、预览与发布共用的只读快照；修改一律产出新对象。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = DOCUMENT_SCHEMA_VERSION
    lineage_id: str = Field(pattern=_LINEAGE_ID_RE)
    name: str = Field(min_length=1, max_length=64)
    description: str = Field(default="", max_length=512)
    nodes: list[NodeDef] = Field(min_length=1, max_length=MAX_DOCUMENT_NODES)
    edges: list[EdgeDef] = Field(default_factory=list, max_length=MAX_DOCUMENT_EDGES)
    layout: WorkflowLayout = Field(default_factory=WorkflowLayout)

    @field_validator("schema_version")
    @classmethod
    def _known_schema_version(cls, value: int) -> int:
        if value != DOCUMENT_SCHEMA_VERSION:
            raise PydanticCustomError(
                "invalid_schema_version",
                "不支持的文档 schema_version: {version}",
                {"version": value},
            )
        return value

    @model_validator(mode="before")
    @classmethod
    def _prune_unknown_positions(cls, data: Any) -> Any:
        """旧版式里残留已删除节点的坐标是常态，剪掉即可；保留它只会越存越大。

        必须放在 `before`：`after` 返回新实例在 `__init__` 路径上不被采用（Pydantic 会告警并丢弃）。
        """
        if not isinstance(data, dict):
            return data
        nodes = data.get("nodes") or []
        known = {node.get("node_id") if isinstance(node, dict) else getattr(node, "node_id", None) for node in nodes} - {None}
        layout = data.get("layout")
        if isinstance(layout, dict) and isinstance(layout.get("positions"), dict):
            positions = {node_id: value for node_id, value in layout["positions"].items() if node_id in known}
            if len(positions) != len(layout["positions"]):
                return {**data, "layout": {**layout, "positions": positions}}
        elif isinstance(layout, WorkflowLayout):
            positions = {node_id: value for node_id, value in layout.positions.items() if node_id in known}
            if len(positions) != len(layout.positions):
                return {
                    **data,
                    "layout": WorkflowLayout(positions=positions, viewport=layout.viewport, notes=layout.notes),
                }
        return data

    @model_validator(mode="after")
    def _document_is_valid(self) -> WorkflowDocument:
        ids = [node.node_id for node in self.nodes]
        if len(ids) != len(set(ids)):
            raise ValueError("节点 ID 重复")

        known = set(ids)
        for edge in self.edges:
            if edge.source not in known:
                raise ValueError(f"边的源节点不存在: {edge.source}")
            if edge.target not in known:
                raise ValueError(f"边的目标节点不存在: {edge.target}")
            if edge.source == edge.target:
                raise ValueError(f"不允许自环: {edge.source}")
        if (cycle := find_cycle(ids, self.edges)) is not None:
            raise ValueError(f"工作流图存在环: {' → '.join(cycle)}")

        starts = [node for node in self.nodes if node.type == "start"]
        ends = [node for node in self.nodes if node.type == "end"]
        if len(starts) > 1:
            raise ValueError("start 节点至多一个")
        if len(ends) > 1:
            raise ValueError("end 节点至多一个")
        for start in starts:
            if any(edge.target == start.node_id for edge in self.edges):
                raise ValueError(f"start 节点 {start.node_id} 不允许有入边")
        for end in ends:
            if any(edge.source == end.node_id for edge in self.edges):
                raise ValueError(f"end 节点 {end.node_id} 不允许有出边")

        if starts:
            start_id = starts[0].node_id
            roots = {node.node_id for node in self.nodes if not any(edge.target == node.node_id for edge in self.edges)}
            if roots != {start_id}:
                raise ValueError(f"存在 start 时它必须是唯一根节点，当前根节点: {sorted(roots)}")
            unreachable = sorted(known - _reachable(start_id, self.edges))
            if unreachable:
                raise ValueError(f"这些节点从 {start_id} 不可达: {unreachable}")

        _start_variables(self.nodes)

        for node in self.nodes:
            size = _json_size(node.config)
            if size > MAX_NODE_CONFIG_BYTES:
                raise ValueError(f"节点 {node.node_id} 的 config 超过 {MAX_NODE_CONFIG_BYTES} 字节（实际 {size}）")

        size = _json_size(self.model_dump(mode="json"))
        if size > MAX_DOCUMENT_BYTES:
            raise ValueError(f"文档序列化后超过 {MAX_DOCUMENT_BYTES} 字节（实际 {size}）")
        return self

    def start_variables(self) -> list[StartVariable]:
        return _start_variables(self.nodes)

    def node(self, node_id: str) -> NodeDef | None:
        return next((node for node in self.nodes if node.node_id == node_id), None)


class WorkflowDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    lineage_id: str = Field(pattern=_LINEAGE_ID_RE)
    #: 草稿基于哪个已发布版本；从未发布的新 lineage 为 None。
    base_workflow_id: str | None = None
    revision: int = Field(ge=1)
    document: WorkflowDocument
    updated_at: str = Field(default_factory=now_iso)

    @model_validator(mode="after")
    def _lineage_matches(self) -> WorkflowDraft:
        if self.document.lineage_id != self.lineage_id:
            raise ValueError("草稿 document.lineage_id 与草稿 lineage_id 不一致")
        return self


def canonical_payload(document: WorkflowDocument) -> dict[str, Any]:
    """内容身份的唯一喂料：键序由 `OPT_SORT_KEYS` 归一，不纳入 ID/版本/时间/lineage。"""
    return {
        "schema_version": document.schema_version,
        "name": document.name,
        "description": document.description,
        "nodes": [node.model_dump(mode="json") for node in document.nodes],
        "edges": [edge.model_dump(mode="json") for edge in document.edges],
        "layout": document.layout.model_dump(mode="json"),
    }


def canonical_bytes(document: WorkflowDocument) -> bytes:
    return orjson.dumps(canonical_payload(document), option=orjson.OPT_SORT_KEYS)


def document_hash(document: WorkflowDocument) -> str:
    return "sha256:" + hashlib.sha256(canonical_bytes(document)).hexdigest()


def document_from_definition(definition: WorkflowDef, *, layout: WorkflowLayout | None = None) -> WorkflowDocument:
    """把旧定义投影成文档：没有保存过 layout 的定义得到空版式，读取不报错。"""
    return WorkflowDocument(
        schema_version=DOCUMENT_SCHEMA_VERSION,
        lineage_id=definition.lineage_id or definition.workflow_id,
        name=definition.name,
        description=definition.description,
        nodes=[node.model_copy(deep=True) for node in definition.nodes],
        edges=[edge.model_copy(deep=True) for edge in definition.edges],
        layout=layout or WorkflowLayout(),
    )


def definition_from_document(
    document: WorkflowDocument,
    *,
    workflow_id: str,
    version: int = 1,
    created_by: str = "platform.workflow",
    created_at: str | None = None,
) -> WorkflowDef:
    return WorkflowDef(
        workflow_id=workflow_id,
        lineage_id=document.lineage_id,
        schema_version=document.schema_version,
        name=document.name,
        description=document.description,
        version=version,
        nodes=[node.model_copy(deep=True) for node in document.nodes],
        edges=[edge.model_copy(deep=True) for edge in document.edges],
        created_by=created_by,
        created_at=created_at if created_at is not None else now_iso(),
        content_hash=document_hash(document),
    )


__all__ = [
    "DOCUMENT_SCHEMA_VERSION",
    "MAX_DOCUMENT_BYTES",
    "MAX_DOCUMENT_EDGES",
    "MAX_DOCUMENT_NODES",
    "MAX_NODE_CONFIG_BYTES",
    "MAX_START_VARIABLES",
    "MAX_STICKY_NOTES",
    "MAX_VARIABLE_PATH_DEPTH",
    "MAX_VARIABLE_REFS_PER_NODE",
    "Position",
    "StartVariable",
    "StickyNote",
    "Viewport",
    "WorkflowDocument",
    "WorkflowDraft",
    "WorkflowLayout",
    "canonical_bytes",
    "canonical_payload",
    "definition_from_document",
    "document_from_definition",
    "document_hash",
]
