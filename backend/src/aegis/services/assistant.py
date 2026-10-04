"""语义交互服务：自然语言 → 白名单动作 + 认知镜像（架构文档 §九「用户交互中心」的落点）。

完善计划批次 B1 的四类任务：查询、预案问答、演练、上报。三条硬口径写在这里，不是愿景：

1. **执行类必须人工确认**：`run.drill` / `create.report` 只产出"待确认动作"，本服务从不落地执行；
   确认走 `confirm()`，动作 ID 一次性有效（用过即失效、过期即失效、跨会话不可用）。
2. **白名单之外无动作**：意图先由规则词表判，规则判不动才让 LLM 建议；LLM 建议的动作若不在
   白名单内，一律拒绝并留痕（`rejections`）。LLM 说"我替你发布了预警"在这套系统里不成立。
3. **认知镜像只讲平台已有的事实**：叙述由 `rationale`/`degradations`/`stages`/通道回执拼成；
   LLM 只做措辞润色，且润色文本里出现事实里没有的数字就整段弃用（不编数）。

内核环不 import 可选腿（架构铁律 3）：所有外部能力经 `AssistantActions` 注入，
构造点在 `container.py`；缺哪个能力就如实回答"当前形态不可执行"，不假装有结果。
"""

from __future__ import annotations

import re
import secrets
import time
from collections import deque
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from aegis.config import Settings, get_settings
from aegis.domain.enums import HazardType
from aegis.observability.tracer import Tracer
from aegis.services.semantic_parser import DisasterTextParser, detect_hazard, extract_region
from aegis.services.task_parser import PLAYBOOKS

_MAX_SESSIONS = 200
_MAX_TURNS = 20
_MAX_PENDING_PER_SESSION = 8
_ANSWER_LIMIT = 4_000

_WARNING_ID_RE = re.compile(r"wrn_[0-9a-f]{6,24}")
_TRACE_ID_RE = re.compile(r"trc_[0-9a-f]{16}")
_EVENT_ID_RE = re.compile(r"evt_[0-9a-f]{12}")
_TASK_ID_RE = re.compile(r"stu_[0-9a-f]{16}")


@dataclass(frozen=True, slots=True)
class ActionSpec:
    """一个白名单动作的说明书：是否需要人工确认、缺依赖时的答复口径。

    `example` 是这个词表自己的一句话样本：能力面把它交给前端当"点这里试一条"的预填文本，
    而 `tests/unit/test_assistant_intent.py` 断言每个 example 都能被本模块的词表判回它自己
    那个动作。词表改了、样本没跟上，那条用例就会响——否则页面上的建议条会一路悄悄失效。
    """

    name: str
    title: str
    requires_confirmation: bool
    needs: tuple[str, ...] = ()
    example: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": self.name,
            "title": self.title,
            "requires_confirmation": self.requires_confirmation,
            "needs": list(self.needs),
            "example": self.example,
        }


#: 白名单就是白名单：不在这里的动作，无论规则还是 LLM 提出来，都只会得到一次拒绝。
ACTION_SPECS: tuple[ActionSpec, ...] = (
    ActionSpec("query.warnings", "查询已发布预警", False, ("list_warnings",), "最近发布了哪些预警"),
    ActionSpec("query.chains", "查询最近链路", False, ("list_chains",), "最近的事件列表"),
    ActionSpec("query.tasks", "查询任务单元", False, ("get_task",), "任务单元清单"),
    ActionSpec("query.stations", "查询站点台账", False, ("list_stations",), "站点台账里有哪些站"),
    ActionSpec("query.agents", "查询在线智能体", False, ("list_agents",), "现在有哪些在线智能体"),
    ActionSpec("query.metrics", "查询指标量测", False, ("latency_report",), "p95 时延达标吗"),
    ActionSpec("explain.warning", "解释一条预警（认知镜像）", False, ("get_warning",), "解释 wrn_… 为什么定这个等级"),
    ActionSpec("explain.chain", "解释一条链路（认知镜像）", False, ("get_chain",), "链路追踪 trc_… 每段耗时"),
    ActionSpec("ask.plan", "预案问答", False, (), "这个沟道泥位抬升要不要转移群众"),
    ActionSpec("run.drill", "发起一次演练", True, ("run_drill",), "发起一次演练"),
    ActionSpec("create.report", "提交人工上报", True, ("submit_report",), "我要上报：沟道泥位抬升，下游有村庄"),
    ActionSpec("list.actions", "列出可用动作", False, (), "你能做什么"),
)
ACTION_WHITELIST: frozenset[str] = frozenset(spec.name for spec in ACTION_SPECS)
_SPEC_BY_NAME: dict[str, ActionSpec] = {spec.name: spec for spec in ACTION_SPECS}

