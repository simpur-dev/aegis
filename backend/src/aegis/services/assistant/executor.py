"""统一执行器：一次动作请求只走这一条路。

为什么要有它：旧实现把"解析参数、查依赖、判权限、执行、拒绝留痕"散在 `respond()` 的四五个
分支里，新增一个动作要在四处加 if。现在每一步只有一种失败语义、一个错误码（见 `_ERROR_STATUS`），
动作自己只管 `execute()`。

写动作的**确认与落库不在这里**：执行器只回答"这个动作现在能不能直接跑"（`needs_confirm`），
把"用内存 pending 还是 control_actions"留给编排层——P1 仍是内存，P2 接库时只改编排层。
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from aegis.domain.operator import role_at_least
from aegis.services.assistant.base import (
    Action,
    ActionContext,
    ActionError,
    ActionResult,
    ImpactPreview,
    ResolvedTargets,
)
from aegis.storage.store import SupportsControlActions

#: 错误码 → HTTP 状态。只在这里映射一次，路由层不再各自 if/elif。
ERROR_STATUS: dict[str, int] = {
    "E_ACTION_NOT_FOUND": 404,
    "E_ACTION_NOT_ALLOWED": 403,
    "E_ACTION_PARAM_INVALID": 422,
    "E_ACTION_CONFLICT": 409,
    "E_ACTION_EXPIRED": 410,
    "E_ACTION_RATE_LIMITED": 429,
    "E_TARGET_NOT_FOUND": 404,
    "E_TARGET_AMBIGUOUS": 409,
    "E_OPERATOR_REQUIRED": 401,
    "E_OPERATOR_INVALID": 403,
    "E_ACTION_EXECUTION_FAILED": 502,
    "E_ACTION_NOT_REVERSIBLE": 409,
    "E_DRILL_UNAVAILABLE": 503,
    "E_ALERTS_UNAVAILABLE": 503,
}


@dataclass(frozen=True, slots=True)
class ExecutionOutcome:
    """一次执行的结果。`status` 只有两种：待确认与已执行，是编排层唯一要分叉的地方。"""

    action: str
    status: str
    targets: ResolvedTargets
    preview: ImpactPreview | None = None
    result: ActionResult | None = None
    latency_ms: float = 0.0
    #: 待确认时后端给出的动作号与有效期；已执行时为空。
    action_id: str | None = None
    expires_in_seconds: int = 0
    #: 幂等键命中：返回的是首次那条记录，副作用没有重复发生。
    replayed: bool = False


class Executor:
    """八步管线里与"业务无关"的那几步。

    `repo` 是动作台账（`control_actions`）。它缺席时**只有读动作能跑**：写动作必须跨重启存活，
    内存 pending 做不到这一点，所以宁可明确拒绝，也不退回"重启就丢"的老路。
    """

    def __init__(self, *, repo: SupportsControlActions | None = None, confirm_ttl_seconds: int = 300) -> None:
        self._repo = repo
        # 下限压到 1 秒：生产默认 300，测试要能在秒级验证"过期确认不生效"
        self._ttl = max(1, int(confirm_ttl_seconds))

    async def run(self, action: Action, raw: dict[str, Any], ctx: ActionContext) -> ExecutionOutcome:
        started = time.perf_counter()
        params = self._parse(action, raw)
        targets = await action.resolve(params, ctx)
        self._authorize(action, ctx)
        preview = await action.preview(targets, params, ctx) if action.spec.previewable else ImpactPreview(targets=targets.items)
        if action.spec.needs_confirm and not ctx.approved:
            return await self._gate(action, params, targets, preview, ctx, started)
        result = await self._execute(action, targets, params, ctx)
        return ExecutionOutcome(
            action=action.spec.name,
            status="executed",
            targets=targets,
            preview=preview,
            result=result,
            latency_ms=(time.perf_counter() - started) * 1000.0,
        )

    async def resume(self, action: Action, ctx: ActionContext, action_id: str) -> ExecutionOutcome:
        """确认后的执行：原子迁移 `proposed → executing`，再跑动作、落结果。"""
        started = time.perf_counter()
        repo = self._require_repo()
        record = await repo.confirm_action(action_id)
        if record is None:
            raise ActionError("E_ACTION_CONFLICT", "动作已被处置、已过期或不存在，本次确认没有生效")
        params = self._parse(action, dict(record.get("resolved_args") or {}))
        self._authorize(action, ctx)
        targets = await action.resolve(params, ctx)
        try:
            result = await self._execute(action, targets, params, ctx)
        except ActionError as exc:
            await repo.mark_action_failed(action_id, {"code": exc.code, "message": exc.message})
            raise
        await repo.mark_action_executed(action_id, {"summary": result.summary, **result.data})
        return ExecutionOutcome(
            action=action.spec.name,
            status="executed",
            targets=targets,
            result=result,
            latency_ms=(time.perf_counter() - started) * 1000.0,
            action_id=action_id,
        )

    async def _gate(
        self,
        action: Action,
        params: Any,
        targets: ResolvedTargets,
        preview: ImpactPreview,
        ctx: ActionContext,
        started: float,
    ) -> ExecutionOutcome:
        repo = self._require_repo()
        operator = ctx.operator
        if operator is None:
            raise ActionError("E_OPERATOR_REQUIRED", f"「{action.spec.title}」需要有效席位（X-Aegis-Operator）")
        record, replayed = await repo.create_action(
            action_id=f"act_{secrets.token_hex(8)}",
            operator_id=operator.operator_id,
            action=action.spec.name,
            space_id=ctx.space_id,
            utterance=ctx.utterance,
            resolved_args=params.model_dump(mode="json"),
            impact_scope=preview.as_dict(),
            expires_in_seconds=self._ttl,
            idempotency_key=ctx.idempotency_key,
        )
        return ExecutionOutcome(
            action=action.spec.name,
            status="needs_confirm",
            targets=targets,
            preview=preview,
            latency_ms=(time.perf_counter() - started) * 1000.0,
            action_id=str(record["action_id"]),
            expires_in_seconds=self._ttl,
            replayed=replayed,
        )

    def _require_repo(self) -> SupportsControlActions:
        if self._repo is None:
            raise ActionError(
                "E_ACTION_UNAVAILABLE",
                "当前形态没有动作台账（control_actions）：写动作需要持久层，内存形态下不可用",
            )
        return self._repo

    @staticmethod
    def _parse(action: Action, raw: dict[str, Any]) -> Any:
        try:
            return action.spec.params.model_validate(raw)
        except ValidationError as exc:
            raise ActionError(
                "E_ACTION_PARAM_INVALID",
                f"「{action.spec.title}」参数不合法",
                detail={"errors": exc.errors(include_url=False)[:6]},
            ) from exc

    @staticmethod
    def _authorize(action: Action, ctx: ActionContext) -> None:
        spec = action.spec
        if ctx.operator is None:
            if spec.writes:
                raise ActionError("E_OPERATOR_REQUIRED", f"「{spec.title}」需要有效席位（X-Aegis-Operator）")
            return
        if not role_at_least(ctx.operator.role, spec.required_role):
            raise ActionError(
                "E_ACTION_NOT_ALLOWED",
                f"「{spec.title}」需要 {spec.required_role.value} 及以上角色，当前是 {ctx.operator.role.value}",
            )

    @staticmethod
    async def _execute(action: Action, targets: ResolvedTargets, params: Any, ctx: ActionContext) -> ActionResult:
        try:
            return await action.execute(targets, params, ctx)
        except ActionError:
            raise
        except Exception as exc:
            raise ActionError(
                "E_ACTION_EXECUTION_FAILED",
                f"{type(exc).__name__}: {str(exc)[:200]}",
            ) from exc
