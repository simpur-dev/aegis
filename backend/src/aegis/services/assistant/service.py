"""对话编排：把一句话组织成一串可解释的帧。

这一层只做四件事：开会话、判意图、调执行器、把结果说成人话。它不实现任何动作，也不碰业务表
（演练/上报由动作自己调既有服务）。写动作的确认仍在内存 pending 里，P2 接 `control_actions`
时只改这一层的两个方法。
"""

from __future__ import annotations

import secrets
import time
from collections import deque
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Any

from aegis.config import Settings, get_settings
from aegis.domain.operator import Operator
from aegis.observability.tracer import Tracer
from aegis.services.assistant.base import ActionContext, ActionError
from aegis.services.assistant.executor import Executor
from aegis.services.assistant.intent import Intent, IntentClassifier, out_of_scope_action
from aegis.services.assistant.narrate import narrate_result
from aegis.services.assistant.registry import ActionRegistry
from aegis.services.interpret.text_guard import number_tokens

_MAX_SESSIONS = 200
_MAX_TURNS = 20
_MAX_PENDING_PER_SESSION = 8
_ANSWER_LIMIT = 4_000


@dataclass(frozen=True, slots=True)
class AssistantEvent:
    """一帧 SSE 事件。`type` 是前端渲染的分支键，`data` 里只放可直接外显的事实。"""

    type: str
    data: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {"type": self.type, **self.data}


@dataclass(slots=True)
class PendingAction:
    action_id: str
    action: str
    args: dict[str, Any]
    summary: str
    expires_at: float
    status: str = "pending"

    def as_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "action": self.action,
            "summary": self.summary,
            "args": {key: (value[:160] if isinstance(value, str) else value) for key, value in self.args.items()},
            "status": self.status,
            "expires_in_seconds": max(0.0, round(self.expires_at - time.monotonic(), 1)),
        }


@dataclass(slots=True)
class AssistantSession:
    session_id: str
    trace_id: str
    updated_at: float
    history: deque[dict[str, str]] = field(default_factory=lambda: deque(maxlen=_MAX_TURNS * 2))
    pending: dict[str, PendingAction] = field(default_factory=dict)


