"""子代理审查第二轮修复的回归用例（agents 层）。

- 因子引擎：权重变体引用未声明因子必须响亮失败（此前静默不生效 + route_note 谎报）；
- 因子引擎：I 组（佐证面）不再当覆盖率硬门——完整观测的冰湖评估不得整单拒绝；
- 熔断：半开试探名额在"不计失败的失败"（4xx）后必须归还，否则永久卡死 half_open；
- 泥石流路由：outburst 变体真正生效（T 分由联动标志驱动，不再是默认降雨权重）。
"""

from __future__ import annotations

import pytest

from aegis.agents.core.breaker import LlmBreaker
from aegis.agents.hazards.risk_agent.tools.debris_flow_risk_tool import FACTOR_CONFIG as DEBRIS_CONFIG
from aegis.agents.hazards.risk_agent.tools.factor_engine import compute_factor_assessment
from aegis.agents.hazards.risk_agent.tools.glacial_lake_risk_tool import FACTOR_CONFIG as LAKE_CONFIG
from aegis.domain.assess import FactorObservation

TS = "2026-11-20T08:00:00Z"


def _obs(factor: str, *, value: float | None = None, class_value: str | None = None) -> dict:
    if class_value is not None:
        return FactorObservation(factor=factor, class_value=class_value, samples=2, observed_at=TS).model_dump(mode="json")
    return FactorObservation(factor=factor, min_value=value, max_value=value, mean_value=value, samples=2, observed_at=TS).model_dump(
        mode="json"
    )


class TestCoverageGate:
    def test_I组不当硬门_完整观测出结论(self) -> None:
        """站点画像缺 downstream_distance_km 不再把冰湖评估整单拒绝（此前 insufficient_data）。"""
        observations = [
            _obs("lake_volume_m3", value=1_000_000.0),
            _obs("water_level_ratio", value=0.9),
            # T 组：给足两个主触发（0.5+0.3=0.8 ≥ 0.6 门控），landslide_into_lake(0.2) 缺失走重归一
            _obs("ice_avalanche_into_lake", class_value="yes"),
            _obs("seepage_level", class_value="mild"),
        ]
        envelope = compute_factor_assessment(observations, region="540121", config=LAKE_CONFIG)
        assert envelope["assessment_status"] == "scored", f"reasons={envelope.get('insufficient_reasons')}"
        assert envelope["R"] is not None
        # I 组缺数据仍有留痕（佐证面降级），但不是 insufficient_reason
        assert any("I 组" in warning for warning in envelope["warnings"])

    def test_P组不足仍拒绝(self) -> None:
        """硬门语义对 P/T 不变：P 组全缺照样 insufficient_data。"""
        envelope = compute_factor_assessment([_obs("ice_avalanche_into_lake", class_value="yes")], region="540121", config=LAKE_CONFIG)
        assert envelope["assessment_status"] == "insufficient_data"


class TestRouteVariantGuard:
    def test_变体引用未声明因子响亮失败(self) -> None:
        bad = {
            **DEBRIS_CONFIG,
            "route": {"factor_key": "water_source_type", "variants": {"rainstorm": {"T": {"undeclared_factor": 1.0}}}},
        }
        observations = [_obs("water_source_type", class_value="rainstorm")]
        with pytest.raises(ValueError, match="未声明的因子"):
            compute_factor_assessment(observations, region="540121", config=bad)


class TestDebrisRouteVariant:
    def test_outburst变体真正生效(self) -> None:
        """outburst 路由 + 联动标志：变体整组替换权重，T 由标志驱动（此前恒按默认降雨权重）。"""
        observations = [
            _obs("water_source_type", class_value="outburst"),
            _obs("upstream_breach_flag", class_value="yes"),
            # required 因子必须有观测（z 非 None 即可，值本身可以是 0）
            _obs("rainfall_mm_h", value=0.0),
            # P 组：0.4+0.3=0.7 ≥ 0.6 门控
            _obs("channel_gradient", value=0.35),
            _obs("catchment_area_km2", value=12.0),
        ]
        envelope = compute_factor_assessment(observations, region="540121", config=DEBRIS_CONFIG)
        assert envelope["assessment_status"] == "scored", f"reasons={envelope.get('insufficient_reasons')}"
        flag = envelope["factor_scores"]["upstream_breach_flag"]
        assert flag["weight_kind"] == "route_variant", "变体权重必须真的用上（此前未声明因子永远走 default）"
        assert flag["weight"] == pytest.approx(1.0)
        rain = envelope["factor_scores"]["rainfall_mm_h"]
        assert rain["weight_kind"] == "route_variant_zero" and rain["weight"] == 0.0, "整组替换：未列入变体的因子权重归零"
        assert "启用对应权重变体" in envelope.get("route_note", "")
        assert envelope["T"] is not None and envelope["T"] > 0, "联动标志为 yes 时 T 分不得为 0"


class TestBreakerProbeRelease:
    def test_非传输失败后试探名额归还(self) -> None:
        breaker = LlmBreaker(threshold=1)
        breaker.note_failure()  # 达阈值 → open
        assert breaker.acquire() is False, "冷却窗口内拒绝"
        now = [breaker._clock()]
        breaker._clock = lambda: now.__setitem__(0, now[0] + 3600) or now[0]  # 冷却即过（替身钟）
        assert breaker.acquire() is True, "转半开并占用唯一试探名额"
        # 4xx：不计失败（is_transport_failure=False → 不调 note_failure），但名额必须归还
        breaker.release_probe()
        assert breaker.acquire() is True, "归还后可再次试探（此前永久卡死 half_open）"
