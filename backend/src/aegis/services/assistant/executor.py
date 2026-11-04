"""统一执行器：一次动作请求只走这一条路。

为什么要有它：旧实现把"解析参数、查依赖、判权限、执行、拒绝留痕"散在 `respond()` 的四五个
分支里，新增一个动作要在四处加 if。现在每一步只有一种失败语义、一个错误码（见 `_ERROR_STATUS`），
动作自己只管 `execute()`。

写动作的**确认与落库不在这里**：执行器只回答"这个动作现在能不能直接跑"（`needs_confirm`），
把"用内存 pending 还是 control_actions"留给编排层——P1 仍是内存，P2 接库时只改编排层。
"""

from __future__ import annotations

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
}


@dataclass(frozen=True, slots=True)
class ExecutionOutcome:
    """一次执行的结果。`status` 只有两种，因为"待确认"与"已执行"是编排层唯一要分叉的地方。"""

    action: str
    status: str
    targets: ResolvedTargets
    preview: ImpactPreview | None = None
    result: ActionResult | None = None
    latency_ms: float = 0.0

    @property
    def needs_confirm(self) -> bool:
        return self.status == "needs_confirm"


class Executor:
    """八步管线里与"业务无关"的那几步。"""

    async def run(self, action: Action, raw: dict[str, Any], ctx: ActionContext) -> ExecutionOutcome:
        started = time.perf_counter()
        params = self._parse(action, raw)
        targets = await action.resolve(params, ctx)
        self._authorize(action, ctx)
        preview = await action.preview(targets, params, ctx) if action.spec.previewable else ImpactPreview(targets=targets.items)
        if action.spec.needs_confirm and not ctx.approved:
            return ExecutionOutcome(
                action=action.spec.name,
                status="needs_confirm",
                targets=targets,
                preview=preview,
                latency_ms=(time.perf_counter() - started) * 1000.0,
            )
        result = await self._execute(action, targets, params, ctx)
        return ExecutionOutcome(
            action=action.spec.name,
            status="executed",
            targets=targets,
            preview=preview,
            result=result,
            latency_ms=(time.perf_counter() - started) * 1000.0,
        )

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
