"""链路产物：`ChainResult` / `StageResult` 与 D10 凭证转记。

这是**对外口径**（API、前端 `types.ts`、`chain_runs` 落库映射、报表都读它），
D7 编排归一明确要求"退役 chain 之后这些形状不变"，所以把它单独成模块：
段实现（`stages.py`）与编排器（`workflow_chain.py`）、工作流节点共用同一份产物定义，
谁都不许另立一个"看起来一样"的结果类。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from aegis.domain.messages import AgentMessage, StandardizedTaskUnit, TriggerHit, WarningRecord
from aegis.services.risk_engine import RiskVerdict

#: 五段的名字与顺序。名字本身是契约：指标样本名由 `f"stage_{name}_ms"` 派生，
#: 改一个字符串就等于悄悄改了指标准径（见 `integrations.chain_facts`）。
STAGE_ORDER = ("perceive", "assess", "plan", "execute", "feedback")


@dataclass(slots=True)
class StageResult:
    name: str
    mode: str  # agent | local | hybrid | skipped
    ok: bool
    latency_ms: float
    note: str = ""
    #: D10 的可审计面：这一段结论/文案是哪个模型、哪版 prompt 产的（智能体没带凭证就是 None，平台不代填）。
    #: 只在有值时进 `as_dict()`——本地降级腿"没有模型参与"这件事不该被写成 `model_info: null` 让读的人猜。
    model_info: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        data = {"name": self.name, "mode": self.mode, "ok": self.ok, "latency_ms": round(self.latency_ms, 3), "note": self.note}
        if self.model_info is not None:
            data["model_info"] = self.model_info
        return data


@dataclass(slots=True)
class ChainResult:
    trace_id: str
    event_id: str
    stages: list[StageResult] = field(default_factory=list)
    hits: list[TriggerHit] = field(default_factory=list)
    verdict: RiskVerdict | None = None
    task_units: list[StandardizedTaskUnit] = field(default_factory=list)
    warning: WarningRecord | None = None
    errors: list[str] = field(default_factory=list)
    degradations: list[str] = field(default_factory=list)
    # 本次规划参考过的历史案例 id：可解释性证据，不是"提示词装饰"——报告里要能回答"凭什么这么判"。
    reference_cases: list[str] = field(default_factory=list)
    # 本次规划注入的检索上下文块 id：与案例召回分开记账，因为两者的语料面不同（案例库 vs 历史任务单元）
    context_docs: list[str] = field(default_factory=list)
    #: 各段应答携带的模型凭证（段名 → 凭证）。三段的生产者是三个 helper，而段的记账在主流程里，
    #: 两者之间要有明确中转，而不是让 helper 去改 stages。
    stage_models: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """链路成功 = 各段执行成功且无降级错误。无触发时同样成功（未告警是正确结果）。"""
        return all(s.ok for s in self.stages) and not self.errors

    @property
    def acted(self) -> bool:
        return self.verdict is not None

    def stage(self, name: str) -> StageResult | None:
        return next((s for s in self.stages if s.name == name), None)

    def as_dict(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "event_id": self.event_id,
            "ok": self.ok,
            "acted": self.acted,
            "stages": [s.as_dict() for s in self.stages],
            "risk": self.verdict.as_payload() if self.verdict else None,
            "task_units": [u.task_unit_id for u in self.task_units],
            "warning_id": self.warning.warning_id if self.warning else None,
            "errors": self.errors,
            "degradations": self.degradations,
            "reference_cases": self.reference_cases,
            "context_docs": self.context_docs,
        }


def model_receipt(response: AgentMessage) -> dict[str, Any] | None:
    """从智能体应答里取 `model_info`，并带上"谁答的"；没带凭证就如实 None。

    这是 README 那句"模型与 Prompt 版本随结论入库"的唯一落点：凭证由智能体自己声明
    （`agentkit` 把它写进响应信封），链路只转记、不推断——平台替它编一个模型名就是伪造审计证据。
    """
    info = response.model_info
    if info is None:
        return None
    return {"agent": response.source, **info.model_dump(exclude_none=True)}
