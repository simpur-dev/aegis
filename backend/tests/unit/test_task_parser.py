"""任务拆解测试：剧本裁剪、依赖连线、SLA 预算、契约合法性、降级策略。"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from aegis.bus.gateway import ContractRegistry
from aegis.domain.enums import FallbackMode, HazardType, OwnerRole, RiskLevel, TaskType
from aegis.domain.messages import TriggerHit
from aegis.services.risk_engine import RiskVerdict
from aegis.services.task_parser import MONITOR, PLAYBOOKS, REPORT, VERIFY, WARN, TaskParser

REGION = "540121"


def sample_verdict(level: RiskLevel = RiskLevel.RED, hazard: HazardType = HazardType.DEBRIS_FLOW) -> RiskVerdict:
    hits = [
        TriggerHit(rule_id="R-DEBRIS-RAIN-1", hazard_type=hazard.value, region_code=REGION, score=0.9),
        TriggerHit(rule_id="R-DEBRIS-RAIN-2", hazard_type=hazard.value, region_code=REGION, score=0.8),
    ]
    return RiskVerdict(
        hazard_type=hazard,
        region_code=REGION,
        risk_level=level,
        confidence=0.85,
        rationale="测试定级依据",
        hits=hits,
    )


@pytest.fixture
def parser(contracts: ContractRegistry) -> TaskParser:
    return TaskParser(contracts)


class TestLevelDrivenPlaybook:
    def test_red_full_chain(self, parser: TaskParser) -> None:
        units = parser.parse(sample_verdict(RiskLevel.RED))
        assert [u.task_type for u in units] == [
            TaskType.VERIFY,
            TaskType.WARN,
            TaskType.EVACUATE,
            TaskType.MONITOR,
            TaskType.REPORT,
        ]

    @pytest.mark.parametrize("level", [RiskLevel.YELLOW, RiskLevel.BLUE])
    def test_low_level_drops_evacuate(self, parser: TaskParser, level: RiskLevel) -> None:
        types = [u.task_type for u in parser.parse(sample_verdict(level))]
        assert TaskType.EVACUATE not in types
        assert TaskType.WARN in types  # 低等级仍需发布黄/蓝色预警

    def test_none_level_yields_nothing(self, parser: TaskParser) -> None:
        assert parser.parse(sample_verdict(RiskLevel.NONE)) == []

    @pytest.mark.parametrize("hazard", list(HazardType))
    def test_every_hazard_has_playbook(self, parser: TaskParser, hazard: HazardType) -> None:
        units = parser.parse(sample_verdict(RiskLevel.ORANGE, hazard))
        assert units, f"{hazard.value} 缺处置剧本"
        assert units[0].hazard_type == hazard.value


class TestDependencyWiring:
    def test_dependencies_reference_real_ids(self, parser: TaskParser) -> None:
        units = parser.parse(sample_verdict(RiskLevel.RED))
        by_type = {u.task_type: u for u in units}
        ids = {u.task_unit_id for u in units}
        assert by_type[TaskType.WARN].dependencies == [by_type[TaskType.VERIFY].task_unit_id]
        assert by_type[TaskType.EVACUATE].dependencies == [by_type[TaskType.WARN].task_unit_id]
        assert set(by_type[TaskType.REPORT].dependencies) <= ids
        for unit in units:
            assert unit.task_unit_id not in unit.dependencies, "不得出现自依赖"

    def test_low_level_keeps_original_indices(self, parser: TaskParser) -> None:
        """裁剪 EVACUATE 后，REPORT 的依赖必须重映射到仍存在的任务，而非序号错位。"""
        units = parser.parse(sample_verdict(RiskLevel.YELLOW))
        remaining = {u.task_unit_id for u in units}
        for unit in units:
            assert set(unit.dependencies) <= remaining
            assert unit.task_unit_id not in unit.dependencies

    def test_unknown_hazard_keeps_warning_without_evacuation(self, parser: TaskParser) -> None:
        """灾种未定时：仍发预警、不启动定向转移（转移路线依赖已知威胁区）。"""
        types = [u.task_type for u in parser.parse(sample_verdict(RiskLevel.RED, HazardType.UNKNOWN))]
        assert types == [TaskType.VERIFY, TaskType.WARN, TaskType.MONITOR, TaskType.REPORT]
        assert TaskType.EVACUATE not in types


class TestSlaBudgets:
    def test_warn_budget_matches_generation_sla(self, parser: TaskParser) -> None:
        units = {u.task_type: u for u in parser.parse(sample_verdict(RiskLevel.RED))}
        assert units[TaskType.WARN].sla_seconds <= 180

    def test_red_compresses_other_budgets(self, parser: TaskParser) -> None:
        red = {u.task_type: u for u in parser.parse(sample_verdict(RiskLevel.RED))}
        orange = {u.task_type: u for u in parser.parse(sample_verdict(RiskLevel.ORANGE))}
        assert red[TaskType.MONITOR].sla_seconds < orange[TaskType.MONITOR].sla_seconds
        assert red[TaskType.REPORT].sla_seconds == int(3_600 * 0.6)

    def test_priority_boost_clamps_at_one(self, parser: TaskParser) -> None:
        units = parser.parse(sample_verdict(RiskLevel.RED), priority_boost=True)
        assert all(u.priority >= 1 for u in units)
        assert any(u.priority == 1 for u in units)


class TestContractCompliance:
    @pytest.mark.parametrize("level", list(RiskLevel))
    @pytest.mark.parametrize("hazard", list(HazardType))
    def test_all_units_pass_contract(self, contracts: ContractRegistry, level: RiskLevel, hazard: HazardType) -> None:
        units = TaskParser(contracts).parse(sample_verdict(level, hazard))
        payloads = [u.model_dump(exclude_none=True) for u in units]
        assert contracts.stu_errors(payloads) == []

    def test_provenance_fields(self, parser: TaskParser) -> None:
        unit = parser.parse(sample_verdict(RiskLevel.RED))[0]
        assert unit.created_by == "platform.task_parser"
        assert unit.trigger_refs == ["R-DEBRIS-RAIN-1", "R-DEBRIS-RAIN-2"]
        assert {ref.id for ref in unit.input_data_refs} == set(unit.trigger_refs)
        assert unit.context_snapshot["risk_level"] == int(RiskLevel.RED)

    def test_objective_contains_hazard_and_level(self, parser: TaskParser) -> None:
        unit = parser.parse(sample_verdict(RiskLevel.RED))[0]
        assert "泥石流" in unit.objective and "红色" in unit.objective


class TestFallbackPolicies:
    def test_human_owned_steps_escalate(self, parser: TaskParser) -> None:
        units = {u.task_type: u for u in parser.parse(sample_verdict(RiskLevel.RED))}
        policy = units[TaskType.EVACUATE].fallback_policy
        assert policy.mode is FallbackMode.ESCALATE
        assert policy.escalate_to_role is OwnerRole.HUMAN_COMMANDER

    def test_critical_steps_retry(self, parser: TaskParser) -> None:
        units = {u.task_type: u for u in parser.parse(sample_verdict(RiskLevel.RED))}
        assert units[TaskType.WARN].fallback_policy.mode is FallbackMode.RETRY
        assert units[TaskType.VERIFY].fallback_policy.mode is FallbackMode.RETRY

    def test_routine_steps_degrade_to_rule(self, parser: TaskParser) -> None:
        units = {u.task_type: u for u in parser.parse(sample_verdict(RiskLevel.RED))}
        assert units[TaskType.REPORT].fallback_policy.mode is FallbackMode.DEGRADE_TO_RULE


class TestStepValidation:
    def test_step_defaults(self) -> None:
        assert WARN.depends_on == (0,) and REPORT.depends_on == (1, 2, 3)
        assert MONITOR.owner_role is OwnerRole.AGENT

    def test_playbooks_cover_all_hazards(self) -> None:
        assert set(PLAYBOOKS) == set(HazardType)

    def test_step_is_frozen(self) -> None:
        with pytest.raises(FrozenInstanceError):
            VERIFY.sla_seconds = 1  # type: ignore[misc]
