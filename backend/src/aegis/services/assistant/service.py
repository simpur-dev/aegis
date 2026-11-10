"""对话编排：把一句话组织成一串可解释的帧。

这一层只做四件事：开会话、判意图、调执行器、把结果说成人话。它不实现任何动作，也不碰业务表
（演练/上报由动作自己调既有服务）。写动作的"待确认 → 执行"落 `control_actions`：
执行器负责状态迁移，这一层只负责把它翻译成帧与回执。
"""

from __future__ import annotations

import asyncio
import secrets
import time
from collections import deque
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Any

from aegis.config import Settings, get_settings
from aegis.domain.operator import Operator, OperatorRole, role_at_least
from aegis.observability.tracer import Tracer
from aegis.services.assistant.audit import AuditTrail
from aegis.services.assistant.base import Action, ActionContext, ActionError, ResolvedTargets
from aegis.services.assistant.executor import Executor
from aegis.services.assistant.guard import WriteRateLimiter
from aegis.services.assistant.intent import (
    Intent,
    IntentClassifier,
    bulk_scope_without_target,
    out_of_scope_action,
    resolve_coreference,
)
from aegis.services.assistant.narrate import narrate_result
from aegis.services.assistant.registry import ActionRegistry
from aegis.services.assistant.review import DualReview, ReviewPolicy
from aegis.services.interpret.text_guard import invented_numbers
from aegis.services.operator_auth import OperatorKeyError
from aegis.storage.store import SupportsControlActions

_MAX_SESSIONS = 200
_MAX_TURNS = 20
_ANSWER_LIMIT = 4_000


@dataclass(frozen=True, slots=True)
class AssistantEvent:
    """一帧 SSE 事件。`type` 是前端渲染的分支键，`data` 里只放可直接外显的事实。"""

    type: str
    data: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {"type": self.type, **self.data}


@dataclass(slots=True)
class AssistantSession:
    session_id: str
    trace_id: str
    updated_at: float
    history: deque[dict[str, str]] = field(default_factory=lambda: deque(maxlen=_MAX_TURNS * 2))


