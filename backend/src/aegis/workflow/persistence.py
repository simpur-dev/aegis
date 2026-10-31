"""工作流持久化端口：定义写穿与实例检查点的边界（P1-5）。

引擎与仓储只依赖本模块的协议，不导入 `aegis.persistence`：
PostgreSQL 实现放在 `aegis.persistence.workflow_store`，由装配层注入（同 ADR-0003
"读路径留内存、写路径投缓冲"的口径）。没有持久层时传 `None`，引擎按纯内存运行。

实例检查点刻意是**快照**而不是增量：实例可能在运行中被人改图（插节点/改参数），
定义仓储里的那一版已经不能代表现场，所以 `runtime.definition` 存的是实例自己的图。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from aegis.workflow.model import WorkflowDef, WorkflowInstance


class WorkflowRuntime(BaseModel):
    """实例行里除节点状态之外的现场：定义快照 + 等待点 + 决策 + 画布位置。

    定义快照必须随实例走：在途实例允许插节点/改参数，恢复时按定义仓储重建会
    把"这张图实际长什么样"换成模板的旧样子。
    """

    model_config = ConfigDict(extra="forbid")

    definition: WorkflowDef
    lineage_id: str = ""
    decisions: dict[str, dict[str, Any]] = Field(default_factory=dict)
    pending_waits: dict[str, str] = Field(default_factory=dict)
    pending_options: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    positions: dict[str, dict[str, float]] = Field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class InstanceCheckpoint:
    """一次落库的完整现场：关系化的实例/节点状态 + 运行态快照。"""

    instance: WorkflowInstance
    runtime: WorkflowRuntime


class WorkflowPersistencePort(Protocol):
    """写侧非阻塞（实现可投缓冲），读侧只在启动恢复时调用。"""

    def save_definition(self, definition: WorkflowDef) -> None: ...

    def save_instance(self, checkpoint: InstanceCheckpoint) -> None: ...

    async def load_definitions(self, *, limit: int) -> list[WorkflowDef]: ...

    async def load_open_instances(self, *, limit: int) -> list[InstanceCheckpoint]: ...


__all__ = ["InstanceCheckpoint", "WorkflowPersistencePort", "WorkflowRuntime"]
