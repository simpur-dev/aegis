"""意图判定：把一句话变成"哪个动作 + 什么参数"。

三条口径（沿用并强化旧实现）：
1. **规则优先，LLM 兜底**。规则判得动就不问模型——省一次调用，也少一次可被注入的面。
2. **LLM 只有选择权**。它只能在注册表里挑一个动作名；挑表外的、或让我们"直接执行"的，一律拒绝并留痕。
3. **越权不等于降级**。"把全网预警都删掉"不能因为含"预警"二字就被 `warning.list` 接住回一份清单；
   它要得到一个明确的"该动作未开放/角色不足"，而不是一次听懂了似的回避（旧实现的问题）。

`classify` 不做权限判定——角色是否足够由执行器统一判，这里只负责"这句想干什么"。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field

from aegis.services.assistant.registry import ActionRegistry
from aegis.services.interpret.semantic_parser import detect_hazard, extract_region

_WARNING_ID_RE = re.compile(r"wrn_[0-9a-f]{6,24}")
_TRACE_ID_RE = re.compile(r"trc_[0-9a-f]{16}")
_EVENT_ID_RE = re.compile(r"evt_[0-9a-f]{12}")
_TASK_ID_RE = re.compile(r"stu_[0-9a-f]{16}")
_TICKS_RE = re.compile(r"(\d+)\s*轮")
_LEVEL_RE = re.compile(r"([1-4])\s*级")
_MINUTES_RE = re.compile(r"(\d+)\s*分钟")
_TASK_CONTROL_ACTIONS = frozenset({"task.claim", "task.ack", "task.assign", "task.transfer", "task.close"})
_ASSIGNEE_RE = re.compile(r"(?:派给|指派给|转给|移交给)\s*([A-Za-z0-9_-]{4,32})")
_ACTION_ID_RE = re.compile(r"act_[0-9a-f]{6,24}")
_MENTION_RE = re.compile(r"@([A-Za-z0-9_-]{4,32})")
_WARNING_CONTROL_ACTIONS = frozenset(
    {
        "warning.publish",
        "warning.revoke",
        "warning.change_level",
        "warning.silence",
        "warning.resend",
    }
)
#: "全网/全部/都"这类整体作用域词。写动作一旦带上它们，就不再是"对某一条做点什么"。
_BULK_SCOPE_RE = re.compile(r"全网|全部|所有|每一条|每个|都")
#: 一句里点名了具体对象（预警/任务/动作编号）——有编号才谈得上单条处置。
_TARGET_ID_RE = re.compile(r"wrn_[0-9a-f]{6,24}|stu_[0-9a-f]{16}|act_[0-9a-f]{6,24}")

#: 意图词表：顺序即优先级（先具体后宽泛），"解释 wrn_xxx 为什么是红色"不能被查询意图抢走。
_INTENT_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("warning.explain", ("为什么", "依据", "凭什么", "解释", "怎么定的")),
    ("chain.explain", ("链路追踪", "谁判的", "每段耗时", "段时延")),
    ("metric.report", ("指标", "时延", "成功率", "sla", "p95", "违约")),
    ("agent.list", ("智能体", "agent", "在线", "注册")),
    ("station.list", ("站点", "台站", "监测站", "台账")),
    ("task.list", ("任务单元", "stu", "任务清单")),
    ("chain.list", ("链路", "事件列表", "最近事件")),
    ("warning.list", ("预警", "发布", "告警")),
    ("notify.lead", ("通知值班长", "叫值班长")),
    ("notify.group", ("群发通知", "同时通知")),
    ("action.status", ("动作状态", "动作进度")),
    ("plan.ask", ("预案", "处置方案", "怎么办", "怎么处置", "要不要转移", "转移路线")),
    ("drill.start", ("演练", "模拟一次", "跑一次", "surge")),
    ("report.submit", ("上报", "报告险情", "我要报", "我发现", "我看到", "刚看到")),
)

#: 祈使式前缀：只有"把/请/帮我…"或整体作用域才算"要执行"，否则"这条预警可以撤回吗"会被误杀。
_IMPERATIVE_PREFIX = re.compile(r"^(把|请|帮我|我们|立即|马上|现在|快)")

#: 写动作动词 → 目标动作名。命中的动作若已注册，就按正常意图继续解析参数；
#: 未注册时才由 `AssistantService.respond` 明确回"该动作未开放"，而不是含糊地接一份清单。
_VERB_ACTIONS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("删除", "删掉", "清空", "撤回", "撤销", "撤掉"), "warning.revoke"),
    (("直接发布", "立即发布", "发出去", "发布出去"), "warning.publish"),
    (("改级", "调整等级", "调整为", "改成", "降为", "升为"), "warning.change_level"),
    (("静默", "免打扰"), "warning.silence"),
    (("重发", "再发一遍"), "warning.resend"),
    (("改阈值", "修改阈值", "调整阈值"), "rule.update"),
    (("派单", "派给", "指派给"), "task.assign"),
    (("移交", "转给"), "task.transfer"),
    (("认领", "接手这个任务"), "task.claim"),
    (("确认完成", "处置完成"), "task.ack"),
    (("结单", "关闭任务"), "task.close"),
    (("打开阀门", "关闭阀门", "设备控制"), "device.control"),
)


@dataclass(frozen=True, slots=True)
class Intent:
    action: str | None
    args: dict[str, Any] = field(default_factory=dict)
    confidence: float = 1.0
    decided_by: str = "rule"
    note: str = ""
    #: 被拒的动作名（LLM 建议了注册表外的动作时填）。编排层据此留痕，不静默丢弃。
    rejected: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "args": {key: (value[:120] if isinstance(value, str) else value) for key, value in self.args.items()},
            "confidence": round(self.confidence, 4),
            "decided_by": self.decided_by,
            "note": self.note,
        }


class PlanStep(BaseModel):
    """意图解构产出的单个计划步；动作必须落在注册表内，越权动作在规划期即被剔除。"""

    model_config = {"extra": "forbid"}

    action: str
    reason: str = Field(default="", max_length=200)


class PlanDecomposition(BaseModel):
    model_config = {"extra": "forbid"}

    steps: list[PlanStep] = Field(default_factory=list)


def out_of_scope_action(text: str) -> tuple[str, str] | None:
    """命中"要执行某个写动作"时返回 (目标动作名, 命中的动词)，否则 None。

    只判祈使式或整体作用域：`"这条预警可以撤回吗"` 是问句，不是指令，不能闸。
    """
    body = str(text or "").strip()
    imperative = bool(_IMPERATIVE_PREFIX.search(body)) or any(word in body for word in ("全部", "所有", "全网", "都"))
    if not imperative:
        return None
    for verbs, action in _VERB_ACTIONS:
        for verb in verbs:
            if verb in body:
                return action, verb
    return None


def bulk_scope_without_target(text: str) -> bool:
    """整体作用域的写指令，且一句里没点名任何具体编号 → 超出任何单条动作的能力边界。

    助手只做操作者本来能做的事，不放大权限：`warning.revoke` 撤销的是**一条**预警，
    "把全网预警都删掉"是抹掉整张表，没有哪个角色本来就有这个能力。动作注册与否都不改变
    这一点，因此越权闸不能只看注册表里有没有这个名字。
    """
    body = str(text or "").strip()
    return bool(_BULK_SCOPE_RE.search(body)) and not bool(_TARGET_ID_RE.search(body))


class IntentClassifier:
    """规则 + LLM 的意图判定器。`registry` 只用来校验"这个动作存不存在"。"""

    def __init__(self, registry: ActionRegistry, *, llm: Any | None = None) -> None:
        self._registry = registry
        self._llm = llm

    @property
    def llm_available(self) -> bool:
        return bool(self._llm is not None and getattr(self._llm, "available", False))

    def classify(
        self,
        text: str,
        *,
        region_code: str | None = None,
        hazard_hint: str | None = None,
        reporter: str | None = None,
    ) -> Intent:
        body = str(text or "").strip()
        matched = self._match(body)
        if matched is not None:
            return self._build(matched, body, region_code=region_code, hazard_hint=hazard_hint, reporter=reporter, decided_by="rule")
        return Intent(action=None, confidence=0.0, decided_by="none", note="未能识别意图")

    async def classify_async(
        self,
        text: str,
        *,
        region_code: str | None = None,
        hazard_hint: str | None = None,
        reporter: str | None = None,
    ) -> Intent:
        intent = self.classify(text, region_code=region_code, hazard_hint=hazard_hint, reporter=reporter)
        if intent.action is not None or not self.llm_available:
            return intent
        suggested, rejected = await self._llm_action(text)
        if suggested is None:
            if rejected:
                return Intent(
                    action=None,
                    confidence=0.0,
                    decided_by="llm",
                    note=f"建议动作 {rejected} 未注册，已拒绝",
                    rejected=rejected,
                )
            return intent
        return self._build(suggested, text, region_code=region_code, hazard_hint=hazard_hint, reporter=reporter, decided_by="llm")

    # ---------- 规则 ----------

    def _match(self, text: str) -> str | None:
        lowered = text.lower()
        if _WARNING_ID_RE.search(text):
            # 带编号的祈使句是「对这条预警做点什么」，不能被解释意图抢走。
            verb = out_of_scope_action(text)
            if verb is not None and self._registry.get(verb[0]) is not None:
                return verb[0]
            return "warning.explain"
        if _TRACE_ID_RE.search(text) or _EVENT_ID_RE.search(text):
            return "chain.explain"
        if _TASK_ID_RE.search(text):
            verb = out_of_scope_action(text)
            if verb is not None and verb[0].startswith("task.") and self._registry.get(verb[0]) is not None:
                return verb[0]
            return "task.get"
        for action, keywords in _INTENT_RULES:
            if any(keyword in lowered for keyword in keywords):
                return action
        return None

    def _build(
        self,
        action: str,
        text: str,
        *,
        region_code: str | None,
        hazard_hint: str | None,
        reporter: str | None,
        decided_by: str,
    ) -> Intent:
        if self._registry.get(action) is None:
            return Intent(action=None, confidence=0.0, decided_by=decided_by, note=f"动作 {action} 未开放")
        args: dict[str, Any] = {}
        if action == "warning.explain" and (found := _WARNING_ID_RE.search(text)):
            args["warning_id"] = found.group()
        if action == "chain.explain":
            trace = _TRACE_ID_RE.search(text)
            event = _EVENT_ID_RE.search(text)
            if trace:
                args["trace_id"] = trace.group()
            elif event:
                args["event_id"] = event.group()
        if action == "task.get" and (task := _TASK_ID_RE.search(text)):
            args["task_unit_id"] = task.group()
        if action == "action.status" and (found := _ACTION_ID_RE.search(text)):
            args["action_id"] = found.group()
        if action in {"notify.lead", "notify.group"}:
            args["message"] = text[:500]
        if action == "notify.group":
            args["operator_ids"] = _MENTION_RE.findall(text)[:20]
        if action in _TASK_CONTROL_ACTIONS and (found := _TASK_ID_RE.search(text)):
            args["task_id"] = found.group()
        if action in {"task.assign", "task.transfer"} and (who := _ASSIGNEE_RE.search(text)):
            args["assignee"] = who.group(1)
        if action in _WARNING_CONTROL_ACTIONS and (found := _WARNING_ID_RE.search(text)):
            args["warning_id"] = found.group()
        if action == "warning.change_level" and (level := _LEVEL_RE.search(text)):
            args["level"] = int(level.group(1))
        if action == "warning.silence" and (minutes := _MINUTES_RE.search(text)):
            args["minutes"] = max(1, min(int(minutes.group(1)), 240))
        if action == "warning.list":
            args["limit"] = 10
            if region := extract_region(text, default=region_code):
                args["region_code"] = region
            if "已发布" in text or "发布过" in text:
                args["released"] = True
            elif "未发布" in text or "草稿" in text:
                args["released"] = False
        if action == "task.list":
            args["limit"] = 20
            if region := extract_region(text, default=region_code):
                args["region_code"] = region
        if action == "station.list":
            args["limit"] = 20
            if region := extract_region(text, default=region_code):
                args["region_code"] = region
        if action == "chain.list":
            args["limit"] = 8
        if action == "plan.ask":
            hazard = detect_hazard(text, hint=hazard_hint)
            args["hazard_type"] = None if hazard is None else hazard.value
            args["query"] = text[:500]
            if region := extract_region(text, default=region_code):
                args["region_code"] = region
        if action == "drill.start":
            args["scenario"] = "normal" if ("normal" in text.lower() or "正常" in text) else "surge"
            ticks = _TICKS_RE.search(text)
            args["ticks"] = max(1, min(5, int(ticks.group(1)))) if ticks else 2
            if region := extract_region(text, default=region_code):
                args["region_code"] = region
        if action == "report.submit":
            hazard = detect_hazard(text, hint=hazard_hint)
            args["note"] = text[:2_000]
            args["reporter"] = (reporter or "web-assistant")[:64]
            args["hazard_type"] = None if hazard is None else hazard.value
            args["region_code"] = extract_region(text, default=region_code) or region_code or ""
        confidence = 1.0 if decided_by == "rule" else 0.6
        missing = _missing_target(action, args)
        if missing is not None:
            # 缺 ID 不是"执行失败"而是"没法执行"：直接给出这个 ID 在哪个页面读得到，
            # 别让建议芯片点下去只得到一句 422。
            return Intent(action=None, confidence=0.0, decided_by=decided_by, note=missing)
        return Intent(action=action, args=args, confidence=confidence, decided_by=decided_by)

    # ---------- LLM 兜底 ----------

    async def _llm_action(self, text: str) -> tuple[str | None, str | None]:
        """返回 (动作名, 被拒的动作名)。被拒要带出去留痕，不能静默当成"没听懂"。"""
        llm = self._llm
        if llm is None:
            return None, None
        try:
            raw = await llm.chat_json(
                [
                    {
                        "role": "system",
                        "content": (
                            '只输出 JSON：{"action": 下列之一, "reason": 一句话}。'
                            f"可选动作：{sorted(self._registry.names)}。不得虚构其它动作。"
                        ),
                    },
                    {"role": "user", "content": text[:600]},
                ]
            )
        except Exception:
            return None, None
        action = str(raw.get("action", "")).strip()
        return (action, None) if self._registry.get(action) is not None else (None, action or "<empty>")

    async def propose_plan(self, text: str, *, region_code: str | None = None, hazard_hint: str | None = None) -> dict[str, Any]:
        """多步解构：LLM 只有规划权，产出仍是"动作 + 参数"，一律不执行。"""
        llm = self._llm
        if llm is None or not self.llm_available:
            return {"steps": [], "decided_by": "none", "note": "意图解构需要已配置的 LLM；未配置则不产出多步计划（不臆造）。"}
        try:
            decomp = await llm.chat_structured(
                [],
                schema=PlanDecomposition,
                task="assistant",
                prompt_key="assistant",
                prompt_vars={"text": text[:600], "actions": str(sorted(self._registry.names))},
                temperature=0.1,
            )
        except Exception as exc:
            return {"steps": [], "decided_by": "none", "note": f"解构降级：{type(exc).__name__}"}

        steps: list[dict[str, Any]] = []
        rejected: list[str] = []
        for step in decomp.steps:
            if self._registry.get(step.action) is None:
                rejected.append(step.action or "<empty>")
                continue
            intent = self._build(step.action, text, region_code=region_code, hazard_hint=hazard_hint, reporter=None, decided_by="llm")
            entry = intent.as_dict()
            entry["requires_confirmation"] = self._registry.require(step.action).spec.needs_confirm
            if step.reason:
                entry["reason"] = step.reason
            steps.append(entry)
        return {
            "steps": steps,
            "rejected": rejected[:8],
            "decided_by": "llm",
            "note": "LLM 仅有规划权；执行类动作仍逐个过人工确认闸，本接口不触发任何执行。",
        }


def _missing_target(action: str, args: dict[str, Any]) -> str | None:
    """带标识符的动作缺 ID 时返回指路文案：告诉用户这个 ID 在哪个页面读得到。"""
    if action == "warning.explain" and not args.get("warning_id"):
        return "缺少预警编号（wrn_…）：编号在「预警发布」页点「详情」后，抽屉顶部的「预警标识」"
    if action == "chain.explain" and not (args.get("trace_id") or args.get("event_id")):
        return "缺少链路标识（trc_/evt_…）：链路号在总览页「链路执行记录」的第一列"
    if action == "action.status" and not args.get("action_id"):
        return "缺少动作编号（act_…）：编号在待确认卡片的「动作标识」里"
    if action == "notify.group" and not args.get("operator_ids"):
        return "缺少收件人：请说「群发通知 @lead-01 @lead-02 …」"
    if action in _TASK_CONTROL_ACTIONS and not args.get("task_id"):
        return "缺少任务 ID（stu_…）：ID 在处置空间的任务清单里，也可以先查最近链路"
    if action in {"task.assign", "task.transfer"} and not args.get("assignee"):
        return "缺少受理人席位 ID：请说「派给 lead-01」或「转给 lead-02」"
    if action in _WARNING_CONTROL_ACTIONS and not args.get("warning_id"):
        return "缺少预警编号（wrn_…）：编号在「预警发布」页点「详情」后，抽屉顶部的「预警标识」"
    if action == "warning.change_level" and not args.get("level"):
        return "缺少目标等级：请说「调整为 2 级」（1..4 级，1 级最高）"
    if action == "task.get" and not args.get("task_unit_id"):
        return "缺少任务单元 ID（stu_…）：ID 在「预警发布」页「详情」里的「关联任务单元」表，也可以先查最近链路"
    return None