class AssistantService:
    """语义交互服务本体：纯 asyncio，所有外部能力经注册表里的动作注入。"""

    def __init__(
        self,
        *,
        registry: ActionRegistry,
        llm: Any | None = None,
        settings: Settings | None = None,
        tracer: Tracer | None = None,
        control_actions: SupportsControlActions | None = None,
        control_actions_persistent: bool = False,
        operator_auth: Any | None = None,
    ) -> None:
        self._registry = registry
        self._classifier = IntentClassifier(registry, llm=llm)
        self._llm = llm
        self._settings = settings or get_settings()
        self._ttl = float(self._settings.assistant_session_ttl_seconds)
        self._repo = control_actions
        self._persistent = control_actions_persistent
        # 双人复核是配置档位：默认关。打开时执行器按 spec 决定提案要几票，
        # 确认路由按同一份 policy 校验投票资格——两份清单不各写一遍。
        self._review_policy = ReviewPolicy(enabled=self._settings.assistant_dual_review_enabled)
        self._dual_review = DualReview(control_actions, self._review_policy) if control_actions is not None else None
        # P4 二次认证（设计 §14.1.3）：与双人复核正交的另一道门——同一席位再出示一次令牌。
        # 默认关（`assistant_step_up_auth_enabled`），关着时 D5「指令即执行」逐字不变。
        self._step_up_enabled = bool(getattr(self._settings, "assistant_step_up_auth_enabled", False))
        self._operator_auth = operator_auth
        # 审计（第 8 步）与动作台账同一个 repo；confirm/撤销的审计行也从这条出口走
        self._audit = AuditTrail(control_actions)
        # 动作台账缺席时只有读动作能跑：写动作必须跨重启存活，内存 pending 做不到
        self._executor = Executor(
            repo=control_actions,
            confirm_ttl_seconds=int(self._settings.assistant_confirm_ttl_seconds),
            review_policy=self._review_policy,
        )
        # 单会话写动作频控（设计 §4.8）：读动作不占额度，写动作 10 次/分钟
        self._writes = WriteRateLimiter()
        self._tracer = tracer or Tracer()
        self._sessions: dict[str, AssistantSession] = {}
        self._order: deque[str] = deque(maxlen=_MAX_SESSIONS)
        self.rejections: deque[dict[str, Any]] = deque(maxlen=200)
        self.replies = 0

    # ---------- 对外面 ----------

    @property
    def llm_available(self) -> bool:
        return self._classifier.llm_available

    def capabilities(self, role: Any | None = None) -> dict[str, Any]:
        return {
            "llm_configured": self.llm_available,
            "actions": self._registry.capabilities(role),
            "session_ttl_seconds": self._ttl,
            "confirm_ttl_seconds": int(self._settings.assistant_confirm_ttl_seconds),
            # 内存形态也能跑写动作，但重启即失：如实标注，前端据此提示"演示形态"
            "control_actions_persistent": self._persistent,
            # 配置面只报"开没开"，不报"谁能复核"：动作清单里每项自带 dual_review 标记，
            # 两处合起来才是完整事实，前端不需要再抄一份角色表。
            "dual_review_enabled": self._review_policy.enabled,
        }

    async def respond(
        self,
        text: str,
        *,
        session_id: str | None = None,
        operator: Operator | None = None,
        reporter: str | None = None,
        region_code: str | None = None,
        idempotency_key: str | None = None,
        identity_kind: str = "declared",
    ) -> AsyncGenerator[AssistantEvent, None]:
        started = time.perf_counter()
        body = str(text or "").strip()[:2_000]
        session = self._touch(session_id)
        yield AssistantEvent(
            "meta",
            {
                "session_id": session.session_id,
                "llm_configured": self.llm_available,
                "operator_id": None if operator is None else operator.operator_id,
                "actions": list(self._registry.names),
            },
        )
        if not body:
            yield AssistantEvent("error", {"message": "消息为空"})
            return

        # 越权闸：两种指令都不能进入意图解析——① 动作还没开放；② 动作已开放，但这句话要的是
        # 整体作用域的批量操作，而助手只做操作者本来能做的**单条**处置，不放大权限。
        violation = out_of_scope_action(body)
        if violation is not None:
            action_name, verb = violation
            unregistered = self._registry.get(action_name) is None
            bulk = bulk_scope_without_target(body)
            if unregistered or bulk:
                reason = (
                    f"指令要求「{verb}」，该动作当前未开放"
                    if unregistered
                    else f"指令要求「{verb}」作用于整体范围，超出单条处置能力；请点名具体编号"
                )
                code = "E_ACTION_NOT_OPEN" if unregistered else "E_ACTION_BULK_SCOPE"
                self._reject(session, action=action_name, reason=reason, detail={"code": code})
                # 聊天级拒绝也要落 audit_ops：内存 deque 重启即失，越权尝试不能只活在进程里。
                if self._audit.available:
                    await self._audit.write(
                        self._audit.rejected(
                            action_id=None,
                            action=self._registry.get(action_name) or action_name,
                            operator=operator,
                            error=ActionError(code, reason),
                            utterance=body,
                        )
                    )
                yield AssistantEvent(
                    "rejected",
                    {
                        "instruction": verb,
                        "action": action_name,
                        "reason": (
                            "该动作当前未开放；可以做的动作见 GET /assistant/capabilities"
                            if unregistered
                            else "整体范围的批量指令不在助手能力内；请点名一条预警编号（wrn_…）"
                        ),
                        "session_id": session.session_id,
                    },
                )
                yield AssistantEvent(
                    "answer",
                    {
                        "text": (
                            "该指令涉及的动作尚未开放，已留痕。"
                            if unregistered
                            else "该指令要整体批量操作，助手只能按编号逐条处置，已留痕。"
                        )
                    },
                )
                return

        # 多轮指代消解在分类前："撤回它"会补上上一轮的编号；utterance 仍保留用户原始输入。
        effective_text = resolve_coreference(body, session.history)
        intent = await self._classifier.classify_async(effective_text, region_code=region_code, reporter=reporter)
        yield AssistantEvent("intent", intent.as_dict())
        session.history.append({"role": "user", "text": body[:500]})

        answer_parts: list[str] = []
        async for event in self._run_intent(
            session,
            intent,
            body=body,
            operator=operator,
            reporter=reporter,
            region_code=region_code,
            idempotency_key=idempotency_key,
            identity_kind=identity_kind,
        ):
            if event.type == "answer":
                answer_parts.append(str(event.data.get("text", "")))
            yield event

        session.history.append({"role": "assistant", "text": " ".join(answer_parts)[:500]})
        self.replies += 1
        self._tracer.record("assistant_reply_ms", max((time.perf_counter() - started) * 1000.0, 0.0), trace_id=session.trace_id)
        yield AssistantEvent(
            "done",
            {
                "session_id": session.session_id,
                "latency_ms": round((time.perf_counter() - started) * 1000.0, 3),
                "rejected_count": len(self.rejections),
            },
        )

    async def confirm(
        self,
        *,
        action_id: str,
        actor: str | None = None,
        operator: Operator | None = None,
        operator_key: str | None = None,
    ) -> dict[str, Any]:
        """确认执行：台账里 `proposed → executing → executed/failed`，并发下只能成功一次。

        双人复核打开时多一条支路：第一票只记票、不执行；第二票把状态迁到 `executing`
        后才走同一份执行器。两条支路共用下面这唯一的 `resume`，不复制执行逻辑。

        P4 二次认证（设计 §14.1.3）：高危动作（`spec.dual_review=True` 那 10 个）在
        `step_up_auth_enabled` 打开时要**本人再出示一次令牌**。它与双人复核正交——
        复核是"两个不同席位各投一票"（横向），二次认证是"本人再亮一次钥匙"（纵向）。
        校验**先于复核票**：认证没过就落票，同一个人第二次带对令牌来时会被判"已经投过"，
        第二票更是会把状态直接迁到 `executing` 再卡住——失败不消耗复核票的前提是票还没落。
        """
        if self._repo is None:
            return {"status": "rejected", "reason": "当前形态没有动作台账（control_actions），写动作不可用", "action_id": action_id}
        record = await self._repo.get_action(action_id)
        if record is None:
            self._reject(None, action=action_id, reason="动作不存在或已过期")
            return {"status": "rejected", "reason": "动作不存在或已过期", "action_id": action_id}
        action = self._registry.get(str(record["action"]))
        if action is None:
            return {"status": "rejected", "reason": f"动作 {record['action']} 不在注册表内", "action_id": action_id}
        ctx = ActionContext(
            session_id="",
            utterance=str(record.get("utterance") or ""),
            operator=operator,
            space_id=record.get("space_id"),
            approved=True,
        )
        # 二次认证先于记票：认证失败时不落票、不迁状态，否则同一个人再也点不动第二次。
        step_up = await self._step_up(action, operator, operator_key, record)
        if step_up is not None:
            return step_up
        preconfirmed = await self._vote_if_required(action_id, action, record, operator)
        if isinstance(preconfirmed, dict):
            return preconfirmed
        try:
            outcome = await self._executor.resume(action, ctx, action_id, preconfirmed=preconfirmed)
        except ActionError as exc:
            self._reject(None, action=action.spec.name, reason=exc.message, detail={"code": exc.code})
            # 只有"没抢到迁移"（或明确过期）才需要读回当前态分辨 expired/rejected。
            # 参数不合法、角色不够、目标不存在都是执行前就被拒：动作仍是 proposed，
            # 报成 expired 会让值班员以为"窗口过了"，其实换个人/修好参数还能再来。
            status = await self._conflict_status(action_id) if exc.code in {"E_ACTION_CONFLICT", "E_ACTION_EXPIRED"} else "rejected"
            return {"status": status, "action_id": action_id, "action": action.spec.name, "reason": exc.message, "result": None}
        data = {} if outcome.result is None else dict(outcome.result.data)
        return {"status": "executed", "action_id": action_id, "action": action.spec.name, "actor": actor, "result": data}

    async def _step_up(
        self,
        action: Action,
        operator: Operator | None,
        operator_key: str | None,
        record: dict[str, Any],
    ) -> dict[str, Any] | None:
        """高危动作的二次认证。返回 `None` 表示通过（可以执行），返回 dict 表示就地回执。

        只在"开关打开 + 该动作是高危"时才拦：默认关时 D5「指令即执行」逐字不变。
        回执与审计都带真实 `action_id`：认证失败是"这条动作没通过认证"，不是一条无主事件。
        """
        if self._operator_auth is None or not self._step_up_enabled:
            return None
        if not action.spec.dual_review:
            return None
        action_id = str(record.get("action_id") or "")
        if operator is None:
            return {
                "status": "rejected",
                "action_id": action_id,
                "action": action.spec.name,
                "reason": "高危动作需要二次认证：先选席位再带上 X-Aegis-Operator-Key",
                "result": None,
            }
        try:
            await self._operator_auth.step_up(raw_key=operator_key, operator_id=operator.operator_id)
        except OperatorKeyError as exc:
            # 认证失败也留一行审计（"想做但没通过"也要有账，与 §4.9 的三段结局一致）。
            if self._audit.available:
                await self._audit.write(
                    self._audit.rejected(
                        action_id=action_id,
                        action=action,
                        operator=operator,
                        error=exc,
                        utterance=str(record.get("utterance") or ""),
                        space_id=record.get("space_id"),
                        resolved_args=dict(record.get("resolved_args") or {}),
                    )
                )
            return {
                "status": "rejected",
                "action_id": action_id,
                "action": action.spec.name,
                "reason": exc.message,
                "result": None,
            }
        return None

    async def _vote_if_required(
        self,
        action_id: str,
        action: Action,
        record: dict[str, Any],
        operator: Operator | None,
    ) -> bool | dict[str, Any]:
        """复核支路。返回 `bool` 表示走执行器（是否第二票已预迁移），返回 dict 表示就地回执。

        资格/重复票的失败刻意往上抛 `ActionError`：那不是"确认没成功"的业务回执，
        而是 401/403/409 的 HTTP 事实，由 API 层的 `_http_error` 统一翻译。
        """
        required = int(record.get("review_required") or 0)
        if required < 2 or str(record.get("status")) != "proposed":
            return False
        if self._dual_review is None or not self._dual_review.enabled:
            # 台账是跨重启的：动作创建时开着双人复核，重启后配置关了，不能悄悄降成单票。
            return {
                "status": "rejected",
                "action_id": action_id,
                "action": action.spec.name,
                "reason": "该提案要求双人复核，但当前实例未开启该配置项；不降级成单票执行",
                "result": None,
            }
        try:
            outcome = await self._dual_review.vote(action_id, operator, action.spec)
        except ActionError as exc:
            # 复核资格被拒（duty 点复核 / 同人二票）是 §9 的"越权"一行：intent 已随提案在库
            if self._audit.available:
                await self._audit.write(
                    self._audit.rejected(
                        action_id=action_id,
                        action=action,
                        operator=operator,
                        error=exc,
                        utterance=str(record.get("utterance") or ""),
                        space_id=record.get("space_id"),
                        resolved_args=dict(record.get("resolved_args") or {}),
                    )
                )
            self._reject(None, action=action.spec.name, reason=exc.message, detail={"code": exc.code})
            raise
        if outcome.ready:
            return True
        reviews = list(outcome.record.get("reviewers") or [])
        return {
            "status": "awaiting_review",
            "action_id": action_id,
            "action": action.spec.name,
            "review_required": required,
            "reviews": reviews,
            "reason": (
                f"已收 {len(reviews)}/{required} 票，还需另一名"
                f"{self._review_policy.minimum_role(action.spec).value} 及以上席位复核（不能是同一人）"
            ),
            "result": None,
        }

    async def _conflict_status(self, action_id: str) -> str:
        """确认失败时读回当前态：已过期与已处置是两句不同的话。"""
        if self._repo is None:
            return "rejected"
        current = await self._repo.get_action(action_id)
        if current is None:
            return "rejected"
        status = str(current["status"])
        # 仍是 proposed 却没抢到原子 UPDATE：只可能是 expires_at 已过（那条 UPDATE 带了这个条件）
        return "expired" if status in {"expired", "proposed"} else "rejected"

    async def create_action(
        self,
        action_name: str,
        params: dict[str, Any],
        *,
        operator: Operator | None,
        space_id: str | None = None,
        idempotency_key: str | None = None,
        utterance: str = "",
    ) -> dict[str, Any]:
        """不经对话直接创建动作（供 UI 按钮与多步执行）。读动作会直接执行，写动作落 `proposed`。"""
        action = self._registry.require(action_name)
        ctx = ActionContext(
            session_id="",
            utterance=utterance,
            operator=operator,
            space_id=space_id,
            idempotency_key=idempotency_key,
        )
        outcome = await self._executor.run(action, params, ctx)
        if outcome.status == "needs_confirm":
            return {
                "status": "proposed",
                "action_id": outcome.action_id,
                "action": action_name,
                "expires_in_seconds": outcome.expires_in_seconds,
                "replayed": outcome.replayed,
                "preview": None if outcome.preview is None else outcome.preview.as_dict(),
                "review_required": outcome.review_required,
                "reviews": list(outcome.reviews),
            }
        data = {} if outcome.result is None else dict(outcome.result.data)
        return {"status": "executed", "action_id": None, "action": action_name, "result": data}

    async def action_status(self, action_id: str) -> dict[str, Any] | None:
        if self._repo is None:
            return None
        return await self._repo.get_action(action_id)

    async def maintenance(self) -> dict[str, int]:
        """周期收尾动作台账：`proposed` 过期置 `expired`，卡死的 `executing` 置 `failed`。

        与启动时 `container._recover_control_actions` 同一套判据。只在那时收一次不够：
        长跑进程里卡住的 `executing` 与过期的提案谁也等不到下一次重启，
        确认时只能得到一句"已被处置"，而台账上它永远是执行中。
        """
        if self._repo is None:
            return {"expired": 0, "failed": 0}
        return {
            "expired": await self._repo.expire_actions(),
            "failed": await self._repo.recover_stuck_actions(),
        }

    async def revoke_action(self, *, action_id: str, operator: Operator | None = None) -> dict[str, Any]:
        """60 秒撤销窗口：先认人、再认动作，最后才动台账。

        撤销不是"删掉一条提案"：已执行的动作已经在业务侧落了副作用，必须调
        `action.revoke()` 做补偿（发布 → 标记 revoked；改级 → 改回原等级）。
        责任落在发起人身上：本人只能撤自己的，值班长及以上可以替别人收尾。
        """
        if self._repo is None:
            raise ActionError("E_ACTION_UNAVAILABLE", "当前形态没有动作台账（control_actions），撤销不可用")
        if operator is None:
            raise ActionError("E_OPERATOR_REQUIRED", "撤销动作需要有效席位（X-Aegis-Operator）")
        record = await self._repo.get_action(action_id)
        if record is None:
            raise ActionError("E_ACTION_NOT_FOUND", f"动作不存在：{action_id}")
        action = self._registry.get(str(record["action"]))
        if action is None:
            raise ActionError("E_ACTION_NOT_FOUND", f"动作 {record['action']} 不在注册表内")
        if not action.spec.reversible:
            raise ActionError(
                "E_ACTION_NOT_REVERSIBLE",
                f"「{action.spec.title}」不可撤销：只有声明可逆的动作提供撤销",
            )
        if operator.operator_id != str(record["operator_id"]) and not role_at_least(operator.role, OperatorRole.LEAD):
            raise ActionError(
                "E_ACTION_NOT_ALLOWED",
                f"撤销别人的动作需要 {OperatorRole.LEAD.value} 及以上角色；本人只能撤销自己发起的动作",
            )
        status = str(record.get("status") or "")
        if status not in {"proposed", "executed"}:
            raise ActionError("E_ACTION_CONFLICT", f"动作当前是 {status}，不能撤销")
        revoked = await self._repo.revoke_action(action_id)
        if revoked is None:
            raise ActionError("E_ACTION_CONFLICT", "超出撤销窗口（执行后 60 秒内），或动作已被处置；请走正式撤回流程")

        compensation: dict[str, Any] | None = None
        compensation_error: str | None = None
        if status == "executed":
            # 提案还没执行：撤掉它只是撤掉一次授权，业务侧什么都没发生，不需要补偿
            try:
                outcome = await action.revoke(
                    record,
                    ActionContext(
                        session_id="",
                        operator=operator,
                        utterance=str(record.get("utterance") or ""),
                        space_id=record.get("space_id"),
                    ),
                )
                compensation = {"summary": outcome.summary, **outcome.data}
            except ActionError as exc:
                compensation_error = f"{exc.code}: {exc.message}"
            except Exception as exc:
                compensation_error = f"{type(exc).__name__}: {exc}"

        after: dict[str, Any] = {"revoked": True}
        if compensation is not None:
            after["compensation"] = compensation
        if compensation_error is not None:
            after["compensation_error"] = compensation_error
        if self._audit.available:
            await self._audit.write(
                self._audit.result(
                    action_id=action_id,
                    action=action,
                    params=dict(record.get("resolved_args") or {}),
                    targets=ResolvedTargets(),
                    ctx=ActionContext(
                        session_id="", operator=operator, utterance=str(record.get("utterance") or ""), space_id=record.get("space_id")
                    ),
                    after=after,
                )
            )
        result: dict[str, Any] = {"status": "revoked", "action_id": action_id, "action": action.spec.name, "was": status}
        if compensation_error is not None:
            result["compensation_error"] = compensation_error
        elif compensation is not None:
            result["compensation"] = compensation
        return result

    # ---------- 编排 ----------

    async def _run_intent(
        self,
        session: AssistantSession,
        intent: Intent,
        *,
        body: str,
        operator: Operator | None,
        reporter: str | None,
        region_code: str | None,
        idempotency_key: str | None,
        identity_kind: str,
    ) -> AsyncGenerator[AssistantEvent, None]:
        if intent.rejected:
            self._reject(session, action=intent.rejected, reason="建议的动作不在注册表内")
            if self._audit.available:
                reason = f"LLM 建议的动作 {intent.rejected} 不在注册表内，已拒绝"
                await self._audit.write(
                    self._audit.rejected(
                        action_id=None,
                        action=intent.rejected,
                        operator=operator,
                        error=ActionError("E_ACTION_NOT_OPEN", reason),
                        utterance=body[:500],
                    )
                )
        if intent.action is None:
            yield AssistantEvent("answer", {"text": self._capability_hint(intent.note)})
            return
        action = self._registry.require(intent.action)
        # 强身份闸（设计 §14.1.1）：强认证开启后，写动作必须带有效令牌（identity_kind='key'），
        # 仅声明身份在此拒绝、不进频控也不落提案；读动作不会走到这里。
        if action.spec.writes and self._strong_identity_required(identity_kind):
            reason = "该操作需要按人令牌：请带上 X-Aegis-Operator-Key（强认证开启后写动作不接受仅声明的席位）"
            self._reject(session, action=intent.action, reason=reason, detail={"code": "E_OPERATOR_KEY_REQUIRED"})
            if self._audit.available:
                await self._audit.write(
                    self._audit.rejected(
                        action_id=None,
                        action=action,
                        operator=operator,
                        error=ActionError("E_OPERATOR_KEY_REQUIRED", reason),
                        utterance=body[:500],
                    )
                )
            yield AssistantEvent("error", {"code": "E_OPERATOR_KEY_REQUIRED", "message": reason})
            yield AssistantEvent("answer", {"text": reason})
            return
        # 写动作频控（按操作者）：提示等待而不是静默排队，读动作不占额度。
        # 匿名写动作不占频控（它会在执行器处被 E_OPERATOR_REQUIRED 拒绝）。
        # 拒绝也要落 audit_ops——频控是"这次不执行"的决定，必须可查。
        if action.spec.writes and operator is not None and not self._writes.allow(operator.operator_id):
            reason = "本会话一分钟内的写动作已达 10 次，请稍后再试；读动作不受影响"
            self._reject(session, action=intent.action, reason=reason, detail={"code": "E_ACTION_RATE_LIMITED"})
            if self._audit.available:
                await self._audit.write(
                    self._audit.rejected(
                        action_id=None,
                        action=action,
                        operator=operator,
                        error=ActionError("E_ACTION_RATE_LIMITED", reason),
                        utterance=body[:500],
                    )
                )
            yield AssistantEvent("error", {"code": "E_ACTION_RATE_LIMITED", "message": reason})
            yield AssistantEvent("answer", {"text": reason})
            return
        ctx = ActionContext(
            session_id=session.session_id,
            utterance=body[:500],
            operator=operator,
            approved=False,
            idempotency_key=idempotency_key,
        )
        yield AssistantEvent("status", {"text": f"执行 {intent.action}"})
        try:
            outcome = await self._executor.run(action, dict(intent.args), ctx)
        except ActionError as exc:
            self._reject(session, action=intent.action, reason=exc.message, detail={"code": exc.code})
            yield AssistantEvent("error", {"code": exc.code, "message": exc.message})
            yield AssistantEvent("answer", {"text": exc.message})
            return

        if outcome.status == "needs_confirm":
            preview = {} if outcome.preview is None else outcome.preview.as_dict()
            yield AssistantEvent(
                "proposal",
                {
                    "action_id": outcome.action_id,
                    "action": intent.action,
                    "summary": f"{action.spec.title}：{body[:120]}" if body else action.spec.title,
                    "args": dict(intent.args),
                    "status": "proposed",
                    "expires_in_seconds": outcome.expires_in_seconds,
                    "replayed": outcome.replayed,
                    "preview": preview,
                    "review_required": outcome.review_required,
                    "reviews": list(outcome.reviews),
                    "session_id": session.session_id,
                },
            )
            review_note = "需两名不同席位复核，" if outcome.review_required >= 2 else ""
            yield AssistantEvent(
                "answer",
                {
                    "text": f"已生成待确认动作「{action.spec.title}」（{outcome.action_id}），{review_note}确认后才会执行；"
                    f"{int(outcome.expires_in_seconds // 60)} 分钟内有效。"
                },
            )
            return

        result = outcome.result
        data = {} if result is None else dict(result.data)
        yield AssistantEvent("result", {"action": intent.action, **data})
        text = "" if result is None else result.summary
        if not text:
            text = narrate_result(intent.action, data)
        yield AssistantEvent("answer", {"text": await self._polish(text, session)})

    def _strong_identity_required(self, identity_kind: str) -> bool:
        """强认证开启后，写动作必须 identity_kind='key'；口径与 API 层 `_require_strong_identity` 一致。"""
        return bool(self._settings.operator_key_auth_enabled) and identity_kind != "key"

    def _capability_hint(self, note: str) -> str:
        titles = "、".join(action.spec.title for action in self._registry.actions)
        prefix = "没听懂这句，" if not note else f"{note}，"
        return prefix + "可以做的事：" + titles

    # ---------- 措辞层（LLM 只改措辞，不新增数字） ----------

    async def _polish(self, facts: str, session: AssistantSession | None) -> str:
        if not self.llm_available or not facts:
            return facts[:_ANSWER_LIMIT]
        llm = self._llm
        if llm is None:
            return facts[:_ANSWER_LIMIT]
        # 润色是可有可无的一层：慢模型不许把 answer 帧（已拿到的事实）扣在手里。
        # 超时就直出，reason 里留一行降级，排查时能分清"模型慢"与"模型编数"。
        timeout = float(self._settings.assistant_polish_timeout_seconds)
        try:
            polished = await asyncio.wait_for(
                llm.chat(
                    [
                        {
                            "role": "system",
                            "content": "你是值班秘书。只能复述给定事实，不得新增数字、地名、等级或时间；输出简洁中文要点。",
                        },
                        {"role": "user", "content": facts[:2_000]},
                    ],
                    temperature=0.2,
                ),
                timeout=timeout,
            )
        except TimeoutError:
            self._reject(session, action="llm.polish", reason=f"措辞润色超时（>{timeout:.1f}s），按事实直出")
            return facts[:_ANSWER_LIMIT]
        except Exception as exc:
            self._reject(session, action="llm.polish", reason=f"措辞润色降级 {type(exc).__name__}")
            return facts[:_ANSWER_LIMIT]
        invented = invented_numbers(polished, facts)
        if invented:
            self._reject(session, action="llm.polish", reason="润色引入新数字，整段弃用", detail={"invented": sorted(invented)[:8]})
            return facts[:_ANSWER_LIMIT]
        return str(polished)[:_ANSWER_LIMIT]

    # ---------- 会话与留痕 ----------

    def _touch(self, session_id: str | None) -> AssistantSession:
        now = time.monotonic()
        if session_id and (existing := self._sessions.get(session_id)):
            existing.updated_at = now
            return existing
        self._evict_expired(now)
        sid = session_id or f"as_{secrets.token_hex(6)}"
        session = AssistantSession(session_id=sid, trace_id=f"trc_{secrets.token_hex(8)}", updated_at=now)
        self._sessions[sid] = session
        self._order.append(sid)
        while len(self._sessions) > _MAX_SESSIONS:
            oldest = self._order.popleft()
            if oldest != sid:
                self._sessions.pop(oldest, None)
        return session

    def _evict_expired(self, now: float) -> None:
        stale = [sid for sid, session in self._sessions.items() if now - session.updated_at > self._ttl]
        for sid in stale:
            self._sessions.pop(sid, None)
            if sid in self._order:
                self._order.remove(sid)

    def _reject(self, session: AssistantSession | None, *, action: str, reason: str, detail: dict[str, Any] | None = None) -> None:
        row: dict[str, Any] = {"action": action, "reason": reason, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        if detail:
            row["detail"] = detail
        self.rejections.append(row)
        if session is not None:
            session.history.append({"role": "system", "text": f"rejected {action}: {reason}"[:500]})