#: 意图词表：顺序即优先级（先具体后宽泛），"解释 wrn_xxx 为什么是红色" 不能被查询意图抢走。
_INTENT_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("explain.warning", ("为什么", "依据", "凭什么", "解释", "怎么定的")),
    ("explain.chain", ("链路追踪", "谁判的", "每段耗时", "段时延")),
    ("query.metrics", ("指标", "时延", "成功率", "sla", "p95", "违约")),
    ("query.agents", ("智能体", "agent", "在线", "注册")),
    ("query.stations", ("站点", "台站", "监测站", "台账")),
    ("query.tasks", ("任务单元", "stu", "任务清单")),
    ("query.chains", ("链路", "事件列表", "最近事件")),
    ("query.warnings", ("预警", "发布", "告警")),
    ("ask.plan", ("预案", "处置方案", "怎么办", "怎么处置", "要不要转移", "转移路线")),
    ("run.drill", ("演练", "模拟一次", "跑一次", "surge")),
    ("create.report", ("上报", "报告险情", "我要报", "我发现", "我看到", "刚看到")),
    ("list.actions", ("你能做什么", "会什么", "帮助", "help")),
)


#: 越权指令闸：命中就明确拒绝并留痕，绝不"降级成一个只读查询"糊过去。
#: 不做这一层的后果很具体——"把全网预警都删掉"里有"预警"二字，会被查询意图接住并回一份预警清单，
#: 用户读到的是"它听懂了"，实际发生的是"它回避了"，而验收口径要的是"越权被拒且留痕"。
_OUT_OF_SCOPE_VERBS = (
    "删除",
    "删掉",
    "清空",
    "撤掉",
    "撤回",
    "撤销",
    "停用",
    "关机",
    "重启",
    "下线",
    "覆盖",
    "群发",
    "改阈值",
    "修改阈值",
    "调整阈值",
    "直接发布",
    "立即发布",
)
_OUT_OF_SCOPE_PREFIX = re.compile(r"^(把|请|帮我|我们|立即|马上|现在|快)")


def out_of_scope(text: str) -> str | None:
    """命中返回被越权的动词，未命中返回 None。

    只有祈使式（把/请/帮我/立即…）或带"全部/所有/全网/都"这类整体作用域才判越权：
    "这条预警可以撤回吗""为什么发布了红色预警"都是正常问句，一律闸掉就等于把助手做废。
    """
    body = str(text or "").strip()
    verb = next((item for item in _OUT_OF_SCOPE_VERBS if item in body), None)
    if verb is None:
        return None
    if _OUT_OF_SCOPE_PREFIX.search(body) or any(word in body for word in ("全部", "所有", "全网", "都")):
        return verb
    return None


@dataclass(frozen=True, slots=True)
class Intent:
    action: str
    args: dict[str, Any] = field(default_factory=dict)
    confidence: float = 1.0
    decided_by: str = "rule"
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "args": {key: (value[:120] if isinstance(value, str) else value) for key, value in self.args.items()},
            "confidence": round(self.confidence, 4),
            "decided_by": self.decided_by,
            "note": self.note,
            "requires_confirmation": _SPEC_BY_NAME[self.action].requires_confirmation if self.action in _SPEC_BY_NAME else True,
        }


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
    created_at: float
    expires_at: float
    status: str = "pending"
    result: dict[str, Any] | None = None

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
    created_at: float
    updated_at: float
    history: deque[dict[str, str]] = field(default_factory=lambda: deque(maxlen=_MAX_TURNS * 2))
    pending: dict[str, PendingAction] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "turns": len(self.history),
            "pending_actions": [action.as_dict() for action in self.pending.values() if action.status == "pending"],
            "history": list(self.history),
        }


