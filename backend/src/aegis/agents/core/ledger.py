"""LLM 调用台账：一次调用的可审计事实（模型/prompt版本/token/时延/校验态）。

台账是**跨切关注点**：平台网关与灾种模块都要落账，落账的形状就必须只有一份——
`scripts/quality_gate.py` 的台账文件三道形状闸（每行必须带 `structured`/`attempt`/
`validation`）就是按这份形状写的，字段一改闸门会**静默关掉**，所以字段名不许动。

放在 L3 的 `agents/core/`（而不是 L4 的 `services/llm/`）：灾种模块的边界门禁是
白名单制，共享件必须落在两侧都能依赖的契约面里（ADR-0017）。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Any

from aegis.observability.tracer import Tracer


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


__all__ = ["LlmCallRecord"]


class LlmUsageLedger:
    """调用台账：环形上限 + 可选 tracer 埋点。

    平台网关按实例持一本（随网关走），灾种模块用模块级共享的一本（provider 是进程级
    复用的）。**实现只有这一份**——台账行的形状、首过率的原料口径都必须一致，
    否则 `scripts/quality_gate.py` 的首过率在两侧就不是同一个数。
    """

    def __init__(self, *, limit: int = 10_000, tracer: Tracer | None = None) -> None:
        self._rows: deque[LlmCallRecord] = deque(maxlen=max(16, int(limit)))
        self._tracer = tracer

    def record(self, rec: LlmCallRecord) -> None:
        self._rows.append(rec)
        if self._tracer is not None:
            self._tracer.record("llm_call_ms", rec.latency_ms, trace_id=rec.trace_id, agent_id=rec.agent_id)

    def records(self) -> list[LlmCallRecord]:
        return list(self._rows)


#: 灾种 provider 用的模块级台账（provider 是进程级复用的，台账也该是）。
_SHARED = LlmUsageLedger()


def shared_ledger() -> LlmUsageLedger:
    """进程级共享台账（灾种 provider 落账用）。"""
    return _SHARED


def record_call(rec: LlmCallRecord) -> None:
    """往进程级共享台账落一行（灾种 provider 用）。"""
    _SHARED.record(rec)


def reset_shared_ledger() -> None:
    """清空进程级共享台账（测试用：模块级全局态，用例之间必须还原，否则漏进聚合断言）。"""
    _SHARED._rows.clear()
