"""因子引擎（观测输入口径）的边界测试。

钉子：
- 覆盖率门控是**组内加权因子覆盖率**：恰 0.60 放行、0.599 拒绝；
- 必需因子缺失直接 insufficient，不输出 R；
- 非必需缺失**不填 0**：按剩余权重重归一，并如实披露缺失；
- 代表值口径：经典触发因子取窗口最大值（agg=max），孕灾因子取窗口均值（agg=mean），
  分类因子取 `class_value`；
- R==0 → `provisional_level="none"`（"算出来没风险"），不是 `unknown`（"算不出来"）；
- 空观测 → insufficient，不崩。
"""

from __future__ import annotations

from typing import Any

import pytest

from aegis.agents.hazards.risk_agent.tools.factor_engine import (
    compute_factor_assessment,
    mock_weight_source,
)
from aegis.agents.hazards.risk_agent.tools.rockfall_risk_tool import rockfall_factor_assessment_tool
from aegis.domain.assess import FactorObservation

TS = "2024-07-08T03:00:00.000Z"


def _obs(
    factor: str,
    *,
    value: float | None = None,
    class_value: str | None = None,
    mean_value: float | None = None,
) -> dict:
    if class_value is not None:
        item = FactorObservation(factor=factor, class_value=class_value, samples=2, observed_at=TS)
    else:
        item = FactorObservation(
            factor=factor,
            min_value=value,
            max_value=value,
            mean_value=mean_value if mean_value is not None else value,
            samples=2,
            observed_at=TS,
        )
    return item.model_dump(mode="json")


def _factor(key: str, *, group: str, weight: float, agg: str = "mean", lo: float = 0.0, hi: float = 100.0) -> dict:
    return {
        "key": key,
        "label": key,
        "group": group,
        "unit": "",
        "agg": agg,
        "normalize": {"method": "linear_pos", "L": lo, "U": hi},
        "weight": weight,
        "weight_source": mock_weight_source(),
        "data_status": "mock",
    }


def _config(p_weights: tuple[float, float] = (0.6, 0.4), *, required: list[str] | None = None) -> dict[str, Any]:
    return {
        "hazard_type": "debris_flow",
        "hazard_type_cn": "泥石流",
        "rule_version": "MOCK-v0.1",
        "disclaimer": "测试用 MOCK 配置",
        "min_coverage": 0.6,
        "required_factors": ["rainfall_mm_h"] if required is None else required,
        "groups": {
            "P": [
                _factor("slope_angle_deg", group="P", weight=p_weights[0]),
                _factor("catchment_area_km2", group="P", weight=p_weights[1]),
            ],
            "T": [_factor("rainfall_mm_h", group="T", weight=1.0, agg="max", hi=150.0)],
        },
        "route": None,
        "protected_rules": [],
    }


def test_coverage_exactly_at_gate_scores() -> None:
    """恰 0.60 覆盖率：放行并给出 P/T/R。"""
    env = compute_factor_assessment(
        [_obs("slope_angle_deg", value=50.0), _obs("rainfall_mm_h", value=75.0)],
        "540121",
        _config(),
    )
    assert env["completeness"]["P"] == 0.6
    assert env["assessment_status"] == "scored"
    assert env["R"] == round(100.0 * env["P"] * env["T"], 2)
    assert env["coverage_basis"] == "weighted_factor_coverage"


def test_coverage_below_gate_is_insufficient() -> None:
    """0.599 覆盖率：拒绝评分，原因指名 P 组与门控值。"""
    env = compute_factor_assessment(
        [_obs("slope_angle_deg", value=50.0), _obs("rainfall_mm_h", value=75.0)],
        "540121",
        _config(p_weights=(0.599, 0.401)),
    )
    assert env["completeness"]["P"] == 0.599
    assert env["assessment_status"] == "insufficient_data"
    assert env["R"] is None
    assert any("P 组数据覆盖率" in reason and "60%" in reason for reason in env["insufficient_reasons"])


def test_required_factor_missing_is_insufficient() -> None:
    """必需因子缺失：即使覆盖率够，也不评分（必需 = 没有它不能谈这个灾种）。"""
    env = compute_factor_assessment([_obs("slope_angle_deg", value=50.0)], "540121", _config())
    assert env["assessment_status"] == "insufficient_data"
    assert "rainfall_mm_h" in env["missing_factors"]
    assert any("必需因子缺失" in reason for reason in env["insufficient_reasons"])