class AssistantService:
    """语义交互服务本体：纯 asyncio，所有外部能力经注册表里的动作注入。"""

    def __init__(
        self,
        *,
        registry: ActionRegistry,
        llm: Any | None = None,
        settings: Settings | None = None,
        tracer: Tracer | None = None,
    ) -> None:
        self._registry = registry
        self._classifier = IntentClassifier(registry, llm=llm)
        self._executor = Executor()
        self._llm = llm
        self._settings = settings or get_settings()
        self._ttl = float(self._settings.assistant_session_ttl_seconds)
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
            "max_pending_actions": _MAX_PENDING_PER_SESSION,
        }

    async def respond(
        self,
        text: str,
        *,
        session_id: str | None = None,
        operator: Operator | None = None,
        reporter: str | None = None,
        region_code: str | None = None,
        hazard_hint: str | None = None,
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

        # 越权闸：先看这句话是不是在要一个"还没开放"的写动作，再谈查询。
        violation = out_of_scope_action(body)
        if violation is not None:
            action_name, verb = violation
            if self._registry.get(action_name) is None:
                self._reject(session, action=action_name, reason=f"指令要求「{verb}」，该动作当前未开放")
                yield AssistantEvent(
                    "rejected",
                    {
                        "instruction": verb,
                        "action": action_name,
                        "reason": "该动作当前未开放；可以做的动作见 GET /assistant/capabilities",
                        "session_id": session.session_id,
                    },
                )
                yield AssistantEvent("answer", {"text": "该指令涉及的动作尚未开放，已留痕。"})
                return

        intent = await self._classifier.classify_async(body, region_code=region_code, hazard_hint=hazard_hint, reporter=reporter)
        yield AssistantEvent("intent", intent.as_dict())
        session.history.append({"role": "user", "text": body[:500]})

        answer_parts: list[str] = []
        async for event in self._run_intent(session, intent, body=body, operator=operator, reporter=reporter, region_code=region_code):
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
        self, *, session_id: str, action_id: str, actor: str | None = None, operator: Operator | None = None
    ) -> dict[str, Any]:
        """人工确认执行：一次性、带期、限本会话。"""
        session = self._sessions.get(session_id)
        if session is None:
            return {"status": "rejected", "reason": "会话不存在或已过期", "action_id": action_id}
        pending = session.pending.get(action_id)
        if pending is None:
            self._reject(session, action=action_id, reason="动作 ID 不在本会话待确认清单内")
            return {"status": "rejected", "reason": "动作 ID 不在本会话待确认清单内", "action_id": action_id}
        if pending.status != "pending":
            return {"status": "rejected", "reason": f"动作已处置（{pending.status}），不重复执行", "action_id": action_id}
        if time.monotonic() > pending.expires_at:
            pending.status = "expired"
            self._reject(session, action=pending.action, reason="确认超时，动作已过期")
            return {"status": "expired", "reason": "确认超时，动作已过期", "action_id": action_id}
        action = self._registry.get(pending.action)
        if action is None or not action.spec.needs_confirm:
            pending.status = "rejected"
            self._reject(session, action=pending.action, reason="动作不在需确认目录内")
            return {"status": "rejected", "reason": "动作不在需确认目录内", "action_id": action_id}

        pending.status = "running"
        ctx = ActionContext(
            session_id=session.session_id,
            utterance=pending.summary,
            operator=operator,
            approved=True,
        )
        try:
            outcome = await self._executor.run(action, dict(pending.args), ctx)
        except ActionError as exc:
            pending.status = "failed"
            self._reject(session, action=pending.action, reason=exc.message, detail={"code": exc.code})
            return {"status": "failed", "action_id": action_id, "action": pending.action, "reason": exc.message, "result": None}
        result = outcome.result
        data = {} if result is None else dict(result.data)
        pending.status = "executed"
        return {"status": "executed", "action_id": action_id, "action": pending.action, "actor": actor, "result": data}

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
    ) -> AsyncGenerator[AssistantEvent, None]:
        if intent.rejected:
            self._reject(session, action=intent.rejected, reason="建议的动作不在注册表内")
        if intent.action is None:
            yield AssistantEvent("answer", {"text": self._capability_hint(intent.note)})
            return
        action = self._registry.require(intent.action)
        ctx = ActionContext(
            session_id=session.session_id,
            utterance=body[:500],
            operator=operator,
            approved=False,
        )
        yield AssistantEvent("status", {"text": f"执行 {intent.action}"})
        try:
            outcome = await self._executor.run(action, dict(intent.args), ctx)
        except ActionError as exc:
            self._reject(session, action=intent.action, reason=exc.message, detail={"code": exc.code})
            yield AssistantEvent("error", {"code": exc.code, "message": exc.message})
            yield AssistantEvent("answer", {"text": exc.message})
            return

        if outcome.needs_confirm:
            pending = self._propose(session, intent.action, dict(intent.args), body)
            preview = {} if outcome.preview is None else outcome.preview.as_dict()
            yield AssistantEvent("proposal", {**pending.as_dict(), "preview": preview, "session_id": session.session_id})
            yield AssistantEvent(
                "answer",
                {
                    "text": f"已生成待确认动作「{action.spec.title}」（{pending.action_id}），确认后才会执行；"
                    f"{int(self._ttl // 60)} 分钟内有效。"
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
        try:
            polished = await llm.chat(
                [
                    {
                        "role": "system",
                        "content": "你是值班秘书。只能复述给定事实，不得新增数字、地名、等级或时间；输出简洁中文要点。",
                    },
                    {"role": "user", "content": facts[:2_000]},
                ],
                temperature=0.2,
            )
        except Exception as exc:
            self._reject(session, action="llm.polish", reason=f"措辞润色降级 {type(exc).__name__}")
            return facts[:_ANSWER_LIMIT]
        invented = number_tokens(polished) - number_tokens(facts)
        if invented:
            self._reject(session, action="llm.polish", reason="润色引入新数字，整段弃用", detail={"invented": sorted(invented)[:8]})
            return facts[:_ANSWER_LIMIT]
        return str(polished)[:_ANSWER_LIMIT]

    # ---------- 会话与留痕 ----------

    def _touch(self, session_id: str | None) -> AssistantSession:
        now = time.monotonic()
        if session_id and (existing := self._sessions.get(session_id)):
            existing.updated_at = now
            self._expire_pending(existing, now)
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

    def _propose(self, session: AssistantSession, action: str, args: dict[str, Any], body: str) -> PendingAction:
        self._expire_pending(session, time.monotonic())
        while len(session.pending) >= _MAX_PENDING_PER_SESSION:
            session.pending.pop(next(iter(session.pending)), None)
        spec = self._registry.require(action).spec
        pending = PendingAction(
            action_id=f"act_{secrets.token_hex(6)}",
            action=action,
            args=args,
            summary=f"{spec.title}：{body[:120]}" if body else spec.title,
            expires_at=time.monotonic() + self._ttl,
        )
        session.pending[pending.action_id] = pending
        return pending

    def _expire_pending(self, session: AssistantSession, now: float) -> None:
        for pending in session.pending.values():
            if pending.status == "pending" and now > pending.expires_at:
                pending.status = "expired"

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
