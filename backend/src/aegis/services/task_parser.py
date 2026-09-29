"""任务解析与拆解：把风险定级结论转为标准化任务单元（STU）集。

这是"防控任务智能解析与自动化拆解"的平台侧骨架：内置灾种处置剧本（playbook），
决策智能体在场时其产出覆盖本地结果，缺位/超时时由本模块降级生成——保证链路始终可用。
"""

from __future__ import annotations

from dataclasses import dataclass

from aegis.bus.gateway import ContractRegistry
from aegis.config import Settings, get_settings
from aegis.domain.enums import FallbackMode, HazardType, OwnerRole, RefType, RiskLevel, TaskType
from aegis.domain.messages import DataRef, StandardizedTaskUnit, new_event_id
from aegis.errors import SchemaInvalidError
from aegis.services.risk_engine import RiskVerdict


@dataclass(frozen=True, slots=True)
class Step:
    task_type: TaskType
    objective: str
    capability: str
    owner_role: OwnerRole
    sla_seconds: int
    priority: int = 3
    outputs: tuple[str, ...] = ("task_result",)
    depends_on: tuple[int, ...] = ()  # 剧本内序号，解析时映射为 STU id


VERIFY = Step(TaskType.VERIFY, "现场核查监测异常与险情", "field_verify", OwnerRole.FIELD_INSPECTOR, 600, 2)
WARN = Step(TaskType.WARN, "生成并发布分级预警", "warn_publish", OwnerRole.AGENT, 180, 1, depends_on=(0,))
EVACUATE = Step(TaskType.EVACUATE, "组织受威胁人群转移", "evacuate_org", OwnerRole.HUMAN_COMMANDER, 900, 1, depends_on=(1,))
MONITOR = Step(TaskType.MONITOR, "加密监测频次并跟踪态势", "monitor_focus", OwnerRole.AGENT, 1_800, 2, ("task_result",), (0,))
REPORT = Step(
    TaskType.REPORT,
    "汇总处置过程并出具报告",
    "report_gen",
    OwnerRole.AGENT,
    3_600,
    4,
    ("task_result", "feedback_report"),
    (1, 2, 3),
)

PLAYBOOKS: dict[HazardType, list[Step]] = {
    HazardType.DEBRIS_FLOW: [VERIFY, WARN, EVACUATE, MONITOR, REPORT],
    HazardType.LANDSLIDE: [VERIFY, WARN, EVACUATE, MONITOR, REPORT],
    HazardType.ROCKFALL: [VERIFY, WARN, MONITOR, REPORT],
    HazardType.AVALANCHE: [WARN, EVACUATE, MONITOR, REPORT],
    HazardType.LAKE_OUTBURST: [VERIFY, WARN, EVACUATE, MONITOR, REPORT],
    HazardType.QUAKE_TRIGGERED: [VERIFY, WARN, EVACUATE, MONITOR, REPORT],
    # 灾种未定：仍发布预警以提醒属地，但不启动定向转移（转移路线依赖已知威胁区）
    HazardType.UNKNOWN: [VERIFY, WARN, MONITOR, REPORT],
}

# 低等级（黄/蓝）不启动人员转移任务，但仍需发布预警与加密监测
_LOW_LEVEL_STEPS: tuple[TaskType, ...] = (TaskType.VERIFY, TaskType.WARN, TaskType.MONITOR, TaskType.REPORT)


class TaskParser:
    def __init__(self, contracts: ContractRegistry | None = None, *, settings: Settings | None = None) -> None:
        self._contracts = contracts
        self._settings = settings or get_settings()

    def playbook_for(self, hazard_type: HazardType) -> list[Step]:
        return PLAYBOOKS.get(hazard_type, PLAYBOOKS[HazardType.UNKNOWN])

    def parse(
        self,
        verdict: RiskVerdict,
        *,
        event_id: str | None = None,
        priority_boost: bool = False,
    ) -> list[StandardizedTaskUnit]:
        if verdict.risk_level is RiskLevel.NONE:
            return []

        event = event_id or new_event_id()
        playbook = self.playbook_for(verdict.hazard_type)
        # 依赖关系始终以完整剧本序号为准，裁剪低等级步骤不重排索引（否则会出现自依赖）
        active_indexes = {
            index for index, step in enumerate(playbook) if verdict.risk_level < RiskLevel.YELLOW or step.task_type in _LOW_LEVEL_STEPS
        }

        units: list[StandardizedTaskUnit] = []
        id_by_index: dict[int, str] = {}
        for index, step in enumerate(playbook):
            if index not in active_indexes:
                continue
            dependencies = [id_by_index[i] for i in step.depends_on if i in id_by_index]
            unit = StandardizedTaskUnit(
                event_id=event,
                hazard_type=verdict.hazard_type.value,
                region_code=verdict.region_code,
                task_type=step.task_type,
                objective=f"[{verdict.hazard_type.cn}/{verdict.risk_level.cn}] {step.objective}",
                priority=max(1, step.priority - (1 if priority_boost else 0)),
                sla_seconds=self._budget(step, verdict),
                required_capabilities=[step.capability],
                owner_role=step.owner_role,
                trigger_refs=[hit.rule_id for hit in verdict.hits],
                input_data_refs=[DataRef(type=RefType.ENTITY, id=hit.rule_id) for hit in verdict.hits],
                outputs=list(step.outputs),
                dependencies=dependencies,
                fallback_policy=self._fallback(step),
                context_snapshot={
                    "risk_level": int(verdict.risk_level),
                    "confidence": verdict.confidence,
                    "rationale": verdict.rationale[:400],
                },
                created_by="platform.task_parser",
            )
            id_by_index[index] = unit.task_unit_id
            units.append(unit)

        if self._contracts is not None:
            self._validate(units)
        return units

    def _budget(self, step: Step, verdict: RiskVerdict) -> int:
        """紧急等级下压缩时限预算，保证预警生成落在 ≤3min 指标内。"""
        if step.task_type is TaskType.WARN:
            return min(step.sla_seconds, int(self._settings.sla_warning_gen_seconds))
        if verdict.risk_level is RiskLevel.RED:
            return max(60, int(step.sla_seconds * 0.6))
        return step.sla_seconds

    @staticmethod
    def _fallback(step: Step):
        from aegis.domain.messages import FallbackPolicy

        if step.owner_role is OwnerRole.HUMAN_COMMANDER:
            return FallbackPolicy(mode=FallbackMode.ESCALATE, escalate_to_role=OwnerRole.HUMAN_COMMANDER)
        if step.task_type in (TaskType.WARN, TaskType.VERIFY):
            return FallbackPolicy(mode=FallbackMode.RETRY, max_retries=2, retry_backoff_ms=1_000)
        return FallbackPolicy(mode=FallbackMode.DEGRADE_TO_RULE, max_retries=1)

    def _validate(self, units: list[StandardizedTaskUnit]) -> None:
        if self._contracts is None:
            return
        payloads = [unit.model_dump(exclude_none=True) for unit in units]
        if errors := self._contracts.stu_errors(payloads):
            raise SchemaInvalidError(
                f"拆解产出 {len(errors)} 个不合契约的 STU",
                detail={"errors": errors[:5]},
            )
