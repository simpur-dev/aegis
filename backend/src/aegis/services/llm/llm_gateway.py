"""LLM 网关 v2（§4.1 / D3）：MA 阶段一切 LLM 环节的地基。

在升级前的 129 行单模型网关之上补齐六项能力，同时**逐字保留**旧对外口径
（`chat`/`chat_json`/`translate`/`polish_warning`/`available`/`model`/`build_gateway_if_configured`），
让既有消费方（WarningService 翻译腿、DisasterTextParser 语义腿、AssistantService）零改动。

1. 多模型路由：按 task key 从 `Settings.llm_models` 选模型与生成参数；未配的 task 继承全局 `llm_model`。
2. 结构化输出：`chat_structured(messages, schema)` —— json 模式 + Pydantic 校验 + 一次修复重试，
   仍不合规即抛 `MalformedLlmOutputError`，交由调用方走既有降级轨（每次尝试各落一条台账，首过率由
   `quality_eval.structured_first_pass_rate` 从台账算、进质量表第五列，§4.5/D8；首批实测见 REPORT 的 MB-4 节）。
3. 流式：`chat_stream` 异步产出增量文本（assistant token 帧 / execute 文案进度用，可选）。
4. 可观测：每次调用记进 Tracer 时延账本（`llm_call_ms`，按 trace_id/agent_id 归因，直通 /metrics），
   并落一份用量台账（模型/prompt版本/token/校验态），成本与时延按 agent×model 归因。
5. 熔断与降级链：provider 连续失败达阈值即判定该模型熔断并切备用模型；整链不可用抛
   `LlmUnavailableError`——LLM 不可用 ≠ 平台不可用（消费方据此走确定性降级）。
6. Prompt 注册表：`prompt_key` 经 `PromptRegistry` 装配消息并把版本号写进用量台账，保证结论可复现（D10）。

纪律：本文件所有路由模型/预算数字来自方案 §3.0 表，均为**未实测**初值，不据此声称达标；
未配置密钥时 `available=False`，绝不用假数据把"没接上"读成"能跑"。
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from aegis.config import LlmModelRoute, Settings, get_settings
from aegis.domain.messages import now_iso
from aegis.errors import AegisError, ErrorCode
from aegis.observability.tracer import Tracer, percentile
from aegis.services.llm.prompt_registry import PromptRegistry
from aegis.services.llm.quality_eval import structured_first_pass_rate

log = logging.getLogger("aegis.services.llm")

_FENCE_OPEN = re.compile(r"^```(?:json)?\s*\n?", re.IGNORECASE)
_FENCE_CLOSE = re.compile(r"\n?```\s*$")
_THINK_BLOCK = re.compile(r"<think>[\s\S]*?</think>")

SchemaT = TypeVar("SchemaT", bound=BaseModel)


class LlmUnavailableError(AegisError):
    code = ErrorCode.NOT_READY


class MalformedLlmOutputError(AegisError):
    code = ErrorCode.INTERNAL


@dataclass(slots=True)
class LlmCallRecord:
    """一次调用的可审计事实（对齐 D1"可审计"：模型/prompt版本/token/时延/校验态）。"""

    task: str
    model: str
    prompt_version: str | None
    temperature: float
    max_tokens: int
    latency_ms: float
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    validation: str  # ok | malformed | transport_error（整链不可用不落行：它在调用之前就抛了）
    trace_id: str | None
    agent_id: str | None
    error: str | None = None
    at: str = ""  # UTC ISO，成本按月归因用（§4.8.3）
    #: 首过率的原料（§4.1.2/§8）：结构化调用的每一次尝试各落一条，`attempt=0` 就是"第一次答"。
    #: 没有这两格，台账里"修复轮过了"和"首答就过"长得一模一样，"首过率"就只能靠声称。
    structured: bool = False
    attempt: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "task": self.task,
            "model": self.model,
            "prompt_version": self.prompt_version,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "latency_ms": round(self.latency_ms, 3),
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "validation": self.validation,
            "trace_id": self.trace_id,
            "agent_id": self.agent_id,
            "error": self.error,
            "at": self.at,
            "structured": self.structured,
            "attempt": self.attempt,
        }


class LlmGateway:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        client: Any | None = None,
        timeout_seconds: float | None = None,
        max_tokens: int | None = None,
        settings: Settings | None = None,
        tracer: Tracer | None = None,
        prompts: PromptRegistry | None = None,
        routes: dict[str, LlmModelRoute] | None = None,
    ) -> None:
        cfg = settings or get_settings()
        self._settings = cfg
        self._api_key = api_key if api_key is not None else cfg.llm_api_key
        self._base_url = base_url or cfg.llm_base_url
        # 全局默认模型：既是 `.model` 属性的口径，也是未配路由 task 的继承源（保持旧单模型行为）。
        self._model = model or cfg.llm_model
        self._timeout = timeout_seconds if timeout_seconds is not None else cfg.llm_timeout_seconds
        self._max_tokens = max_tokens  # None 表示不覆盖，交给路由与 task 决定
        self._tracer = tracer
        self._prompts = prompts if prompts is not None else PromptRegistry.builtin()
        self._routes: dict[str, LlmModelRoute] = dict(routes) if routes is not None else dict(cfg.llm_models)
        self._client = client

        if self._client is None and self._api_key:
            from openai import AsyncOpenAI

            self._client = AsyncOpenAI(api_key=self._api_key, base_url=self._base_url, timeout=self._timeout)

        # provider 级熔断账：连续失败计数与开闸标记按模型名分列。
        self._breaker_threshold = max(1, cfg.llm_breaker_fail_threshold)
        self._failures: dict[str, int] = {}
        self._open: dict[str, bool] = {}
        self._usage: deque[LlmCallRecord] = deque(maxlen=max(16, cfg.llm_usage_ledger_limit))

    # ---------- 对外口径（与升级前逐字一致） ----------

    @property
    def available(self) -> bool:
        return self._client is not None

    @property
    def model(self) -> str:
        return self._model

    # ---------- 路由解析 ----------

    def _route_for(self, task: str) -> LlmModelRoute:
        route = self._routes.get(task)
        if route is not None:
            return route
        # 未登记的 task：回落到 default 的参数，但模型仍继承全局（不臆造模型名）。
        return self._routes.get("default") or LlmModelRoute()

    def _primary_model(self, task: str) -> str:
        route = self._route_for(task)
        return route.model or self._model

    def _candidates(self, task: str) -> list[str]:
        route = self._route_for(task)
        primary = self._primary_model(task)
        out = [primary]
        if route.fallback_model and route.fallback_model != primary:
            out.append(route.fallback_model)
        return out

    def _breaker_open(self, model_name: str) -> bool:
        return bool(self._open.get(model_name))

    def _note_failure(self, model_name: str) -> None:
        self._failures[model_name] = self._failures.get(model_name, 0) + 1
        if self._failures[model_name] >= self._breaker_threshold:
            self._open[model_name] = True
            log.warning("LLM 模型熔断", extra={"model": model_name, "failures": self._failures[model_name]})

    def _note_success(self, model_name: str) -> None:
        if model_name in self._failures:
            self._failures.pop(model_name, None)
        if self._open.get(model_name):
            self._open[model_name] = False

    # ---------- 可观测与用量 ----------

    def _record(self, rec: LlmCallRecord) -> None:
        self._usage.append(rec)
        if self._tracer is not None:
            self._tracer.record("llm_call_ms", rec.latency_ms, trace_id=rec.trace_id, agent_id=rec.agent_id)

    def usage_records(self) -> list[LlmCallRecord]:
        return list(self._usage)

    def current_month(self) -> str:
        return now_iso()[:7]  # "YYYY-MM"

    def monthly_token_usage_by_agent(self, month: str) -> dict[str, int]:
        out: dict[str, int] = {}
        for rec in self._usage:
            if rec.agent_id and rec.at[:7] == month:
                out[rec.agent_id] = out.get(rec.agent_id, 0) + rec.total_tokens
        return out

    def budget_alerts(self, month: str) -> list[dict[str, Any]]:
        """§4.8.3 成本治理：按 agent_id 月度预算，超限【只告警、绝不熔断】（熔断给故障，不给钱）。"""
        budget = self._settings.llm_monthly_token_budget_default
        if budget <= 0:
            return []
        alerts: list[dict[str, Any]] = []
        for agent_id, tokens in sorted(self.monthly_token_usage_by_agent(month).items()):
            if tokens > budget:
                alerts.append(
                    {
                        "agent_id": agent_id,
                        "month": month,
                        "tokens": tokens,
                        "budget": budget,
                        "over_by": tokens - budget,
                        "action": "alert_only",
                        "note": "超限只告警不熔断：钱的问题交预算/审批，不给假故障。",
                    }
                )
        return alerts

    def first_pass_stats(self) -> dict[str, Any]:
        """结构化输出首过率（§4.1.2/§8）：聚合自真实调用台账，一次都没跑则 rate=None（未评测，不冒满分）。

        同时按 task 各算一份——总率会被调用量大的那条腿 dominate，逐 task 才看得出是哪条腿在退步。
        """
        rows = [rec.as_dict() for rec in self._usage]
        rate, breakdown = structured_first_pass_rate(rows)
        by_task: dict[str, float | None] = {}
        for task in sorted({str(row.get("task")) for row in rows if row.get("structured")}):
            by_task[task] = structured_first_pass_rate([row for row in rows if row.get("task") == task])[0]
        return {"rate": rate, "ledger_rows": len(rows), "detail": breakdown, "by_task": by_task}

    def usage_snapshot(self) -> dict[str, Any]:
        """按 (agent_id, model) 归因的用量聚合：成本/时延的治理出口（§4.1.4 / §4.8.3）。

        聚合自真实调用台账，不二次采样——报表与验收同源（架构铁律 7）。
        """
        buckets: dict[tuple[str | None, str], dict[str, Any]] = {}
        for rec in self._usage:
            key = (rec.agent_id, rec.model)
            b = buckets.get(key)
            if b is None:
                b = {"calls": 0, "total_tokens": 0, "validation": {}, "latency": []}
                buckets[key] = b
            b["calls"] += 1
            b["total_tokens"] += rec.total_tokens
            b["validation"][rec.validation] = b["validation"].get(rec.validation, 0) + 1
            b["latency"].append(rec.latency_ms)
        rows = []
        for (agent_id, model_name), b in buckets.items():
            lat = sorted(b["latency"])
            rows.append(
                {
                    "agent_id": agent_id,
                    "model": model_name,
                    "calls": b["calls"],
                    "total_tokens": b["total_tokens"],
                    "validation": dict(b["validation"]),
                    "latency_ms": {
                        "p50": round(percentile(lat, 50), 3),
                        "p95": round(percentile(lat, 95), 3),
                        "max": round(lat[-1], 3) if lat else 0.0,
                    },
                }
            )
        rows.sort(key=lambda r: (r["agent_id"] or "", r["model"]))
        month = self.current_month()
        return {
            "by_agent_model": rows,
            "total_calls": len(self._usage),
            "month": month,
            "budget_alerts": self.budget_alerts(month),
            "structured_first_pass": self.first_pass_stats(),
        }

    def model_info_for_task(self, task: str) -> dict[str, Any]:
        """D10：结论侧写进 `model_info` 的可复现凭证（模型/路由/prompt 版本口径）。"""
        route = self._route_for(task)
        prompt_version = self._prompts.version_for_task(task)
        return {
            "model": route.model or self._model,
            "fallback_model": route.fallback_model,
            "task": task,
            "prompt_version": prompt_version,
        }

    # ---------- 调用主通道：路由 + 熔断 + 用量，一切 LLM 请求的唯一出口 ----------

    async def _complete(
        self,
        messages: list[dict[str, str]],
        *,
        task: str,
        temperature: float | None,
        max_tokens: int | None,
        response_format: dict[str, Any] | None,
        trace_id: str | None,
        agent_id: str | None,
        prompt_version: str | None,
        stream: bool = False,
        structured: bool = False,
        attempt: int = 0,
    ) -> tuple[str, Any, str]:
        """返回 (content, response_obj_or_None, model_used)。非流式一次性返回正文；流式由 chat_stream 直接用。

        这里做三件事：候选模型（主+备）逐个尝试、provider 级熔断切备、调用落用量与时延。
        抛法如实分两类，别指望调用方拿到统一类型：未配置或全部候选熔断开 `LlmUnavailableError`
        （这两条在任何一次调用之前，**不落台账行**）；逐个尝试后仍失败则把最后一个 provider 异常原样上抛
        （每次失败各落一行 `transport_error`）。两种都由调用方走确定性降级。
        """
        if not self.available or self._client is None:
            raise LlmUnavailableError("LLM 未配置（缺少 api key 或 client）")
        route = self._route_for(task)
        eff_temperature = route.temperature if temperature is None else temperature
        eff_max_tokens = max_tokens if max_tokens is not None else (self._max_tokens if self._max_tokens is not None else route.max_tokens)
        candidates = self._candidates(task)
        actionable = [m for m in candidates if not self._breaker_open(m)]
        if not actionable:
            raise LlmUnavailableError("LLM 全部候选模型处于熔断状态", detail={"task": task, "candidates": candidates})

        last_error: Exception | None = None
        for model_name in actionable:
            started = time.perf_counter()
            kwargs: dict[str, Any] = {
                "model": model_name,
                "messages": messages,
                "temperature": eff_temperature,
                "max_tokens": eff_max_tokens,
            }
            if response_format is not None:
                kwargs["response_format"] = response_format
            try:
                response = await self._client.chat.completions.create(**kwargs)
            except Exception as exc:  # provider/传输故障：计入熔断，切备用模型
                latency_ms = (time.perf_counter() - started) * 1000
                self._note_failure(model_name)
                last_error = exc
                self._record(
                    LlmCallRecord(
                        task=task,
                        model=model_name,
                        prompt_version=prompt_version,
                        temperature=eff_temperature,
                        max_tokens=eff_max_tokens,
                        latency_ms=latency_ms,
                        prompt_tokens=0,
                        completion_tokens=0,
                        total_tokens=0,
                        validation="transport_error",
                        trace_id=trace_id,
                        agent_id=agent_id,
                        at=now_iso(),
                        structured=structured,
                        attempt=attempt,
                        error=f"{type(exc).__name__}: {str(exc)[:180]}",
                    )
                )
                continue
            latency_ms = (time.perf_counter() - started) * 1000
            self._note_success(model_name)
            usage = getattr(response, "usage", None)
            self._record(
                LlmCallRecord(
                    task=task,
                    model=model_name,
                    prompt_version=prompt_version,
                    temperature=eff_temperature,
                    max_tokens=eff_max_tokens,
                    latency_ms=latency_ms,
                    prompt_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
                    completion_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
                    total_tokens=int(getattr(usage, "total_tokens", 0) or 0),
                    validation="ok",
                    trace_id=trace_id,
                    agent_id=agent_id,
                    at=now_iso(),
                    structured=structured,
                    attempt=attempt,
                )
            )
            return "", response, model_name
        # 所有可尝试候选都失败：抛出最后一个传输异常，保持旧"错误传播"语义（调用方降级）。
        assert last_error is not None
        raise last_error

    async def _chat_raw(
        self,
        messages,
        *,
        task,
        temperature,
        max_tokens,
        response_format,
        trace_id,
        agent_id,
        prompt_version,
        structured: bool = False,
        attempt: int = 0,
    ) -> str:
        _, response, model_used = await self._complete(
            messages,
            task=task,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format=response_format,
            trace_id=trace_id,
            agent_id=agent_id,
            prompt_version=prompt_version,
            structured=structured,
            attempt=attempt,
        )
        content = (response.choices[0].message.content or "").strip()
        content = _THINK_BLOCK.sub("", content).strip()
        if not content:
            # 空内容：把刚记录的用量标记为 malformed，方便评测/审计把这算一次不达标首过。
            self._mark_last_malformed("空内容")
            raise MalformedLlmOutputError("LLM 返回空内容", detail={"model": model_used})
        return content

    def _mark_last_malformed(self, reason: str) -> None:
        if not self._usage:
            return
        last = self._usage[-1]
        last.validation = "malformed"
        last.error = reason[:180]

    # ---------- 公开方法 ----------

    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.3,
        max_tokens: int | None = None,
        task: str = "default",
        trace_id: str | None = None,
        agent_id: str | None = None,
        prompt_version: str | None = None,
    ) -> str:
        # temperature 保留旧默认 0.3；调用方显式传值优先，未传时按 task 路由的 temperature。
        eff_temperature = None if temperature == 0.3 and task != "default" else temperature
        return await self._chat_raw(
            messages,
            task=task,
            temperature=eff_temperature,
            max_tokens=max_tokens,
            response_format=None,
            trace_id=trace_id,
            agent_id=agent_id,
            prompt_version=prompt_version,
        )

    async def chat_json(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.1,
        task: str = "default",
        trace_id: str | None = None,
        agent_id: str | None = None,
        prompt_version: str | None = None,
    ) -> dict[str, Any]:
        raw = await self._chat_raw(
            messages,
            task=task,
            temperature=temperature,
            max_tokens=None,
            response_format=None,
            trace_id=trace_id,
            agent_id=agent_id,
            prompt_version=prompt_version,
        )
        return self._parse_json_object(raw)

    async def chat_structured(
        self,
        messages: list[dict[str, str]],
        *,
        schema: type[SchemaT],
        task: str = "default",
        temperature: float = 0.1,
        repair: int = 1,
        trace_id: str | None = None,
        agent_id: str | None = None,
        prompt_version: str | None = None,
        prompt_key: str | None = None,
        prompt_vars: dict[str, Any] | None = None,
    ) -> SchemaT:
        """结构化输出：json 模式产出 → Pydantic 严格校验 → 不合规一次修复重试 → 仍失败即抛错走降级。

        枚举/必填/类型越界一律拒收（如 1–5 之外的等级），绝不"就近夹取"糊弄下游——
        把非法结论伪装成合法是这一型平台最贵的事故。修复重试只重发一次，不无限重试。

        `prompt_key` 给定则消息由 PromptRegistry 装配（§4.1.6）：正文与版本号同源，
        写进 model_info 的 prompt_version 才真正描述实际发送的提示词（可复现的前提）。
        """
        if prompt_key is not None:
            messages, prompt_version = self._prompts.build(prompt_key, **(prompt_vars or {}))
        elif prompt_version is None:
            prompt_version = self._prompts.version_for_task(task)
        current = list(messages)
        last_error: MalformedLlmOutputError | None = None
        for attempt in range(repair + 1):
            raw = ""
            # 取正文必须在 try 内：`_chat_raw` 对"空内容"抛的正是 MalformedLlmOutputError，
            # 放在 try 外会让这一类不合规**绕过整个修复重试**、直接把这条腿打死
            # （真机实测：deepseek-reasoner 9 次仲裁里 5 次空内容，5 次都没进修复轮，研判整段降级）。
            try:
                raw = await self._chat_raw(
                    current,
                    task=task,
                    temperature=temperature,
                    max_tokens=None,
                    response_format={"type": "json_object"},
                    trace_id=trace_id,
                    agent_id=agent_id,
                    prompt_version=prompt_version,
                    structured=True,
                    attempt=attempt,
                )
                parsed = self._parse_json_object(raw)
                return schema.model_validate(parsed)
            except (MalformedLlmOutputError, ValidationError) as exc:
                reason = str(exc)
                self._mark_last_malformed(reason[:180])
                last_error = MalformedLlmOutputError(
                    "LLM 结构化输出不合规" if attempt < repair else "LLM 结构化输出多次不合规，降级",
                    detail={"task": task, "reason": reason[:200], "attempt": attempt},
                )
                if attempt >= repair:
                    break
                # 修复轮：把上一次的非法输出原样带回，要求只按 schema 纠正格式，不改事实。
                # 上一条是空内容时无从回带（也是 provider 明确不接受的空消息），只重问一次。
                echo = [{"role": "assistant", "content": raw[:4_000]}] if raw else []
                ask = (
                    "上一条没有产出任何内容。请按要求输出且仅输出一个合法 JSON 对象。"
                    if not raw
                    else (
                        "上一条输出不符合要求的 JSON schema。请只修正格式与缺失字段，"
                        "不得新增或改动事实，重新输出且仅输出一个合法 JSON 对象。"
                    )
                )
                current = [*messages, *echo, {"role": "user", "content": ask}]
        assert last_error is not None
        raise last_error

    async def chat_stream(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.3,
        max_tokens: int | None = None,
        task: str = "default",
        trace_id: str | None = None,
        agent_id: str | None = None,
        prompt_version: str | None = None,
    ) -> AsyncIterator[str]:
        """流式产出增量文本；整段用量在生成结束后落一条台账（validation=ok/streamed）。

        流式是可选增强，不改变任何过程帧契约；未配 client 时同样抛 LlmUnavailableError 由上层降级。
        """
        if not self.available or self._client is None:
            raise LlmUnavailableError("LLM 未配置（缺少 api key 或 client）")
        route = self._route_for(task)
        eff_temperature = temperature if temperature != 0.3 or task == "default" else route.temperature
        eff_max_tokens = max_tokens if max_tokens is not None else (self._max_tokens if self._max_tokens is not None else route.max_tokens)
        model_name = self._primary_model(task)
        if self._breaker_open(model_name):
            raise LlmUnavailableError("LLM 流式模型处于熔断状态", detail={"task": task, "model": model_name})
        started = time.perf_counter()
        total_chars = 0
        try:
            stream = await self._client.chat.completions.create(
                model=model_name,
                messages=messages,
                temperature=eff_temperature,
                max_tokens=eff_max_tokens,
                stream=True,
            )
            async for chunk in stream:
                piece = _stream_piece(chunk)
                if piece:
                    total_chars += len(piece)
                    yield piece
        except Exception as exc:
            self._note_failure(model_name)
            self._record(
                LlmCallRecord(
                    task=task,
                    model=model_name,
                    prompt_version=prompt_version,
                    temperature=eff_temperature,
                    max_tokens=eff_max_tokens,
                    latency_ms=(time.perf_counter() - started) * 1000,
                    prompt_tokens=0,
                    completion_tokens=0,
                    total_tokens=0,
                    validation="transport_error",
                    trace_id=trace_id,
                    agent_id=agent_id,
                    at=now_iso(),
                    error=f"{type(exc).__name__}: {str(exc)[:180]}",
                )
            )
            raise
        self._note_success(model_name)
        self._record(
            LlmCallRecord(
                task=task,
                model=model_name,
                prompt_version=prompt_version,
                temperature=eff_temperature,
                max_tokens=eff_max_tokens,
                latency_ms=(time.perf_counter() - started) * 1000,
                prompt_tokens=0,
                completion_tokens=max(0, total_chars // 4),  # 无 usage 帧时的粗估，仅用于相对趋势，不当达标依据
                total_tokens=max(0, total_chars // 4),
                validation="streamed",
                trace_id=trace_id,
                agent_id=agent_id,
                at=now_iso(),
            )
        )

    # ---------- 既有平台用途：翻译 / 润色（保持逐字行为） ----------

    async def translate(
        self,
        text: str,
        *,
        target_language: str = "藏语（卫藏方言，书面语）",
        agent_id: str | None = None,
        trace_id: str | None = None,
    ) -> str:
        """翻译预警正文。失败即抛错，由调用方降级为纯中文并标记待译——不伪造译文。

        `agent_id` 要一路带到用量账本：藏文腿是 execute 在调，token 记在 None 名下会让
        按智能体的预算（§4.8.3）少算这一路。
        """
        if not text.strip():
            raise MalformedLlmOutputError("待译文本为空")
        messages = [
            {"role": "system", "content": "你是应急管理的多语翻译，译文必须忠实、简洁、无添加信息，只输出译文本身。"},
            {"role": "user", "content": f"把下面的中文预警翻译成{target_language}：\n{text}"},
        ]
        return (await self.chat(messages, temperature=0.0, agent_id=agent_id, trace_id=trace_id)).strip()

    async def polish_warning(self, title: str, body: str) -> dict[str, str]:
        """把预警正文压成面向公众的简短表述，输出 JSON：{title, body}。"""
        result = await self.chat_json(
            [
                {"role": "system", "content": "输出 JSON 对象，键为 title 与 body，不得新增事实或等级。"},
                {"role": "user", "content": f"标题：{title}\n正文：{body}\n请改写为面向公众的简明预警文案。"},
            ]
        )
        if "title" not in result or "body" not in result:
            raise MalformedLlmOutputError("润色结果缺少 title/body", detail={"keys": sorted(result)})
        return {"title": str(result["title"])[:120], "body": str(result["body"])[:2_000]}

    # ---------- JSON 解析（与升级前同一口径：剥围栏、拒非对象、坏输入带片段） ----------

    @staticmethod
    def _parse_json_object(raw: str) -> dict[str, Any]:
        cleaned = _FENCE_CLOSE.sub("", _FENCE_OPEN.sub("", raw.strip())).strip()
        try:
            parsed = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            raise MalformedLlmOutputError(
                "LLM 返回的 JSON 无法解析",
                detail={"reason": str(exc), "excerpt": cleaned[:200]},
            ) from exc
        if not isinstance(parsed, dict):
            raise MalformedLlmOutputError("LLM 返回的 JSON 顶层不是对象", detail={"type": type(parsed).__name__})
        return parsed


def _stream_piece(chunk: Any) -> str:
    """从流式分片里取增量文本，容错缺字段：delta 可能是 None 或没有 content。"""
    choices = getattr(chunk, "choices", None)
    if not choices:
        return ""
    delta = getattr(choices[0], "delta", None)
    return (getattr(delta, "content", None) or "") if delta is not None else ""


def build_gateway_if_configured(
    settings: Settings | None = None,
    *,
    tracer: Tracer | None = None,
    prompts: PromptRegistry | None = None,
) -> LlmGateway | None:
    """配置了密钥才返回实例；否则返回 None，让平台走无 LLM 的确定性路径。"""
    cfg = settings or get_settings()
    if not cfg.llm_api_key:
        return None
    return LlmGateway(settings=cfg, tracer=tracer, prompts=prompts)
