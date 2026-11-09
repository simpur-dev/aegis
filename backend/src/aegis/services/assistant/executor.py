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
from aegis.services.assistant.audit import AuditTrail
from aegis.services.assistant.base import (
    Action,
    ActionContext,
    ActionError,
    ActionResult,
    ImpactPreview,
    ResolvedTargets,
)
from aegis.services.assistant.guard import MAX_TARGETS, FailureBreaker
from aegis.services.assistant.review import ReviewPolicy
from aegis.storage.store import SupportsControlActions

#: 错误码 → HTTP 状态。只在这里映射一次，路由层不再各自 if/elif。
ERROR_STATUS: dict[str, int] = {
    "E_ACTION_NOT_FOUND": 404,
    "E_ACTION_NOT_ALLOWED": 403,
    "E_ACTION_PARAM_INVALID": 422,
    "E_ACTION_CONFLICT": 409,
    "E_ACTION_EXPIRED": 410,
    "E_ACTION_REVIEW_FORBIDDEN": 403,
    "E_ACTION_DUPLICATE_REVIEW": 409,
    "E_ACTION_RATE_LIMITED": 429,
    "E_ACTION_TOO_MANY_TARGETS": 422,
    "E_TARGET_NOT_FOUND": 404,
    "E_TARGET_AMBIGUOUS": 409,
    "E_OPERATOR_REQUIRED": 401,
    "E_OPERATOR_INVALID": 403,
    "E_ACTION_EXECUTION_FAILED": 502,
    "E_ACTION_NOT_REVERSIBLE": 409,
    "E_DRILL_UNAVAILABLE": 503,
    "E_ALERTS_UNAVAILABLE": 503,
    "E_ACTION_UNAVAILABLE": 503,
    "E_RULE_LEDGER_UNAVAILABLE": 503,
    "E_DEVICE_UNAVAILABLE": 503,
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
    #: 高安全档位下这条提案要几票（0 = 本人一次确认）；前端据此显示复核进度。
    review_required: int = 0
    reviews: tuple[dict[str, Any], ...] = ()


class Executor:
    """八步管线里与"业务无关"的那几步。

    `repo` 是动作台账（`control_actions`）。它缺席时**只有读动作能跑**：写动作必须跨重启存活，
    内存 pending 做不到这一点，所以宁可明确拒绝，也不退回"重启就丢"的老路。
    """

    def __init__(
        self,
        *,
        repo: SupportsControlActions | None = None,
        confirm_ttl_seconds: int = 300,
        review_policy: ReviewPolicy | None = None,
        breaker: FailureBreaker | None = None,
    ) -> None:
        self._repo = repo
        # 下限压到 1 秒：生产默认 300，测试要能在秒级验证"过期确认不生效"
        self._ttl = max(1, int(confirm_ttl_seconds))
        self._reviews = review_policy or ReviewPolicy(enabled=False)
        # 连续失败熔断：按动作名记，成功一次即清零（设计 §4.8）
        self._breaker = breaker or FailureBreaker()
        # 审计（第 8 步）：与动作台账同一个 repo——audit_ops 与 control_actions 同库同事务才有意义
        self._audit = AuditTrail(repo)

    async def run(self, action: Action, raw: dict[str, Any], ctx: ActionContext) -> ExecutionOutcome:
        started = time.perf_counter()
        params = self._parse(action, raw)
        targets = await action.resolve(params, ctx)
        try:
            self._authorize(action, ctx)
            # 批量上限与熔断都在提案/确认之前判，且放在 try 内：写动作被这两条拦下也要留审计行
            self._enforce_targets(action, targets)
            self._ensure_closed(action)
        except ActionError as exc:
            # 越权也留一行（§9：越权各一次）；匿名尝试记 operator_id=unknown。
            if action.spec.writes:
                await self._audit.write(
                    self._audit.rejected(action_id=None, action=action, operator=ctx.operator, error=exc, utterance=ctx.utterance)
                )
            raise
        preview = await action.preview(targets, params, ctx) if action.spec.previewable else ImpactPreview(targets=targets.items)
        if action.spec.needs_confirm and not ctx.approved:
            return await self._gate(action, params, targets, preview, ctx, started)
        if action.spec.writes:
            # 无确认闸的写动作（认领/回执/关闭/单发通知）也要过审计闸：没有台账就不执行，
            # 不存在"跑了但没记"的路径——这比 gated 动作的 require_repo 更进一步。
            return await self._run_audited(action, params, targets, ctx, started)
        result = await self._execute(action, targets, params, ctx)
        return ExecutionOutcome(
            action=action.spec.name,
            status="executed",
            targets=targets,
            preview=preview,
            result=result,
            latency_ms=(time.perf_counter() - started) * 1000.0,
        )

    async def _run_audited(
        self,
        action: Action,
        params: Any,
        targets: ResolvedTargets,
        ctx: ActionContext,
        started: float,
    ) -> ExecutionOutcome:
        """直接执行的写动作：intent 在执行前、result 在执行后（含失败），两条共用审计关联键。"""
        self._require_repo()
        audit_id = self._audit.new_action_id()
        await self._audit.write(self._audit.intent(action_id=audit_id, action=action, params=params, targets=targets, ctx=ctx))
        try:
            result = await self._execute(action, targets, params, ctx)
        except ActionError as exc:
            await self._audit.write(
                self._audit.result(
                    action_id=audit_id,
                    action=action,
                    params=params,
                    targets=targets,
                    ctx=ctx,
                    after={"rejected": exc.code, "message": exc.message[:200]},
                )
            )
            raise
        await self._audit.write(
            self._audit.result(
                action_id=audit_id,
                action=action,
                params=params,
                targets=targets,
                ctx=ctx,
                after={"summary": result.summary, **result.data},
            )
        )
        return ExecutionOutcome(
            action=action.spec.name,
            status="executed",
            targets=targets,
            result=result,
            latency_ms=(time.perf_counter() - started) * 1000.0,
        )

    async def resume(self, action: Action, ctx: ActionContext, action_id: str, *, preconfirmed: bool = False) -> ExecutionOutcome:
        """确认后的执行：先把参数/权限/目标核对完，再原子迁移 `proposed → executing`。

        顺序不能反过来：迁移一旦落库，这条动作就不再是 `proposed`——此后任何失败
        （参数在版本升级后不再合法、确认人角色不够、目标已被删）都会把它卡在 `executing`，
        既不能重试也不能撤销，只能等 10 分钟后的卡死回收。所以三步只读核对放在迁移前：
        迁移前失败，动作仍是 `proposed`，换个人还能再来；迁移后失败一律标记 `failed`。

        `preconfirmed=True` 用于双人复核的第二票：那一票已经在台账里把状态迁到 `executing`，
        这里不能再调一次 `confirm_action`（命中 0 行会被误报成"已过期"）。
        """
        started = time.perf_counter()
        repo = self._require_repo()
        record = await repo.get_action(action_id)
        if record is None:
            raise ActionError("E_ACTION_CONFLICT", "动作不存在或已被清理，本次确认没有生效")
        status = str(record.get("status") or "")
        if preconfirmed:
            if status != "executing":
                raise ActionError("E_ACTION_CONFLICT", "第二票已落库但动作不在 executing，结果未知")
        elif status == "expired":
            raise ActionError("E_ACTION_EXPIRED", "动作已过期，确认没有生效")
        elif status != "proposed":
            raise ActionError("E_ACTION_CONFLICT", "动作已被处置、已过期或不存在，本次确认没有生效")

        try:
            params = self._parse(action, dict(record.get("resolved_args") or {}))
            self._authorize(action, ctx)
            self._ensure_closed(action)
            targets = await action.resolve(params, ctx)
            self._enforce_targets(action, targets)
        except ActionError as exc:
            # 越权/参数/目标失败也要留痕：有台账时 intent 行已随提案落库，这里补一行 rejected 凑成对。
            await self._audit.write(
                self._audit.rejected(
                    action_id=action_id,
                    action=action,
                    operator=ctx.operator,
                    error=exc,
                    utterance=str(record.get("utterance") or ""),
                    space_id=record.get("space_id"),
                    resolved_args=dict(record.get("resolved_args") or {}),
                )
            )
            if preconfirmed:
                # 第二票已经把状态迁到 executing：不落终态就再也收不了尾
                await repo.mark_action_failed(action_id, {"code": exc.code, "message": exc.message})
            raise

        # 原子抢占仍在台账里：两个人同时确认，只有一个能从 proposed 迁走
        if not preconfirmed and await repo.confirm_action(action_id) is None:
            raise ActionError("E_ACTION_CONFLICT", "动作已被处置、已过期或不存在，本次确认没有生效")
        try:
            result = await self._execute(action, targets, params, ctx)
        except ActionError as exc:
            await repo.mark_action_failed(
                action_id,
                {"code": exc.code, "message": exc.message},
                audit=self._audit.result(
                    action_id=action_id,
                    action=action,
                    params=params,
                    targets=targets,
                    ctx=ctx,
                    after={"rejected": exc.code, "message": exc.message[:200]},
                ),
            )
            raise
        await repo.mark_action_executed(
            action_id,
            {"summary": result.summary, **result.data},
            audit=self._audit.result(
                action_id=action_id,
                action=action,
                params=params,
                targets=targets,
                ctx=ctx,
                after={"summary": result.summary, **result.data},
            ),
        )
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
        action_id = f"act_{secrets.token_hex(8)}"
        record, replayed = await repo.create_action(
            action_id=action_id,
            operator_id=operator.operator_id,
            action=action.spec.name,
            space_id=ctx.space_id,
            utterance=ctx.utterance,
            resolved_args=params.model_dump(mode="json"),
            impact_scope=preview.as_dict(),
            expires_in_seconds=self._ttl,
            idempotency_key=ctx.idempotency_key,
            review_required=self._reviews.votes_required(action.spec),
            # intent 审计行与 proposed 落库同事务；幂等重放时台账会丢弃这一行（首次已记）
            audit=self._audit.intent(
                action_id=action_id,
                action=action,
                params=params,
                targets=targets,
                ctx=ctx,
            ),
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
            review_required=int(record.get("review_required") or 0),
            reviews=tuple(record.get("reviewers") or ()),
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
    def _enforce_targets(action: Action, targets: ResolvedTargets) -> None:
        if len(targets.items) > MAX_TARGETS:
            raise ActionError(
                "E_ACTION_TOO_MANY_TARGETS",
                f"「{action.spec.title}」一次解析出 {len(targets.items)} 个对象，超过 {MAX_TARGETS} 个上限：请拆分后重试",
            )

    def _ensure_closed(self, action: Action) -> None:
        remaining = self._breaker.remaining(action.spec.name)
        if remaining > 0:
            raise ActionError(
                "E_ACTION_RATE_LIMITED",
                f"「{action.spec.title}」连续失败后已熔断，约 {remaining:.0f} 秒后恢复；读动作不受影响",
            )

    async def _execute(self, action: Action, targets: ResolvedTargets, params: Any, ctx: ActionContext) -> ActionResult:
        try:
            result = await action.execute(targets, params, ctx)
        except ActionError:
            # 业务拒绝是确定性的（目标不存在、参数不合法），不参与熔断——否则重试永远开不了门
            raise
        except Exception as exc:
            self._breaker.record_failure(action.spec.name)
            raise ActionError(
                "E_ACTION_EXECUTION_FAILED",
                f"{type(exc).__name__}: {str(exc)[:200]}",
            ) from exc
        self._breaker.record_success(action.spec.name)
        return result
