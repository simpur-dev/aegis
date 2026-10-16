"""推演沙盒测试（§3.6/§4.4）：数值模型单调性、概率单调、成本上限降 K、可复现、预案对比排序。

验收口径是"概率矩阵正确性抽查、演示非 SLA"——这里保证的是模型的数学单调性与确定性可复算，
不声称任何现场达标（缓解/增益系数为未实测初值）。
"""

from __future__ import annotations

from aegis.services.simulation import (
    PLAYBOOK_MITIGATION,
    DebrisFlowModel,
    LakeOutburstModel,
    RulePolicy,
    Sandbox,
    SimConfig,
    aggregate_actions,
    secondary_trigger_probability,
)


class TestNumericalModels:
    def test_mudlevel_monotone_and_threshold(self) -> None:
        m = DebrisFlowModel()
        assert m.mudlevel_cm(10) == 0.0  # 低于阈值不增
        assert m.mudlevel_cm(60) > m.mudlevel_cm(40) > m.mudlevel_cm(30)

    def test_outburst_decay(self) -> None:
        m = LakeOutburstModel()
        assert m.discharge_m3s(100, 0) == 100
        assert m.discharge_m3s(100, 12) < m.discharge_m3s(100, 6) < 100


class TestProbability:
    def test_monotone_in_intensity_and_mitigation(self) -> None:
        # 用真实泥位量级（约 0–7），避免系数把概率顶到饱和。
        assert secondary_trigger_probability(6, 0.0) > secondary_trigger_probability(3, 0.0)
        assert secondary_trigger_probability(6, 0.6) < secondary_trigger_probability(6, 0.0)
        assert 0.0 <= secondary_trigger_probability(6, 0.0) <= 1.0

    def test_clamped_bounds(self) -> None:
        assert secondary_trigger_probability(9999, 0.0) == 1.0  # 上限截断
        assert secondary_trigger_probability(0, 1.0) == 0.05  # 下限：满缓解也留 5% 基线，不假装归零


class TestCostCap:
    async def test_within_budget_not_downscaled(self) -> None:
        cfg = SimConfig(scenarios=8, rounds=5, per_role_tokens=800, token_budget=200_000)
        report = await Sandbox().run(cfg)
        assert report.downscaled is False
        assert report.effective_scenarios == 8

    async def test_over_budget_reduces_K(self) -> None:
        cfg = SimConfig(scenarios=8, rounds=5, per_role_tokens=800, token_budget=100_000)
        report = await Sandbox().run(cfg)
        assert report.downscaled is True
        assert 1 <= report.effective_scenarios < 8
        # 降后估算成本确实落进预算（5 轮 × 4 角色 × 800 = 16000/情景）。
        assert report.effective_scenarios * 16000 <= 100_000

    async def test_effective_scenarios_at_least_one(self) -> None:
        cfg = SimConfig(scenarios=8, token_budget=1)
        assert (await Sandbox().run(cfg)).effective_scenarios == 1


class TestReproducibility:
    async def test_same_seed_identical_output(self) -> None:
        cfg = SimConfig()
        a = (await Sandbox().run(cfg)).as_dict()
        b = (await Sandbox().run(cfg)).as_dict()
        assert a == b

    async def test_report_shape(self) -> None:
        report = await Sandbox().run(SimConfig())
        d = report.as_dict()
        assert {"cost", "probability_matrix", "timeline", "playbook_comparison", "roles"} <= set(d)
        assert report.probability_matrix and report.timeline
        assert all("mean_secondary_rate" in row for row in report.probability_matrix)


class TestPlaybookComparison:
    async def test_evacuation_beats_inaction(self) -> None:
        report = await Sandbox(policy=RulePolicy()).run(SimConfig(playbook="转移"))
        by = {row["playbook"]: row for row in report.playbook_comparison}
        assert by["转移"]["exposure_proxy"] < by["不行动"]["exposure_proxy"]
        assert by["转移"]["mean_secondary_rate"] <= by["不行动"]["mean_secondary_rate"]
        assert by["转移"]["mitigation"] > by["封路"]["mitigation"] > by["不行动"]["mitigation"]


