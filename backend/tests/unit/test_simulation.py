"""推演沙盒测试（§3.6/§4.4）：数值模型单调性、概率单调、成本上限降 K、可复现、预案对比排序。

验收口径是"概率矩阵正确性抽查、演示非 SLA"——这里保证的是模型的数学单调性与确定性可复算，
不声称任何现场达标（缓解/增益系数为未实测初值）。
"""

from __future__ import annotations

from aegis.services.simulation import (
    DebrisFlowModel,
    LakeOutburstModel,
    RulePolicy,
    Sandbox,
    SimConfig,
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
    def test_within_budget_not_downscaled(self) -> None:
        cfg = SimConfig(scenarios=8, rounds=5, per_role_tokens=800, token_budget=200_000)
        report = Sandbox().run(cfg)
        assert report.downscaled is False
        assert report.effective_scenarios == 8

    def test_over_budget_reduces_K(self) -> None:
        cfg = SimConfig(scenarios=8, rounds=5, per_role_tokens=800, token_budget=100_000)
        report = Sandbox().run(cfg)
        assert report.downscaled is True
        assert 1 <= report.effective_scenarios < 8
        # 降后估算成本确实落进预算（5 轮 × 4 角色 × 800 = 16000/情景）。
        assert report.effective_scenarios * 16000 <= 100_000

    def test_effective_scenarios_at_least_one(self) -> None:
        cfg = SimConfig(scenarios=8, token_budget=1)
        assert Sandbox().run(cfg).effective_scenarios == 1


class TestReproducibility:
    def test_same_seed_identical_output(self) -> None:
        cfg = SimConfig()
        a = Sandbox().run(cfg).as_dict()
        b = Sandbox().run(cfg).as_dict()
        assert a == b

    def test_report_shape(self) -> None:
        report = Sandbox().run(SimConfig())
        d = report.as_dict()
        assert {"cost", "probability_matrix", "timeline", "playbook_comparison"} <= set(d)
        assert report.probability_matrix and report.timeline
        assert all("mean_secondary_rate" in row for row in report.probability_matrix)


class TestPlaybookComparison:
    def test_evacuation_beats_inaction(self) -> None:
        report = Sandbox(policy=RulePolicy()).run(SimConfig(playbook="转移"))
        by = {row["playbook"]: row for row in report.playbook_comparison}
        assert by["转移"]["exposure_proxy"] < by["不行动"]["exposure_proxy"]
        assert by["转移"]["mean_secondary_rate"] <= by["不行动"]["mean_secondary_rate"]
        assert by["转移"]["mitigation"] > by["封路"]["mitigation"] > by["不行动"]["mitigation"]
