"""模型卡片 + 质量四表/回归门禁测试（§4.5/§5）：只陈述真实配置，未评测如实 None。"""

from __future__ import annotations

from aegis.bus.gateway import ContractRegistry
from aegis.config import Settings
from aegis.services.model_cards import build_model_cards
from aegis.services.prompt_registry import PromptRegistry
from aegis.services.quality_eval import QualityMetrics, regression_gate, stu_legality_rate


async def _legal_units() -> list[dict]:
    from aegis.agents.plan import PlanAgent
    from aegis.domain.enums import AgentType
    from aegis.domain.messages import make_request

    agent = PlanAgent(transport=None, agent_type=AgentType.PLAN, agent_id="plan.test1", capabilities=["task_decompose"], llm=None)  # type: ignore[arg-type]
    req = make_request(
        source="platform.t",
        target="plan.test1",
        action="plan.stu",
        payload={
            "event_id": "evt_" + "1" * 12,
            "risk": {
                "hazard_type": "debris_flow",
                "region_code": "540121",
                "risk_level": 1,
                "confidence": 0.9,
                "rationale": "r",
                "evidence_refs": ["R-1"],
            },
            "objective": "o",
            "constraints": {},
            "reference_cases": [],
            "retrieved_context": [],
        },
        trace_id="trc_" + "1" * 16,
        reply_to="reply.t.inbox",
        deadline_ms=3000,
    )
    reply = await agent.handle(req)
    return list(reply.payload["task_units"])


class TestModelCards:
    def test_cards_from_real_config_unmeasured_honest(self) -> None:
        cards = build_model_cards(Settings(env="test"), PromptRegistry.builtin())
        by = {c["agent"]: c for c in cards}
        # 已迁入版本化真源的 task → 有 prompt_version；未迁入如实 None。
        assert by["assess"]["prompt_version"] == "1.0.0"
        assert by["plan"]["prompt_version"] == "1.0.0"
        assert by["execute"]["prompt_version"] is None
        assert by["perceive"]["prompt_version"] is None
        # 模型来自真实路由配置。
        assert by["assess"]["model"] == "qwen-plus"
        assert by["perceive"]["model"] == "qwen-turbo"
        # 训练集与评测分未做 → None + measured=False，不冒充。
        for card in cards:
            assert card["trained_dataset"] is None
            assert card["eval_score"] is None
            assert card["measured"] is False


class TestStuLegality:
    async def test_legal_units_score_one(self) -> None:
        units = await _legal_units()
        assert units
        rate, bad = stu_legality_rate(units, ContractRegistry())
        assert rate == 1.0 and bad == 0

    async def test_illegal_unit_drops_rate(self) -> None:
        units = await _legal_units()
        units.append({"not": "a-stu"})
        rate, bad = stu_legality_rate(units, ContractRegistry())
        assert bad >= 1
        assert rate < 1.0

    def test_empty_is_one_but_not_claimed(self) -> None:
        rate, bad = stu_legality_rate([], ContractRegistry())
        assert rate == 1.0 and bad == 0


class TestRegressionGate:
    def test_no_baseline_no_regression(self) -> None:
        result = regression_gate(QualityMetrics(stu_legality=0.95), QualityMetrics())
        assert result.passed is True
        assert result.regressions == []

    def test_drop_below_baseline_fails(self) -> None:
        result = regression_gate(QualityMetrics(stu_legality=0.80), QualityMetrics(stu_legality=0.99))
        assert result.passed is False
        assert "stu_legality" in {r["metric"] for r in result.regressions}

    def test_improvement_or_equal_passes(self) -> None:
        assert regression_gate(QualityMetrics(stu_legality=0.99), QualityMetrics(stu_legality=0.99)).passed is True
        assert regression_gate(QualityMetrics(stu_legality=1.0), QualityMetrics(stu_legality=0.99)).passed is True

    def test_unmeasured_column_neither_passes_nor_regresses(self) -> None:
        result = regression_gate(QualityMetrics(stu_legality=0.99), QualityMetrics(stu_legality=0.99, trigger_f1=0.7))
        assert result.passed is True
        assert "trigger_f1" in result.unmeasured, "当前未评测的列记入 unmeasured，不拿它冒充通过"