class TestRoleTimeline:
    """时间线是"每轮 × 每角色"的票，不是每轮一个动作——§3.6 的角色汇聚口径。"""

    async def test_every_round_carries_every_role_vote(self) -> None:
        cfg = SimConfig(rounds=3)
        report = await Sandbox().run(cfg)
        assert len(report.timeline) == 3
        for row in report.timeline:
            assert set(row["actions"]) == set(cfg.roles)
            assert all(action in PLAYBOOK_MITIGATION for action in row["actions"].values())
            assert "action" not in row, "单动作键已退役：留着就能伪造『四角色一票』"

    async def test_rule_policy_block_is_honest(self) -> None:
        cfg = SimConfig(rounds=3)
        report = await Sandbox(policy=RulePolicy()).run(cfg)
        block = report.roles
        assert block["policy"] == "rule"
        # rule 档离线可复算：asked 就是应问次数，而它没实现计数器 → 按"全问到了"回填。
        assert block["asked"] == 3 * len(cfg.roles)
        assert block["calls"] == block["asked"]
        assert block["fallbacks"] == 0
        assert "note" not in block
        assert [row["key"] for row in block["roles"]] == list(cfg.roles)
        assert all(row["title"] and row["concern"] for row in block["roles"])

    async def test_missing_role_leg_is_called_out(self) -> None:
        """只问两个角色的 policy 就是装配漏接：报告必须自己说出来，不能看起来完整。"""

        class HalfPolicy:
            name = "half"

            def __init__(self) -> None:
                self.calls = 2
                self.fallbacks = 0

            async def choose(self, *, role: str, round_index: int, intensity: float, preferred: str, mudlevel_unit: str = "cm") -> str:
                return preferred

        report = await Sandbox(policy=HalfPolicy()).run(SimConfig(rounds=1))
        assert report.roles["calls"] == 2
        assert report.roles["asked"] == 4
        assert "有角色没被问到" in report.roles["note"]

    async def test_llm_actions_are_visible_per_role(self) -> None:
        """角色各按立场选档时，distinct_actions 要能看出这一份推演真被选过。"""

        class VariedPolicy:
            name = "varied"

            async def choose(self, *, role: str, round_index: int, intensity: float, preferred: str, mudlevel_unit: str = "cm") -> str:
                return "封路" if role == "rescue" else preferred

        report = await Sandbox(policy=VariedPolicy()).run(SimConfig(rounds=2, playbook="转移"))
        assert report.roles["distinct_actions"] == ["封路", "转移"]
        assert all(row["actions"]["rescue"] == "封路" for row in report.timeline)


class TestAggregator:
    """§3.6 汇聚器：四票折成一档生效预案，规则要能口述、能复算。"""

    def test_table_and_whitelist_stay_in_step(self) -> None:
        """两张表一旦分叉，"票能生效"和"系数可查"就各说各话——白名单外的票会被静默丢掉。"""
        from aegis.simulation.roles import ACTIONS

        assert set(PLAYBOOK_MITIGATION) == set(ACTIONS)

    def test_majority_wins(self) -> None:
        votes = {"commander": "转移", "rescue": "转移", "residents": "不行动", "monitor": "封路"}
        assert aggregate_actions(votes, fallback="封路") == "转移"

    def test_tie_takes_stronger_mitigation_not_a_random_vote(self) -> None:
        """平票若随机取一票，同种子换角色顺序就换概率矩阵，可复现这条就废了。"""
        votes = {"a": "封路", "b": "转移"}
        assert aggregate_actions(votes, fallback="不行动") == "转移"
        assert aggregate_actions({"b": "转移", "a": "封路"}, fallback="不行动") == "转移"

    def test_unknown_vote_cannot_set_the_numbers(self) -> None:
        votes = {"commander": "原地待命", "rescue": "原地待命", "residents": "不行动", "monitor": "不行动"}
        assert aggregate_actions(votes, fallback="转移") == "不行动"

    def test_all_votes_bad_keeps_the_declared_scenario(self) -> None:
        assert aggregate_actions({"commander": "躺平"}, fallback="转移") == "转移"

    async def test_votes_reach_the_probability_only_via_the_frozen_table(self) -> None:
        """角色一致投"不行动"时，本轮触发率必须等于表里 0.0 缓解那一档，而不是模型给的任何数。"""

        class InactionPolicy:
            name = "inaction"

            async def choose(self, *, role: str, round_index: int, intensity: float, preferred: str, mudlevel_unit: str = "cm") -> str:
                return "不行动"

        cfg = SimConfig(rounds=2, playbook="转移")
        passive = await Sandbox(policy=InactionPolicy()).run(cfg)
        active = await Sandbox(policy=RulePolicy()).run(cfg)
        for row, twin in zip(passive.timeline, active.timeline, strict=True):
            assert row["applied_playbook"] == "不行动"
            assert row["mitigation"] == PLAYBOOK_MITIGATION["不行动"]
            assert row["mudlevel_cm"] == twin["mudlevel_cm"], "强度只来自遥测模型，不受票影响"
            assert row["trigger_probability"] == secondary_trigger_probability(row["mudlevel_cm"], 0.0)
            assert row["trigger_probability"] > twin["trigger_probability"]

    async def test_rule_policy_applies_the_declared_playbook(self) -> None:
        report = await Sandbox(policy=RulePolicy()).run(SimConfig(rounds=2, playbook="封路"))
        assert [row["applied_playbook"] for row in report.timeline] == ["封路", "封路"]
        assert report.playbook_comparison  # 反事实对比仍按整表出，不受本轮票影响
