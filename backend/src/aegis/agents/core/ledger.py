"""LLM 调用台账：一次调用的可审计事实（模型/prompt版本/token/时延/校验态）。

台账是**跨切关注点**：平台网关与灾种模块都要落账，落账的形状就必须只有一份——
`scripts/quality_gate.py` 的台账文件三道形状闸（每行必须带 `structured`/`attempt`/
`validation`）就是按这份形状写的，字段一改闸门会**静默关掉**，所以字段名不许动。

放在 L3 的 `agents/core/`（而不是 L4 的 `services/llm/`）：灾种模块的边界门禁是
白名单制，共享件必须落在两侧都能依赖的契约面里（ADR-0017）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


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
