"""链路产物：`ChainResult` / `StageResult` 与 D10 凭证转记。

这是**对外口径**（API、前端 `types.ts`、`chain_runs` 落库映射、报表都读它），
D7 编排归一明确要求"退役 chain 之后这些形状不变"，所以把它单独成模块：
段实现（`pipeline/stages.py`）与编排器（`pipeline/workflow_chain.py`）、工作流节点共用同一份产物定义，
谁都不许另立一个"看起来一样"的结果类。

为什么住在 `domain/` 而不是 `pipeline/`：它是纯数据契约，而读它的不止链路——
`storage`/`persistence` 要按它落台账、`api` 要按它回话。放在 pipeline 里，
那些下游就得反向 import 应用层（这正是本次搬家要消灭的那四条边）。
它只依赖 `domain` 自己的枚举、契约镜像与 `verdict.RiskVerdict`，符合"domain 不依赖任何 aegis 包"。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from aegis.domain.messages import AgentMessage, StandardizedTaskUnit, TriggerHit, WarningRecord
from aegis.domain.verdict import RiskVerdict

#: 五段的名字与顺序。名字本身是契约：指标样本名由 `f"stage_{name}_ms"` 派生，
#: 改一个字符串就等于悄悄改了指标准径（见 `integrations.analytics.chain_facts`）。
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
    #: 承载这条链路的**工作流实例**终态（succeeded/failed/waiting/aborted）。
    #: 段账只记"跑过的段"，实例被 abort 或卡在人工节点时可能只剩成功的前几段——
    #: 只看段就会把"图没跑完"报成成功。空串表示没有实例可看（离线构造的结果）。
    instance_status: str = ""

    @property
    def ok(self) -> bool:
        """链路成功 = 实例终态成功 + 各段执行成功 + 无错误。无触发时同样成功（未告警是正确结果）。

        实例状态优先于段账：引擎在段之间失败/中止时，已跑的段仍然 `ok=True`，
        不拿实例终态兜底，`chain_runs.ok` 就会为一条没走完的链路记"成功"。
        """
        if self.instance_status and self.instance_status != "succeeded":
            return False
        return all(s.ok for s in self.stages) and not self.errors

    @property
    def acted(self) -> bool:
        return self.verdict is not None

    def stage(self, name: str) -> StageResult | None:
        return next((s for s in self.stages if s.name == name), None)

    def as_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
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
        # 只在有实例时出现：离线构造的结果不该被写成 `instance_status: ""` 让读的人猜是"没实例"还是"漏填"。
        if self.instance_status:
            data["instance_status"] = self.instance_status
        return data


def model_receipt(response: AgentMessage) -> dict[str, Any] | None:
    """从智能体应答里取 `model_info`，并带上"谁答的"；没带凭证就如实 None。

    这是 README 那句"模型与 Prompt 版本随结论入库"的唯一落点：凭证由智能体自己声明
    （`agentkit` 把它写进响应信封），链路只转记、不推断——平台替它编一个模型名就是伪造审计证据。
    """
    info = response.model_info
    if info is None:
        return None
    return {"agent": response.source, **info.model_dump(exclude_none=True)}
