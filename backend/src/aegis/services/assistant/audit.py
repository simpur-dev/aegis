"""动作审计（执行器八步的第 8 步）的**单一出口**。

`audit_ops` 从 010 建表起一直是"有表没写入方"的空壳；本模块把它接上，并钉死三条口径：
- 行形状只有一份（`storage.store.AuditRecord`）：执行器、confirm/撤销与两套台账都认它，
  谁都不许绕开它另拼一份 INSERT 字段清单；
- 三种结局与设计 §9 的验收一一对应——**成功**：intent（执行前）+ result（执行后，`after`=结果）；
  **失败**：intent + result（`after`=错误/业务拒绝原因）；**越权**：只有一行 result
  （`after.rejected`=错误码），匿名尝试记 `operator_id='unknown'`（列不加 FK）；
- 与状态迁移**同事务**由台账实现保证（SQL 侧把审计行放进同一条事务，内存侧同语句追加），
  调用方不感知介质差别。

`intent`/`result` 只是行的构造器，真正落库走两条路：伴随迁移的行作为 `audit=` 参数传给
`create_action` / `mark_action_executed` / `mark_action_failed`；独立行（越权、直接执行的
写动作）经 `write()`。幂等重放不补审计行——重复提交不是新意图。
"""

from __future__ import annotations

import secrets
from typing import Any

from pydantic import BaseModel

from aegis.domain.operator import Operator
from aegis.services.assistant.base import Action, ActionContext, ResolvedTargets
from aegis.storage.store import AuditRecord, SupportsControlActions

#: 越权匿名的占位身份：`audit_ops.operator_id` 不加 FK，所以允许这个值（设计 §5.5）。
UNKNOWN = "unknown"


def _args_of(params: Any) -> dict[str, Any]:
    """解析后的参数模型 → JSON 形态；confirm 路径拿到的已是 dict，原样透传。"""
    if isinstance(params, BaseModel):
        return params.model_dump(mode="json")
    return dict(params or {})


def _target_of(targets: ResolvedTargets) -> str:
    return ",".join(targets.ids)[:500]


class AuditTrail:
    """持台账者才可写审计：台账缺席时 `available=False`，写动作在执行器里会被先拒。"""

    def __init__(self, repo: SupportsControlActions | None) -> None:
        self._repo = repo

    @property
    def available(self) -> bool:
        return self._repo is not None

    def new_action_id(self) -> str:
        """直接执行的写动作没有 `control_actions` 行，审计行自己生成关联键（intent/result 共用）。"""
        return f"act_{secrets.token_hex(8)}"

    def intent(
        self,
        *,
        action_id: str,
        action: Action,
        params: Any,
        targets: ResolvedTargets,
        ctx: ActionContext,
    ) -> AuditRecord:
        operator = ctx.operator
        return AuditRecord(
            phase="intent",
            action_id=action_id,
            operator_id=UNKNOWN if operator is None else operator.operator_id,
            role=UNKNOWN if operator is None else operator.role.value,
            action=action.spec.name,
            space_id=ctx.space_id,
            target=_target_of(targets),
            utterance=ctx.utterance,
            resolved_args=_args_of(params),
            idempotency_key=ctx.idempotency_key,
        )

    def result(
        self,
        *,
        action_id: str,
        action: Action,
        params: Any,
        targets: ResolvedTargets,
        ctx: ActionContext,
        after: dict[str, Any],
    ) -> AuditRecord:
        operator = ctx.operator
        return AuditRecord(
            phase="result",
            action_id=action_id,
            operator_id=UNKNOWN if operator is None else operator.operator_id,
            role=UNKNOWN if operator is None else operator.role.value,
            action=action.spec.name,
            space_id=ctx.space_id,
            target=_target_of(targets),
            utterance=ctx.utterance,
            resolved_args=_args_of(params),
            idempotency_key=ctx.idempotency_key,
            after=after,
        )

    def rejected(
        self,
        *,
        action_id: str | None,
        action: Action,
        operator: Operator | None,
        error: Any,
        utterance: str = "",
        space_id: str | None = None,
        resolved_args: dict[str, Any] | None = None,
    ) -> AuditRecord:
        """越权/资格被拒：只有一行 result，`after.rejected` 是给读审计的人看的失败原因。"""
        return AuditRecord(
            phase="result",
            action_id=action_id or self.new_action_id(),
            operator_id=UNKNOWN if operator is None else operator.operator_id,
            role=UNKNOWN if operator is None else operator.role.value,
            action=action.spec.name,
            space_id=space_id,
            utterance=utterance,
            resolved_args=dict(resolved_args or {}),
            after={"rejected": getattr(error, "code", type(error).__name__), "message": str(getattr(error, "message", error))[:200]},
        )

    async def write(self, record: AuditRecord) -> None:
        """独立审计行。台账缺席时静默跳过——调用方在写动作路径上已先行拒绝，这里不再二次报错。"""
        if self._repo is None:
            return
        await self._repo.record_audit(record)
