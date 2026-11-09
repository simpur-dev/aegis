"""置信度策略的硬性质（生产方自产，平台只校验）。

口径：置信度 = 证据强度，不是概率。三条性质逐条钉住：
① 永不为 0（最低 0.15）——"证据弱"不是"没研判"；
② 永不超 0.60——MOCK 方法上限写死，不随数据齐全而升高；
③ 随覆盖率单调，`basis` 四项可复算。
"""

from __future__ import annotations

import pytest

from aegis.agents_ext.risk_agent.confidence import (
    CONFIDENCE_FLOOR,
    MOCK_CAP,
    assess_confidence,
)

PASS = {"is_valid": True, "evidence_sufficient": True, "issues": [], "recommendation": "proceed"}
FAIL = {"is_valid": False, "evidence_sufficient": False, "issues": ["覆盖率不足"], "recommendation": "retry"}


def test_cap_when_coverage_full() -> None:
    """覆盖 1.0 + 验证通过 → 封顶 0.60（不是 1.0）。"""
    confidence, basis = assess_confidence({"P": 1.0, "T": 1.0}, PASS)
    assert confidence == MOCK_CAP
    assert "cap=0.60" in basis


def test_fixed_points_at_gate() -> None:
    """覆盖 0.60：通过 0.44、未通过 0.39（0.15 + 0.40×0.60 ± 0.05）。"""
    passed, _ = assess_confidence({"P": 0.6, "T": 0.6}, PASS)
    failed, _ = assess_confidence({"P": 0.6, "T": 0.6}, FAIL)
    assert passed == pytest.approx(0.44)
    assert failed == pytest.approx(0.39)


def test_never_zero_floors_at_015() -> None:
    """空/零覆盖：最低 0.15，绝不返回 0。"""
    confidence, basis = assess_confidence({"P": 0.0, "T": 0.0}, FAIL)
    assert confidence == CONFIDENCE_FLOOR
    assert basis.startswith("coverage=0.00")


def test_monotonic_in_coverage() -> None:
    """覆盖率单调递增：同一验证结论下，覆盖越高置信度不降。"""
    scores = [assess_confidence({"P": c, "T": c}, PASS)[0] for c in (0.0, 0.2, 0.4, 0.6, 0.8, 1.0)]
    assert scores == sorted(scores)


def test_uses_min_of_groups() -> None:
    """两组都在时取较小者：一块短板决定整桶水。"""
    low_t, _ = assess_confidence({"P": 1.0, "T": 0.2}, PASS)
    high_t, _ = assess_confidence({"P": 1.0, "T": 1.0}, PASS)
    assert low_t < high_t
    assert assess_confidence({"P": 0.2, "T": 1.0}, PASS)[0] == low_t


def test_basis_is_recomputable() -> None:
    """basis 四项齐全：coverage / validation / method / cap。"""
    _, basis = assess_confidence({"P": 0.8, "T": 0.6}, PASS)
    assert "coverage=0.60" in basis
    assert "validation=pass" in basis
    assert "method=factor_engine_mock" in basis
    assert "cap=0.60" in basis


def test_validation_requires_both_flags() -> None:
    """验证通过必须 is_valid 与 evidence_sufficient 同时为真（只过一半不加分）。"""
    half = {"is_valid": True, "evidence_sufficient": False}
    assert assess_confidence({"P": 0.6, "T": 0.6}, half)[0] == pytest.approx(0.39)


def test_empty_completeness_does_not_crash() -> None:
    """缺失/空的 completeness 不崩：按 0 覆盖算，仍有下限。"""
    confidence, basis = assess_confidence({}, {})
    assert confidence == CONFIDENCE_FLOOR
    assert "coverage=0.00" in basis