def test_non_required_missing_renormalizes_not_zero() -> None:
    """非必需缺失不填 0：P 只剩坡度权重，得分等于坡度归一化值，缺失如实披露。"""
    env = compute_factor_assessment(
        [_obs("slope_angle_deg", value=25.0), _obs("rainfall_mm_h", value=0.0)],
        "540121",
        _config(required=[]),
    )
    assert env["assessment_status"] == "scored"
    assert env["missing_factors"] == ["catchment_area_km2"]
    # 坡度 25/100 = 0.25；若把缺失因子填 0 并保留权重，得分会被稀释成 0.15
    assert env["P"] == pytest.approx(0.25)
    assert env["factor_scores"]["catchment_area_km2"]["z"] is None


def test_representative_value_by_agg() -> None:
    """代表值：触发类取窗口最大值，孕灾类取窗口均值；数值优先于类别。"""
    config = _config()
    config["groups"]["P"][0]["agg"] = "mean"
    config["groups"]["T"][0]["agg"] = "max"
    env = compute_factor_assessment(
        [
            _obs("slope_angle_deg", value=20.0, mean_value=40.0),
            _obs("catchment_area_km2", value=10.0),
            _obs("rainfall_mm_h", value=120.0, mean_value=30.0),
        ],
        "540121",
        config,
    )
    assert env["factor_scores"]["slope_angle_deg"]["raw_value"] == 40.0, "孕灾因子取均值"
    assert env["factor_scores"]["rainfall_mm_h"]["raw_value"] == 120.0, "触发因子取窗口最大值"


def test_class_value_and_protected_rule() -> None:
    """分类因子取 class_value；保护规则按类别值命中。"""
    config = _config()
    config["groups"]["P"].append(_factor("glacier_activity", group="P", weight=0.0, agg="max"))
    config["protected_rules"] = [
        {
            "rule_id": "TEST-RULE",
            "description": "测试规则",
            "when": {"field": "glacier_activity", "equals": "active"},
            "level_suggest": "high",
            "validation_status": "mock",
        }
    ]
    env = compute_factor_assessment(
        [
            _obs("slope_angle_deg", value=50.0),
            _obs("catchment_area_km2", value=10.0),
            _obs("rainfall_mm_h", value=75.0),
            _obs("glacier_activity", class_value="active"),
        ],
        "540121",
        config,
    )
    assert env["factor_scores"]["glacier_activity"]["raw_value"] == "active"
    assert [hit["rule_id"] for hit in env["rule_hits"]] == ["TEST-RULE"]


def test_unknown_class_uses_default_route_with_warning() -> None:
    """未知类别不冒充数值：路由走默认权重并留警示。"""
    env = rockfall_factor_assessment_tool(
        [_obs("failure_mode", class_value="volcano"), _obs("slope_height_m", value=180.0)],
        "540121",
    )
    assert env["route_value"] == "volcano"
    assert env["warnings"], "未知路由取值必须有警示"
    assert "无对应变体" in env["warnings"][0]


def test_r_zero_is_none_not_unknown() -> None:
    """R==0 → none（算出来没风险）；unknown 只留给"算不出来"。"""
    env = compute_factor_assessment(
        [_obs("slope_angle_deg", value=0.0), _obs("catchment_area_km2", value=0.0), _obs("rainfall_mm_h", value=0.0)],
        "540121",
        _config(required=[]),
    )
    assert env["assessment_status"] == "scored"
    assert env["R"] == 0
    assert env["provisional_level"] == "none"


def test_empty_observations_insufficient_without_crash() -> None:
    """空观测：insufficient，P/T/R 全空，不抛。"""
    env = compute_factor_assessment([], "540121", _config())
    assert env["assessment_status"] == "insufficient_data"
    assert env["P"] is None and env["T"] is None and env["R"] is None
    assert env["sample_count"] == 0
    assert env["insufficient_reasons"]


def test_sample_count_sums_observations() -> None:
    """sample_count 是观测样本数之和（别名改名后仍要数得对）。"""
    env = compute_factor_assessment(
        [_obs("slope_angle_deg", value=50.0), _obs("rainfall_mm_h", value=75.0)],
        "540121",
        _config(),
    )
    assert env["sample_count"] == 4
