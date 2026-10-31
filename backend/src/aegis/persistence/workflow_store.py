"""`WorkflowPersistencePort` 的 PostgreSQL 实现：定义写穿 + 实例检查点投递。

放在持久层而不是工作流层：协议由 `aegis.workflow.persistence` 定义，实现只在这里
组装 `rows` 映射与 `PostgresStore` 的有界写缓冲，引擎对 asyncpg 一无所知。
写侧不 await 落库——库抖时链路照跑，失败由写缓冲计数并在 `/api/v1/integrations`
的 store 行里可见；读侧只在启动恢复时调用，池必须先建好。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from aegis.persistence import rows
from aegis.persistence.postgres import KIND_WORKFLOW_DEF, KIND_WORKFLOW_INSTANCE

if TYPE_CHECKING:
    from aegis.persistence.postgres import PostgresStore
    from aegis.workflow.model import WorkflowDef
    from aegis.workflow.persistence import InstanceCheckpoint


class PostgresWorkflowPersistence:
    def __init__(self, store: PostgresStore) -> None:
        self._store = store

    def save_definition(self, definition: WorkflowDef) -> None:
        self._store.enqueue_mapped(KIND_WORKFLOW_DEF, [definition], rows.workflow_def_params)

    def save_instance(self, checkpoint: InstanceCheckpoint) -> None:
        self._store.enqueue_mapped(KIND_WORKFLOW_INSTANCE, [checkpoint], rows.workflow_checkpoint_params)

    async def load_definitions(self, *, limit: int) -> list[WorkflowDef]:
        return await self._store.load_workflow_definitions(limit=limit)

    async def load_open_instances(self, *, limit: int) -> list[InstanceCheckpoint]:
        return await self._store.load_open_workflow_instances(limit=limit)


def build_workflow_persistence(durability: Any | None) -> PostgresWorkflowPersistence | None:
    """按存储装配事实决定是否启用写穿；内存后端返回 None，引擎保持纯内存语义。"""
    if durability is None:
        return None
    return PostgresWorkflowPersistence(durability)


__all__ = ["PostgresWorkflowPersistence", "build_workflow_persistence"]
