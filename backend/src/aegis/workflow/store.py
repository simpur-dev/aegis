"""工作流存储：定义（带版本、不可变）与实例运行态。

定义与实例分离是"柔性"的落点：修订模板产生新版本，在途实例继续按创建时快照执行。
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Sequence
from typing import Any

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

    同一 name 的每次修订产生新版本与新 workflow_id，旧版本按 workflow_id 仍可读；
    `_latest` 记录每个 name 的最新版本，`_index` 记录 (name, version) → workflow_id 用于冲突检测。
    """

    def __init__(self, capacity: int = 2_000) -> None:
        if capacity <= 0:
            raise ValueError("capacity 必须为正")
        self._by_id: dict[str, WorkflowDef] = {}
        self._versions: dict[str, list[int]] = {}
        self._index: dict[tuple[str, int], str] = {}  # (name, version) → workflow_id
        self._latest: dict[str, str] = {}  # name → 最新版本的 workflow_id
        self._capacity = capacity
        self._lock = asyncio.Lock()

    async def save(self, definition: WorkflowDef) -> WorkflowDef:
        async with self._lock:
            key = (definition.name, definition.version)
            holder = self._index.get(key)
            if holder is not None and holder != definition.workflow_id:
                raise DuplicateWorkflowName(f"同名同版本冲突: {definition.name} v{definition.version}")
            if len(self._by_id) >= self._capacity and definition.workflow_id not in self._by_id:
                raise RuntimeError(f"定义仓储已达容量上限 {self._capacity}")
            self._by_id[definition.workflow_id] = definition
            self._index[key] = definition.workflow_id
            versions = self._versions.setdefault(definition.name, [])
            if definition.version not in versions:
                versions.append(definition.version)
                versions.sort()
            if definition.version == versions[-1]:
                self._latest[definition.name] = definition.workflow_id
            return definition

    def get(self, workflow_id: str) -> WorkflowDef | None:
        return self._by_id.get(workflow_id)

    def latest(self, name: str) -> WorkflowDef | None:
        workflow_id = self._latest.get(name)
        if workflow_id is None:
            return None
        return self._by_id.get(workflow_id)

    def versions(self, name: str) -> list[int]:
        return list(self._versions.get(name, []))

    def list(self, *, include_archived: bool = False) -> list[WorkflowDef]:
        rows = sorted(self._by_id.values(), key=lambda d: (d.name, d.version))
        return rows if include_archived else [d for d in rows if d.status == "active"]

    def next_version(self, name: str) -> int:
        versions = self._versions.get(name) or [0]
        return max(versions) + 1

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
) -> WorkflowDef:
    return WorkflowDef(
        workflow_id=workflow_id or new_workflow_id(),
        name=name,
        description=description,
        version=version,
        nodes=[node if isinstance(node, NodeDef) else NodeDef.model_validate(node) for node in nodes],
        edges=[edge if isinstance(edge, EdgeDef) else EdgeDef.model_validate(edge) for edge in edges],
        created_by=created_by,
    )