@dataclass(slots=True)
class AssistantActions:
    """注入给助手的外部能力。全部可选：缺位在答复里是明说的事实，不是静默降级。"""

    list_warnings: Callable[..., Awaitable[list[dict[str, Any]]]] | None = None
    get_warning: Callable[[str], Awaitable[dict[str, Any] | None]] | None = None
    list_chains: Callable[..., Awaitable[list[dict[str, Any]]]] | None = None
    get_chain: Callable[[str], Awaitable[dict[str, Any] | None]] | None = None
    get_task: Callable[[str], Awaitable[dict[str, Any] | None]] | None = None
    list_stations: Callable[..., Awaitable[list[dict[str, Any]]]] | None = None
    list_agents: Callable[[], Awaitable[dict[str, Any]]] | None = None
    latency_report: Callable[[], dict[str, Any]] | None = None
    recall_plan: Callable[..., Awaitable[list[dict[str, Any]]]] | None = None
    run_drill: Callable[..., Awaitable[dict[str, Any]]] | None = None
    submit_report: Callable[..., Awaitable[dict[str, Any]]] | None = None


class AssistantService:
    """语义交互服务本体。纯 asyncio、零 I/O，所有外部动作由 `AssistantActions` 提供。"""

    def __init__(
        self,
        *,
        actions: AssistantActions | None = None,
        parser: DisasterTextParser | None = None,
        llm: Any | None = None,
        settings: Settings | None = None,
        tracer: Tracer | None = None,
    ) -> None:
        self._actions = actions or AssistantActions()
        self._parser = parser
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
        return bool(self._llm is not None and getattr(self._llm, "available", False))

    def capabilities(self) -> dict[str, Any]:
        """装配事实：哪些动作有依赖、语义服务是否配了 LLM。前端据此显示"未配置"而不是装可用。"""
        wired = {name: callable(getattr(self._actions, name, None)) for name in AssistantActions.__dataclass_fields__}
        return {
            "llm_configured": self.llm_available,
            "semantic_parser": self._parser is not None,
            "wired_actions": wired,
            "actions": [
                {
                    **spec.as_dict(),
                    "available": not spec.needs or all(wired.get(dep, False) for dep in spec.needs),
                    "missing": [dep for dep in spec.needs if not wired.get(dep, False)],
                }
                for spec in ACTION_SPECS
            ],
            "session_ttl_seconds": self._ttl,
            "max_pending_actions": _MAX_PENDING_PER_SESSION,
        }

    def session(self, session_id: str | None) -> dict[str, Any] | None:
        session = self._sessions.get(session_id) if session_id else None
        return None if session is None else session.as_dict()

    async def respond(
        self,
        text: str,
        *,
        session_id: str | None = None,
        reporter: str | None = None,
        region_code: str | None = None,
        hazard_hint: str | None = None,
    ) -> AsyncGenerator[AssistantEvent, None]:
        """一次对话轮次：以事件流的形式给出过程与结果（API 层负责 SSE 编码）。"""
        # 时延测量一律用 perf_counter：Windows 上 time.monotonic() 走 GetTickCount64()，
        # 步进 15.6 ms，一轮 0.3 ms 的交互会被量成 0——考核项 ≤3s 于是永远绿。
        started = time.perf_counter()
        body = str(text or "").strip()[:2_000]
        session = self._touch(session_id)
        yield AssistantEvent(
            "meta",
            {
                "session_id": session.session_id,
                "llm_configured": self.llm_available,
                "actions": sorted(ACTION_WHITELIST),
            },
        )
        if not body:
            yield AssistantEvent("error", {"message": "消息为空"})
            return

        violation = out_of_scope(body)
        if violation is not None:
            # 越权答复必须是"被拒 + 留痕"，不能悄悄换成一个读动作回给你（完善计划 B1 验收口径）
            self._reject(session, action=violation, reason="指令超出白名单，未执行")
            yield AssistantEvent(
                "rejected",
                {
                    "instruction": violation,
                    "reason": "本助手只做只读查询、预案问答，以及经人工确认的演练与上报",
                    "session_id": session.session_id,
                },
            )
            yield AssistantEvent("answer", {"text": "该指令被拒绝并已留痕。可以做的事：" + "、".join(spec.title for spec in ACTION_SPECS)})
            return

        intent = await self._classify(body, region_code=region_code, hazard_hint=hazard_hint, reporter=reporter)
        yield AssistantEvent("intent", intent.as_dict())
        session.history.append({"role": "user", "text": body[:500]})

        answer_parts: list[str] = []
        async for event in self._execute(session, intent, body=body, reporter=reporter, region_code=region_code):
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

    async def confirm(self, *, session_id: str, action_id: str, actor: str | None = None) -> dict[str, Any]:
        """人工确认执行：一次性、带期、限本会话。拒绝与失败都要有账（完善计划 B1 验收口径）。"""
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
        if pending.action not in ACTION_WHITELIST or not _SPEC_BY_NAME[pending.action].requires_confirmation:
            pending.status = "rejected"
            self._reject(session, action=pending.action, reason="动作不在需确认白名单内")
            return {"status": "rejected", "reason": "动作不在需确认白名单内", "action_id": action_id}

        pending.status = "running"
        try:
            result = await self._perform(pending.action, dict(pending.args), session=session)
        except Exception as exc:
            pending.status = "failed"
            reason = f"{type(exc).__name__}: {str(exc)[:200]}"
            self._reject(session, action=pending.action, reason="执行失败", detail={"error": reason})
            # `reason` 与其它拒绝分支同名同义：前端不必为"失败"单独猜一个字段
            return {"status": "failed", "action_id": action_id, "action": pending.action, "reason": reason, "result": None}
        pending.status = "executed"
        pending.result = result
        return {"status": "executed", "action_id": action_id, "action": pending.action, "actor": actor, "result": result}

    def stats(self) -> dict[str, Any]:
        return {"sessions": len(self._sessions), "replies": self.replies, "rejections": len(self.rejections)}

    # ---------- 意图解析 ----------

    async def _classify(
        self,
        text: str,
        *,
        region_code: str | None,
        hazard_hint: str | None,
        reporter: str | None,
    ) -> Intent:
        matched = self._match_intent(text)
        if matched is not None:
            return self._build_intent(matched, text, region_code=region_code, hazard_hint=hazard_hint, reporter=reporter, decided_by="rule")
        if self.llm_available:
            suggested = await self._llm_intent(text)
            if suggested is not None:
                return self._build_intent(
                    suggested, text, region_code=region_code, hazard_hint=hazard_hint, reporter=reporter, decided_by="llm"
                )
        return Intent(action="list.actions", confidence=0.0, decided_by="none", note="未能识别意图，返回可用动作清单")

    @staticmethod
    def _match_intent(text: str) -> str | None:
        lowered = text.lower()
        if _WARNING_ID_RE.search(text):
            return "explain.warning"
        if _TRACE_ID_RE.search(text) or _EVENT_ID_RE.search(text):
            return "explain.chain"
        for action, keywords in _INTENT_RULES:
            if any(keyword in lowered for keyword in keywords):
                return action
        return None

    def _build_intent(
        self,
        action: str,
        text: str,
        *,
        region_code: str | None,
        hazard_hint: str | None,
        reporter: str | None,
        decided_by: str,
    ) -> Intent:
        if action not in ACTION_WHITELIST:
            self._reject(None, action=action, reason=f"动作 {action} 不在白名单内")
            return Intent(action="list.actions", confidence=0.0, decided_by=decided_by, note=f"建议动作 {action} 越权，已拒绝")
        args: dict[str, Any] = {}
        if action == "explain.warning" and (found := _WARNING_ID_RE.search(text)):
            args["warning_id"] = found.group()
        if action == "explain.chain":
            trace = _TRACE_ID_RE.search(text)
            event = _EVENT_ID_RE.search(text)
            if trace:
                args["trace_id"] = trace.group()
            elif event:
                args["event_id"] = event.group()
        if action == "query.tasks" and (task := _TASK_ID_RE.search(text)):
            args["task_unit_id"] = task.group()
        if action in {"ask.plan", "run.drill", "create.report"}:
            hazard = detect_hazard(text, hint=hazard_hint)
            args["hazard_type"] = None if hazard is None else hazard.value
        if action in {"run.drill", "create.report", "query.warnings", "query.stations"}:
            args["region_code"] = extract_region(text, default=region_code) or region_code or ""
        if action == "run.drill":
            args["scenario"] = "normal" if "normal" in text.lower() or "正常" in text else "surge"
            ticks = re.search(r"(\d+)\s*轮", text)
            args["ticks"] = max(1, min(50, int(ticks.group(1)))) if ticks else 2
        if action == "create.report":
            args["note"] = text[:2_000]
            args["reporter"] = (reporter or "web-assistant")[:64]
        if action == "query.warnings":
            args["limit"] = 10
        if action == "query.stations":
            args["limit"] = 20
        confidence = 1.0 if decided_by == "rule" else 0.6
        return Intent(action=action, args=args, confidence=confidence, decided_by=decided_by)

    async def _llm_intent(self, text: str) -> str | None:
        """规则判不动时才问 LLM：它只能在白名单里选，选外面的一次拒绝都算数。"""
        llm = self._llm
        if llm is None:
            return None
        try:
            raw = await llm.chat_json(
                [
                    {
                        "role": "system",
                        "content": (
                            '只输出 JSON：{"action": 下列之一, "reason": 一句话}。'
                            f"可选动作：{sorted(ACTION_WHITELIST)}。不得虚构其它动作。"
                        ),
                    },
                    {"role": "user", "content": text[:600]},
                ]
            )
        except Exception:
            return None
        action = str(raw.get("action", "")).strip()
        if action not in ACTION_WHITELIST:
            self._reject(None, action=action or "<empty>", reason="LLM 建议的动作不在白名单内")
            return None
        return action

    # ---------- 执行 ----------

    async def _execute(
        self,
        session: AssistantSession,
        intent: Intent,
        *,
        body: str,
        reporter: str | None,
        region_code: str | None,
    ) -> AsyncIterator[AssistantEvent]:
        spec = _SPEC_BY_NAME.get(intent.action)
        if spec is None:
            yield AssistantEvent("answer", {"text": "该动作不在白名单内，未执行。"})
            return
        missing = [dep for dep in spec.needs if not callable(getattr(self._actions, dep, None))]
        if missing:
            yield AssistantEvent(
                "answer",
                {"text": f"「{spec.title}」当前形态不可执行：缺少依赖 " + "、".join(missing) + "。可在配置面接入后重试。"},
            )
            return
        if spec.requires_confirmation:
            args = dict(intent.args)
            args.setdefault("reporter", reporter or "web-assistant")
            args.setdefault("region_code", region_code or args.get("region_code", ""))
            pending = self._propose(session, intent.action, args, body)
            yield AssistantEvent("proposal", {**pending.as_dict(), "session_id": session.session_id})
            yield AssistantEvent(
                "answer",
                {"text": f"已生成待确认动作「{spec.title}」（{pending.action_id}），确认后才会执行；{int(self._ttl // 60)} 分钟内有效。"},
            )
            return
        yield AssistantEvent("status", {"text": f"执行 {intent.action}"})
        try:
            result = await self._perform(intent.action, dict(intent.args), session=session)
        except Exception as exc:
            detail = f"{type(exc).__name__}: {str(exc)[:200]}"
            self._reject(session, action=intent.action, reason="查询执行失败", detail={"error": detail})
            yield AssistantEvent("error", {"message": detail})
            return
        yield AssistantEvent("result", {"action": intent.action, **result})
        yield AssistantEvent("answer", {"text": await self._narrate(intent.action, result)})

    async def _perform(self, action: str, args: dict[str, Any], *, session: AssistantSession) -> dict[str, Any]:
        """动作的唯一实现点：确认路径与查询路径共用，避免两份口径。"""
        a = self._actions
        if action == "query.warnings":
            rows = await _call(a.list_warnings, limit=int(args.get("limit", 10)), region_code=args.get("region_code") or None)
            return {"items": rows[:10], "count": len(rows)}
        if action == "query.chains":
            rows = await _call(a.list_chains, limit=int(args.get("limit", 8)))
            return {"items": rows[:8], "count": len(rows)}
        if action == "query.tasks":
            task_id = str(args.get("task_unit_id", ""))
            if not task_id:
                # 能力面把 example 当"点这里试一条"的预填文本交给前端，这句缺参数是必经之路：
                # 只说"缺少 stu_…"就是把芯片做成死胡同，得同时说出这个 ID 在哪儿能读到。
                return {
                    "error": "缺少任务单元 ID（stu_…）：这次什么都没查。ID 在「预警发布」页点「详情」后"
                    "那张「关联任务单元」表的任务标识列；也可以先「查询最近链路」，结果里的 task_units 就是这些 ID"
                }
            return {"task": await _call(a.get_task, task_id)}
        if action == "query.stations":
            rows = await _call(a.list_stations, region_code=args.get("region_code") or None, limit=int(args.get("limit", 20)))
            return {"items": rows[:20], "count": len(rows)}
        if action == "query.agents":
            return {"agents": await _call(a.list_agents)}
        if action == "query.metrics":
            report = a.latency_report() if a.latency_report is not None else {}
            return {"metrics": _trim_metrics(report)}
        if action == "explain.warning":
            return await self._explain_warning(str(args.get("warning_id", "")), session)
        if action == "explain.chain":
            return await self._explain_chain(str(args.get("trace_id") or args.get("event_id") or ""), session)
        if action == "ask.plan":
            return await self._plan_brief(args)
        if action == "run.drill":
            return {
                "drill": await _call(
                    a.run_drill,
                    scenario=args.get("scenario", "surge"),
                    ticks=int(args.get("ticks", 2)),
                    region_code=args.get("region_code") or None,
                )
            }
        if action == "create.report":
            return {
                "report": await _call(
                    a.submit_report,
                    note=str(args.get("note", "")),
                    region_code=str(args.get("region_code", "")),
                    reporter=str(args.get("reporter", "web-assistant")),
                    hazard_hint=args.get("hazard_type"),
                )
            }
        if action == "list.actions":
            return {"actions": [spec.as_dict() for spec in ACTION_SPECS]}
        raise ValueError(f"未知动作: {action}")

    async def _explain_warning(self, warning_id: str, session: AssistantSession) -> dict[str, Any]:
        if not warning_id:
            return {
                "error": "缺少预警编号（wrn_…）：这句里没读到编号，所以什么都没查。编号在「预警发布」页点「详情」后抽屉顶部的「预警标识」"
            }
        record = await _call(self._actions.get_warning, warning_id)
        if record is None:
            return {"warning_id": warning_id, "found": False}
        chain = None
        trace_id = str(record.get("trace_id", ""))
        if self._actions.get_chain is not None and _TRACE_ID_RE.fullmatch(trace_id):
            chain = await _call(self._actions.get_chain, trace_id)
        facts = narrate_warning(record, chain)
        return {"warning_id": warning_id, "found": True, "facts": facts, "text": await self._polish(facts, session)}

    async def _explain_chain(self, trace_id: str, session: AssistantSession) -> dict[str, Any]:
        if not trace_id:
            return {"error": "缺少链路标识（trc_/evt_…）：这句里没读到标识符，所以什么都没查。链路号在总览页「链路执行记录」的第一列"}
        chain = await _call(self._actions.get_chain, trace_id) if self._actions.get_chain is not None else None
        if chain is None:
            return {"trace_id": trace_id, "found": False, "note": "链路读视图只保留进程内最近事件"}
        facts = narrate_chain(chain)
        return {"trace_id": trace_id, "found": True, "facts": facts, "text": await self._polish(facts, session)}

    async def _plan_brief(self, args: dict[str, Any]) -> dict[str, Any]:
        hazard_value = args.get("hazard_type")
        hazard = HazardType(hazard_value) if hazard_value else detect_hazard(str(args.get("query", "")))
        steps = [
            {
                "task_type": step.task_type.value,
                "objective": step.objective,
                "owner_role": step.owner_role.value,
                "sla_seconds": step.sla_seconds,
                "capability": step.capability,
            }
            for step in PLAYBOOKS.get(hazard or HazardType.UNKNOWN, PLAYBOOKS[HazardType.UNKNOWN])
        ]
        recalled: list[dict[str, Any]] = []
        if self._actions.recall_plan is not None:
            try:
                recalled = list(
                    await _call(
                        self._actions.recall_plan,
                        query=hazard.cn if hazard else "",
                        hazard_type=None if hazard is None else hazard.value,
                        region_code=args.get("region_code") or None,
                        limit=3,
                    )
                )
            except Exception as exc:
                recalled = [{"degraded": True, "reason": f"{type(exc).__name__}"}]
        facts = narrate_plan(hazard, steps, recalled)
        return {"hazard_type": None if hazard is None else hazard.value, "steps": steps, "cases": recalled, "text": facts}

    # ---------- 认知镜像的措辞层 ----------

    async def _narrate(self, action: str, result: dict[str, Any]) -> str:
        if "text" in result and isinstance(result["text"], str):
            return result["text"][:_ANSWER_LIMIT]
        facts = narrate_result(action, result)
        return await self._polish(facts, None)

    async def _polish(self, facts: str, session: AssistantSession | None) -> str:
        """LLM 只允许改写措辞；出现事实里没有的数字就整段弃用。"""
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
        invented = _numbers(polished) - _numbers(facts)
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
        session = AssistantSession(
            session_id=sid,
            trace_id=f"trc_{secrets.token_hex(8)}",
            created_at=now,
            updated_at=now,
        )
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
        spec = _SPEC_BY_NAME[action]
        pending = PendingAction(
            action_id=f"act_{secrets.token_hex(6)}",
            action=action,
            args=args,
            summary=f"{spec.title}：{body[:120]}" if body else spec.title,
            created_at=time.monotonic(),
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
        """越权与被拒动作必须留痕：验收口径要求"指令越权被拒且留痕"。"""
        row: dict[str, Any] = {"action": action, "reason": reason, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        if detail:
            row["detail"] = detail
        self.rejections.append(row)
        if session is not None:
            session.history.append({"role": "system", "text": f"rejected {action}: {reason}"[:500]})


# ---------- 事实叙述（认知镜像的确定性部分，无 LLM 也能出完整答复） ----------


def narrate_warning(record: dict[str, Any], chain: dict[str, Any] | None = None) -> str:
    lines = [
        f"预警 {record.get('warning_id', '')}",
        f"灾种 {record.get('hazard_type', '')}，等级 {record.get('risk_level', '')} 级，区域 {'、'.join(record.get('region_codes') or [])}",
        f"生成 {record.get('generated_at', '')}，发布 {record.get('released_at') or '未发布'}",
    ]
    deliveries = record.get("deliveries") or []
    if deliveries:
        rendered = "；".join(
            f"{item.get('channel')}={item.get('status')}" + (f"({item.get('provider_msg_id')})" if item.get("provider_msg_id") else "")
            for item in deliveries
            if isinstance(item, dict)
        )
        if rendered:
            lines.append("通道回执：" + rendered)
    if record.get("translation_pending"):
        lines.append("藏汉译文待译（LLM 译文未生成，正文以中文为准）")
    if chain:
        lines.append(narrate_chain(chain))
    return "\n".join(lines)


def narrate_chain(chain: dict[str, Any]) -> str:
    stages = chain.get("stages") or []
    parts = [f"{item.get('name')}:{item.get('mode')}" + (f"({item.get('note')})" if item.get("note") else "") for item in stages]
    lines = ["链路段落：" + " → ".join(parts)] if parts else []
    risk = chain.get("risk") or {}
    if risk:
        lines.append(f"定级依据：{str(risk.get('rationale', ''))[:300]}（{risk.get('assessed_by', '')}）")
    if chain.get("degradations"):
        lines.append("降级留痕：" + "；".join(str(item) for item in chain["degradations"]))
    if chain.get("errors"):
        lines.append("错误：" + "；".join(str(item) for item in chain["errors"]))
    if chain.get("reference_cases"):
        lines.append("参考案例：" + "、".join(str(item) for item in chain["reference_cases"]))
    return "\n".join(lines)


def narrate_plan(hazard: HazardType | None, steps: list[dict[str, Any]], cases: list[dict[str, Any]]) -> str:
    title = f"{hazard.cn if hazard else '未定灾种'}处置要点"
    lines = [title] + [
        f"{index + 1}. [{step['task_type']}] {step['objective']}（责任角色 {step['owner_role']}，时限 {step['sla_seconds']} 秒）"
        for index, step in enumerate(steps)
    ]
    if cases:
        lines.append("历史案例佐证：")
        lines.extend(_case_line(case) for case in cases[:3])
    else:
        lines.append("历史案例佐证：无命中（知识腿未装配或召不回，此处不臆造经验）")
    return "\n".join(lines)


def _case_line(case: dict[str, Any]) -> str:
    label = case.get("title") or case.get("case_id") or case.get("source") or "案例"
    summary = str(case.get("summary") or case.get("text") or "")[:160]
    return f"- {label}：{summary}"


def narrate_result(action: str, result: dict[str, Any]) -> str:
    if "error" in result:
        return str(result["error"])
    if result.get("found") is False:
        # 「没找到」必须说出口。`_brief` 会滤掉假值字段，于是 explain 落空时那句话只剩
        # `warning_id=wrn_…`——读起来像查到了，而 `found: False` 这个事实被丢了（真机量过）。
        ident = str(result.get("warning_id") or result.get("trace_id") or "").strip()
        note = str(result.get("note") or "").strip()
        reason = note or "预警与链路的读视图只保留本进程最近若干条"
        return f"没找到 {ident}：{reason}" if ident else f"没找到这条记录：{reason}"
    if action == "query.metrics":
        metrics = result.get("metrics") or {}
        # 键名中性（p50/p95）、单位随 payload：这里若还按 `p50_ms` 读，秒制那几条会静默变 None
        lines = [
            f"{name}：n={stats.get('count')} p50={stats.get('p50')}{stats.get('unit')} "
            f"p95={stats.get('p95')}{stats.get('unit')} 违约={stats.get('breaches', 0)}"
            for name, stats in metrics.items()
            if isinstance(stats, dict)
        ]
        return "\n".join(lines) or "尚无时延样本"
    if action == "query.agents":
        agents = result.get("agents") or {}
        items = agents.get("items") or []
        return f"在线智能体 {agents.get('online', len(items))} 个：" + "、".join(
            str(item.get("agent_id", item)) for item in items[:10] if isinstance(item, dict)
        )
    if action in {"query.warnings", "query.chains", "query.stations"}:
        items = result.get("items") or []
        if not items:
            return "没有查到记录（读视图为空）"
        return f"共 {result.get('count', len(items))} 条，前 {len(items)} 条：" + "\n" + "\n".join(_brief(item) for item in items)
    if action == "query.tasks":
        task = result.get("task")
        return "未找到该任务单元" if task is None else _brief(task)
    if action == "run.drill":
        return "演练已完成：" + _brief(result.get("drill") or {})
    if action == "create.report":
        return "上报已受理：" + _brief(result.get("report") or {})
    if action == "list.actions":
        return "可用动作：" + "、".join(str(item.get("title")) for item in result.get("actions") or [])
    return _brief(result)


def _brief(item: Any) -> str:
    if not isinstance(item, dict):
        return str(item)[:200]
    keys = (
        "warning_id",
        "event_id",
        "trace_id",
        "task_unit_id",
        "station_id",
        "agent_id",
        "hazard_type",
        "risk_level",
        "region_code",
        "title_zh",
        "objective",
        "status",
        "ok",
    )
    return "；".join(f"{key}={item[key]}" for key in keys if item.get(key) not in (None, "", [], {}))[:400]


def _trim_metrics(report: dict[str, Any]) -> dict[str, Any]:
    """指标答复只给分位数与违约数，不给 store 快照：状态面里的东西不必全塞进对话框。"""
    metrics = report.get("metrics") if isinstance(report, dict) else None
    trimmed: dict[str, Any] = {}
    for name, stats in (metrics or {}).items():
        if isinstance(stats, dict):
            trimmed[name] = {key: stats[key] for key in ("count", "unit", "p50", "p95", "p99", "max", "breaches") if key in stats}
    trimmed["collaboration"] = report.get("collaboration") if isinstance(report, dict) else None
    return trimmed


def _numbers(text: str) -> set[str]:
    return set(re.findall(r"\d+(?:\.\d+)?", text))


async def _call(fn: Callable[..., Any] | None, *args: Any, **kwargs: Any) -> Any:
    if fn is None:
        raise RuntimeError("能力未装配")
    result = fn(*args, **kwargs)
    return await result if isinstance(result, Awaitable) else result


__all__ = [
    "ACTION_SPECS",
    "ACTION_WHITELIST",
    "AssistantActions",
    "AssistantEvent",
    "AssistantService",
    "Intent",
    "narrate_chain",
    "narrate_plan",
    "narrate_result",
    "narrate_warning",
]
