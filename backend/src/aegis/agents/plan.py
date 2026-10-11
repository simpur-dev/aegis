"""plan 决策智能体内环（§3.3）：案例/预案 → LLM 拆解 → STU 校验 → 与本地剧本基线比对。

设计取舍（保证"准确无误"优先于"看着更聪明"）：
- STU 的**合法性由构造保证**——拆解骨架复用已测的 `TaskParser`（PLAYBOOKS 基线），LLM 只在预算内
  **精修** objective/priority/sla_seconds（经 `chat_structured` schema 约束边界），不产出裸 STU、
  不自造 ID/依赖。偏离基线的 task_type 如实记 `deviations` 告警，不据此塞进不合规单元。
- 硬约束：`plan.reschedule` 的 ≤10s 预算**禁入 LLM**（§3.3 deadline 倒推，不可协商）——只走规则候选 +
  本地剧本；LLM 仅异步离线优化。LLM 缺位/异常同样回落到确定性基线，链路始终可用。
- 每个单元 `created_by` 记本智能体（应答归属），payload 形状对齐 `chain._plan` 消费口径（task_units）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field, field_validator

from aegis.agents.agentkit import AgentKit, Reply
from aegis.bus.gateway import ContractRegistry
from aegis.domain.enums import Action, HazardType, RiskLevel, TaskType
from aegis.domain.messages import AgentMessage, ModelInfo, TriggerHit
from aegis.services.prompt_guard import fence_untrusted
from aegis.services.risk_engine import RiskVerdict
from aegis.services.task_parser import TaskParser

_EVENT_ID_RE = re.compile(r"^evt_[0-9a-f]{12}$")
_VALID_TASK_TYPES = frozenset(t.value for t in TaskType)


class PlanAdjustment(BaseModel):
    """LLM 对某一 task_type 的精修建议；边界由 schema 卡死，越界即被 chat_structured 拒收。"""

    model_config = {"extra": "forbid"}

    task_type: str
    objective: str = Field(min_length=2, max_length=512)
    priority: int = Field(ge=1, le=5)
    sla_seconds: int = Field(ge=1, le=86_400)

    @field_validator("task_type")
    @classmethod
    def _known_task(cls, v: str) -> str:
        if v not in _VALID_TASK_TYPES:
            raise ValueError(f"未知 task_type: {v}")
        return v


class PlanRefinement(BaseModel):
    model_config = {"extra": "forbid"}

    adjustments: list[PlanAdjustment] = Field(default_factory=list)


@dataclass
class PlanAgent(AgentKit):
    parser: TaskParser | None = None
    #: 注入后对交付单元再校验一次（链路上游也会校验；这里是智能体侧的自证）。
    contracts: ContractRegistry | None = None

    async def handle(self, request: AgentMessage) -> Reply:
        payload = request.payload or {}
        verdict = self._verdict_from_risk(payload.get("risk") or {})
        raw_event = str(payload.get("event_id", ""))
        event_id = raw_event if _EVENT_ID_RE.match(raw_event) else None

        parser = self.parser or TaskParser()
        baseline = parser.parse(verdict, event_id=event_id)

        # reschedule 或无可用 LLM：直接走确定性本地剧本，绝不进 LLM（§3.3 硬约束）。
        if request.action == Action.PLAN_RESCHEDULE.value or self.llm is None or not getattr(self.llm, "available", True):
            units = [self._stamp(u) for u in baseline]
            return Reply(payload={"task_units": self._to_payloads(units), "source": "local_playbook", "llm_decided": False})

        adjustment_reply = await self._refine(request, payload, verdict)
        applied, deviations = self._apply(baseline, adjustment_reply)
        units = [self._stamp(u) for u in applied]

        if self.contracts is not None:
            dumps = self._to_payloads(units)
            if errors := self.contracts.stu_errors(dumps):
                # 自证不合契约：宁回落基线，也不把非法单元交给平台（STU 合法率 100% 是硬门禁）。
                units = [self._stamp(u) for u in baseline]
                deviations.append(f"精修产出 {len(errors)} 项不合契约，回落本地剧本")
                return Reply(
                    payload={
                        "task_units": self._to_payloads(units),
                        "source": "local_playbook_fallback",
                        "llm_decided": False,
                        "deviations": deviations,
                    },
                    model_info=self._provenance(),
                )

        return Reply(
            payload={"task_units": self._to_payloads(units), "source": "llm_refined", "llm_decided": True, "deviations": deviations},
            model_info=self._provenance(),
        )

    async def _refine(self, request: AgentMessage, payload: dict[str, Any], verdict: RiskVerdict) -> PlanRefinement:
        llm = self.llm
        if llm is None:  # handle() 已在调用前挡住无 LLM 路径；这里为 mypy 收窄类型
            raise RuntimeError("plan 精修需要 LLM 网关")
        return await llm.chat_structured(
            [],
            schema=PlanRefinement,
            task="plan",
            prompt_key="plan",
            prompt_vars=self._prompt_vars(payload, verdict),
            agent_id=self.agent_id,
            trace_id=request.trace_id,
        )

    def _apply(self, baseline: list[Any], refinement: PlanRefinement) -> tuple[list[Any], list[str]]:
        by_task = {a.task_type: a for a in refinement.adjustments}
        baseline_types = {u.task_type.value for u in baseline}
        units = []
        for unit in baseline:
            adj = by_task.get(unit.task_type.value)
            if adj is None:
                units.append(unit)
                continue
            units.append(unit.model_copy(update={"objective": adj.objective, "priority": adj.priority, "sla_seconds": adj.sla_seconds}))
        deviations = [f"LLM 建议了基线外 task_type：{t}（未采纳，保持本地剧本覆盖）" for t in sorted(set(by_task) - baseline_types)]
        return units, deviations

    def _prompt_vars(self, payload: dict[str, Any], verdict: RiskVerdict) -> dict[str, str]:
        """填充 PromptRegistry `plan` 模板的变量（正文单一真源在注册表，§4.1.6）。

        目标（用户指令）、参考案例、检索上下文都是外部文本 → 过提示注入围栏（§4.8.2）。
        """
        cases = payload.get("reference_cases") or []
        ctx = payload.get("retrieved_context") or []
        return {
            "verdict_line": f"灾种：{verdict.hazard_type.value} 等级：{int(verdict.risk_level)} 区域：{verdict.region_code}",
            "objective": fence_untrusted("目标", str(payload.get("objective", ""))),
            "constraints": str(payload.get("constraints", {})),
            "cases": fence_untrusted("参考案例", str(list(cases)[:5])) + "\n" if cases else "",
            "context": fence_untrusted("检索佐证", str(list(ctx)[:5])) + "\n" if ctx else "",
        }

    @staticmethod
    def _verdict_from_risk(risk: dict[str, Any]) -> RiskVerdict:
        try:
            hazard = HazardType(str(risk.get("hazard_type", HazardType.UNKNOWN.value)))
            region = str(risk.get("region_code", "540100A1"))
            level = RiskLevel(int(risk.get("risk_level", int(RiskLevel.ORANGE))))
            confidence = float(risk.get("confidence", 0.6))
            rationale = str(risk.get("rationale", "智能体拆解输入"))[:1000]
        except (ValueError, TypeError):
            hazard, region, level, confidence, rationale = (
                HazardType.UNKNOWN,
                "540100A1",
                RiskLevel.ORANGE,
                0.5,
                "定级输入不可解析，回落未知灾种剧本",
            )
        evidence = [e for e in (risk.get("evidence_refs") or []) if isinstance(e, str) and e.startswith("R-")]
        hits = [TriggerHit(rule_id=e, hazard_type=hazard.value, region_code=region, score=0.7) for e in evidence[:32]]
        return RiskVerdict(hazard_type=hazard, region_code=region, risk_level=level, confidence=confidence, rationale=rationale, hits=hits)

    def _stamp(self, unit: Any) -> Any:
        return unit.model_copy(update={"created_by": self.agent_id})

    @staticmethod
    def _to_payloads(units: list[Any]) -> list[dict[str, Any]]:
        return [u.model_dump(exclude_none=True) for u in units]

    def _provenance(self) -> ModelInfo | None:
        getter = getattr(self.llm, "model_info_for_task", None)
        if getter is None:
            return None
        info = getter("plan")
        return ModelInfo(model=info.get("model"), prompt_version=info.get("prompt_version"))
