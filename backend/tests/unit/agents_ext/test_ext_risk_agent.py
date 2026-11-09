"""Risk Agent 测试套件（西藏五类山地地质灾害，因子化口径，不含地震）

【定位】回归测试。以后改 Risk 代码后，跑这个确认没改坏。

【输入口径已换】
  旧口径喂 `data_result.processed_data.events`（别的 Agent 的 State 平铺包）；
  现在喂 `assess.v1` 证据包的观测面 `FactorObservation` dump 列表——按因子名取用。
  两条入口：工具层（`*_factor_assessment_tool`）直调，图级（`run_risk_agent`）走统一契约。

【LLM 隔离】
  图里的解释/验证节点会调 LLM。本套件用 autouse fixture 把两个节点的
  `get_risk_llm` 换成"必抛"桩，验证失败走规则兜底、解释失败走模板兜底——
  跑测试不拨号、不花钱、不依赖网络。

【测试范围声明】
  本套件只验证：①流程正确运行；②Mock 公式按预设行为工作。
  不验证：③专业模型效力；④真实预警准确率（需历史案例验证，不在本期范围）。
"""

from __future__ import annotations

import importlib
from typing import Any

import pytest

from aegis.agents_ext.hazard_registry import (
    UnsupportedHazardError,
    normalize_hazard_type,
    require_hazard,
)
from aegis.agents_ext.risk_agent.errors import AssessRejected
from aegis.agents_ext.risk_agent.main import run_risk_agent
from aegis.agents_ext.risk_agent.tools.factor_engine import (
    R_FORMULA_EXPRESSION,
    R_FORMULA_STATUS,
    normalize_value,
)
from aegis.agents_ext.risk_agent.tools.hazard_risk_registry import (
    HAZARD_RISK_PROFILES,
    list_hazard_risk_capabilities,
)
from aegis.agents_ext.risk_agent.tools.risk_evidence_tool import risk_evidence_tool
from aegis.domain.assess import AssessErrorCode, AssessEvidence, AssessRequest, FactorObservation

ALL_HAZARDS = ["landslide", "rockfall", "debris_flow", "avalanche", "lake_outburst"]
TS = "2024-07-08T03:00:00.000Z"

_MODULES = {
    "landslide": "aegis.agents_ext.risk_agent.tools.landslide_risk_tool",
    "rockfall": "aegis.agents_ext.risk_agent.tools.rockfall_risk_tool",
    "debris_flow": "aegis.agents_ext.risk_agent.tools.debris_flow_risk_tool",
    "avalanche": "aegis.agents_ext.risk_agent.tools.avalanche_risk_tool",
    "lake_outburst": "aegis.agents_ext.risk_agent.tools.glacial_lake_risk_tool",
}


# ============================================================
# LLM 隔离：解释/验证节点不拨号
# ============================================================


class _BrokenStructured:
    def invoke(self, _messages: Any) -> Any:
        raise RuntimeError("测试禁网：LLM 不可用（走模板/规则兜底）")


class _BrokenLlm:
    def with_structured_output(self, _schema: Any, **_kw: Any) -> _BrokenStructured:
        return _BrokenStructured()


@pytest.fixture(autouse=True)
def _no_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    for module_name in (
        "aegis.agents_ext.risk_agent.nodes.risk_assess_node",
        "aegis.agents_ext.risk_agent.nodes.risk_validate_node",
    ):
        monkeypatch.setattr(importlib.import_module(module_name), "get_risk_llm", lambda: _BrokenLlm())


# ============================================================
# 构造：按注册表 sample_values 生成 FactorObservation
# ============================================================


def _observation(factor: str, value: Any) -> FactorObservation:
    common = {"factor": factor, "samples": 2, "observed_at": TS, "sources": ["test"], "derivation": "test"}
    if isinstance(value, str):
        return FactorObservation(class_value=value, **common)
    return FactorObservation(min_value=float(value), max_value=float(value), mean_value=float(value), **common)


def _observations(hazard: str, overrides: dict[str, Any] | None = None, drop_fields: list[str] | None = None) -> list[dict]:
    values = dict(HAZARD_RISK_PROFILES[hazard]["sample_values"])
    if overrides:
        values.update(overrides)
    for field in drop_fields or []:
        values.pop(field, None)
    return [_observation(name, value).model_dump(mode="json") for name, value in values.items()]


def _assessment(hazard: str, **kwargs: Any) -> dict:
    module = importlib.import_module(_MODULES[hazard])
    return module.FACTOR_CONFIG, module


# ============================================================
# 第一层：注册表与因子引擎（不依赖 LLM，必跑）
# ============================================================


def test_registry_five_hazards() -> None:
    """注册表：恰含五个灾种（无地震）；全部 mock 状态并声明局限。"""
    assert sorted(HAZARD_RISK_PROFILES.keys()) == sorted(ALL_HAZARDS)
    assert "earthquake" not in HAZARD_RISK_PROFILES
    for capability in list_hazard_risk_capabilities():
        assert capability["implementation_status"] == "mock"
        assert capability["rule_version"].startswith("MOCK-")
        assert capability["current_limitations"]


def test_registry_normalization_uses_platform_keys() -> None:
    """灾种归一化：标准键与平台一致；旧交付键只在别名表里作为别名。"""
    assert normalize_hazard_type("泥石流") == "debris_flow"
    assert normalize_hazard_type("冰湖") == "lake_outburst"
    assert normalize_hazard_type("ice_rock_snow_avalanche") == "avalanche"
    assert normalize_hazard_type("glacial_lake_outburst") == "lake_outburst"
    assert normalize_hazard_type("火山泥流") == ""
    with pytest.raises(UnsupportedHazardError):
        require_hazard("earthquake")


def test_weight_schema() -> None:
    """权重 schema：每灾种 P/T 组权重非负、和为 1，且来源四要素齐全。"""
    for key in ALL_HAZARDS:
        module = importlib.import_module(_MODULES[key])
        config = module.FACTOR_CONFIG
        for group, factors in config["groups"].items():
            total = 0.0
            for factor in factors:
                weight = factor["weight"]
                assert weight >= 0, f"{key}.{group}.{factor['key']} 权重非负"
                total += weight
                source = factor["weight_source"]
                assert set(source.keys()) >= {"source_reference", "method", "valid_region", "validation_status"}, (
                    f"{key}.{group}.{factor['key']} 权重来源四要素必须齐全"
                )
            assert abs(total - 1.0) < 1e-9, f"{key}.{group} 权重之和应为 1，实际 {total}"


def test_normalize_methods() -> None:
    """因子引擎：五种归一化方法的边界行为。"""
    assert normalize_value(150.0, {"method": "linear_pos", "L": 0.0, "U": 150.0})[0] == 1.0
    assert normalize_value(75.0, {"method": "linear_pos", "L": 0.0, "U": 150.0})[0] == 0.5
    assert normalize_value(-5.0, {"method": "linear_pos", "L": 0.0, "U": 150.0})[0] == 0.0
    assert normalize_value(0.0, {"method": "linear_neg", "L": 0.0, "U": 50.0})[0] == 1.0
    assert normalize_value(50.0, {"method": "linear_neg", "L": 0.0, "U": 50.0})[0] == 0.0
    z, _ = normalize_value(100.0, {"method": "log_pos", "L": 10.0, "U": 10000.0})
    assert abs(z - 1 / 3) < 1e-9
    assert normalize_value(125.0, {"method": "sqrt_pos", "U": 500.0})[0] == 0.5
    band = {"method": "band_pos", "L": 20.0, "band_lo": 30.0, "band_hi": 45.0, "U": 60.0}
    assert normalize_value(38.0, band)[0] == 1.0
    assert normalize_value(20.0, band)[0] == 0.0
    assert normalize_value("yes", {"method": "class_map", "class_map": {"no": 0.0, "yes": 1.0}})[0] == 1.0
    z_bad, note = normalize_value("volcano", {"method": "class_map", "class_map": {"no": 0.0}})
    assert z_bad is None and "不在分类映射" in note


def test_engine_formula_declaration() -> None:
    """公式身份声明：原型候选公式，非规范条款、非任务书公式。"""
    assert R_FORMULA_EXPRESSION == "R_h(t) = 100 × P_h × T_h(t)"
    assert R_FORMULA_STATUS == "prototype_candidate"
    _, module = _assessment("landslide")
    env = module.landslide_factor_assessment_tool(_observations("landslide"), "540121")
    assert env["formula"]["status"] == "prototype_candidate"
    assert "不是课题任务书规定的公式" in env["formula"]["note"]


def test_engine_gating_required_missing() -> None:
    """数据门控：必需因子缺失 → insufficient_data；R 不输出（缺失不填 0）。"""
    _, module = _assessment("landslide")
    env = module.landslide_factor_assessment_tool(_observations("landslide", drop_fields=["rainfall_mm_h"]), "540121")
    assert env["assessment_status"] == "insufficient_data"
    assert env["R"] is None, "数据不足时不得输出组合指数"
    assert "rainfall_mm_h" in env["missing_factors"]
    assert any("必需因子缺失" in reason for reason in env["insufficient_reasons"])


def test_engine_coverage_renormalization() -> None:
    """覆盖门控：可选因子缺失 → 按剩余权重重归一，得分连续变化且披露缺失。"""
    _, module = _assessment("debris_flow")
    full = module.debris_flow_factor_assessment_tool(_observations("debris_flow"), "540121")
    partial = module.debris_flow_factor_assessment_tool(_observations("debris_flow", drop_fields=["sediment_supply_index"]), "540121")
    assert full["assessment_status"] == "scored"
    assert partial["assessment_status"] == "scored", "非必需缺失不门控"
    assert "sediment_supply_index" in partial["missing_factors"]
    assert partial["completeness"]["P"] < 1.0
    assert 0 < partial["P"] <= 1.0
    assert partial["coverage_basis"] == "weighted_factor_coverage"


def test_engine_protected_rules() -> None:
    """保护规则：扳机命中独立于指数（冰崩入湖 / 溃决联动）。"""
    _, module = _assessment("lake_outburst")
    env = module.glacial_lake_factor_assessment_tool(_observations("lake_outburst"), "540121")
    hit_ids = [hit["rule_id"] for hit in env["rule_hits"]]
    assert "GL-ICE-AVALANCHE-SURGE" in hit_ids
    assert all(hit["validation_status"] == "mock" for hit in env["rule_hits"])

    _, df_module = _assessment("debris_flow")
    env2 = df_module.debris_flow_factor_assessment_tool(_observations("debris_flow", overrides={"upstream_breach_flag": "yes"}), "540121")
    assert "DF-OUTBURST-LINK" in [hit["rule_id"] for hit in env2["rule_hits"]]

    df_no = _observations("debris_flow", overrides={"upstream_breach_flag": "no", "spring_surge_flag": "no"})
    assert df_module.debris_flow_factor_assessment_tool(df_no, "540121")["rule_hits"] == []


def test_engine_route_variants() -> None:
    """路由变体：崩塌同数据不同破坏方式 → 触发权重不同 → T/R 不同；缺路由走默认。"""
    _, module = _assessment("rockfall")
    env_slide = module.rockfall_factor_assessment_tool(_observations("rockfall", overrides={"failure_mode": "sliding"}), "540121")
    env_topple = module.rockfall_factor_assessment_tool(_observations("rockfall", overrides={"failure_mode": "toppling"}), "540121")
    assert env_slide["route_value"] == "sliding"
    assert env_topple["T"] != env_slide["T"] or env_topple["R"] != env_slide["R"], "不同破坏方式的触发权重变体应产生不同触发评分"
    env_default = module.rockfall_factor_assessment_tool(_observations("rockfall", drop_fields=["failure_mode"]), "540121")
    assert env_default["route_value"] is None
    assert env_default["warnings"], "缺路由因子应有警示说明"


def test_per_hazard_assessment_normal() -> None:
    """五灾种正常评估：scored、R=100·P·T、因子 schema 完整、Mock 声明齐全。"""
    for key in ALL_HAZARDS:
        profile = HAZARD_RISK_PROFILES[key]
        env = profile["assessment_tool"](_observations(key), "540121")
        assert env["assessment_status"] == "scored", f"{key} 正常数据不应 insufficient"
        assert 0 <= env["P"] <= 1 and 0 <= env["T"] <= 1
        assert env["R"] == round(100.0 * env["P"] * env["T"], 2)
        assert env["provisional_level"] in ("none", "low", "medium", "high")
        for spec in env["factor_scores"].values():
            assert spec.get("weight_source")
            assert spec["data_status"] in ("mock", "literature", "real", "pending")
        assert env["rule_version"].startswith("MOCK-")
        assert "MOCK" in env["disclaimer"]
        assert env["sample_count"] > 0


def test_cross_hazard_isolation() -> None:
    """灾种隔离：冰湖数据喂滑坡因子配置 → 缺必需因子 → insufficient，不产错误等级。"""
    profile = HAZARD_RISK_PROFILES["landslide"]
    env = profile["assessment_tool"](_observations("lake_outburst"), "540121")
    assert env["assessment_status"] == "insufficient_data"
    assert "rainfall_mm_h" in env["missing_factors"]
    env2 = HAZARD_RISK_PROFILES["lake_outburst"]["assessment_tool"](_observations("landslide"), "540121")
    assert env2["assessment_status"] == "insufficient_data"


def test_evidence_tool_generic() -> None:
    """证据工具：观测/推导/局限三分类 + 公式声明 + 缺失因子披露。"""
    env = HAZARD_RISK_PROFILES["lake_outburst"]["assessment_tool"](_observations("lake_outburst"), "540121")
    limits = HAZARD_RISK_PROFILES["lake_outburst"]["current_limitations"]
    evidence = risk_evidence_tool(env, hazard_limitations=limits)
    types = [item["type"] for item in evidence]
    assert "observation" in types and "limitation" in types and "derived" in types
    assert any("候选组合公式" in item["statement"] and "R=" in item["statement"] for item in evidence if item["type"] == "observation")
    assert any("mock" in item.get("caveat", "").lower() for item in evidence if item["type"] == "derived")


# ============================================================
# 第二层：图级测试（统一契约入口，LLM 走兜底）
# ============================================================


def _evidence(hazard: str, *, region: str = "540121", observations: list[dict] | None = None) -> AssessEvidence:
    items = _observations(hazard) if observations is None else observations
    return AssessEvidence(
        region_code=region,
        window="60m",
        generated_at=TS,
        observations=[FactorObservation.model_validate(item) for item in items],
        provenance={"data_kind": "mock", "sources": ["test"], "method": "mock_agent", "is_mock": True},
    )


def _request(hazard: str, *, region: str = "540121") -> AssessRequest:
    return AssessRequest(region_code=region, window="60m", hazard_type=hazard)


def _run_graph(hazard: str) -> dict:
    response = run_risk_agent(_request(hazard), _evidence(hazard))
    return response.model_dump(mode="json")


def test_graph_normal_per_hazard() -> None:
    """五灾种正常输入：响应合契约、等级可映射、置信度封顶、出处为 mock 方法。"""
    for key in ALL_HAZARDS:
        out = _run_graph(key)
        assert out["hazard_type"] == key
        assert out["region_code"] == "540121"
        assert 1 <= out["risk_level"] <= 5
        assert 0 < out["confidence"] <= 0.60, f"{key} 置信度必须在 (0, 0.60]"
        assert out["confidence_basis"].startswith("coverage=")
        assert out["data_provenance"]["method"] == "factor_engine_mock"
        assert out["data_provenance"]["is_mock"] is True
        assert out["human_review_suggested"] is True
        assert out["source"] == "agents_ext_mock"
        assert out["assessment"]["formula"]["status"] == "prototype_candidate"
        assert out["assessment"]["R"] is not None, f"{key} 正常数据应有 R 值"
        assert out["assessment"]["sample_count"] > 0


def test_graph_missing_hazard() -> None:
    """请求里没有灾种（hits 也为空）→ E_ASSESS_INSUFFICIENT，绝不猜灾种。"""
    request = AssessRequest(region_code="540121", window="60m")
    with pytest.raises(AssessRejected) as exc_info:
        run_risk_agent(request, _evidence("debris_flow"))
    assert exc_info.value.error_code == AssessErrorCode.INSUFFICIENT
    assert exc_info.value.retryable is False


def test_graph_unknown_hazard() -> None:
    """枚举里合法但外部实现不支持的灾种（地震触发）→ E_ASSESS_HAZARD_UNSUP，原因含支持清单。"""
    with pytest.raises(AssessRejected) as exc_info:
        run_risk_agent(_request("quake_triggered"), _evidence("debris_flow"))
    assert exc_info.value.error_code == AssessErrorCode.HAZARD_UNSUP
    assert "landslide" in exc_info.value.message


def test_graph_unknown_hazard_from_hits() -> None:
    """hazard_type 缺失时取 hits 里的灾种；hits 给的是未知串 → E_ASSESS_HAZARD_UNSUP。"""
    request = AssessRequest(
        region_code="540121",
        window="60m",
        hits=[{"rule_id": "R-X-1", "hazard_type": "volcano", "region_code": "540121", "score": 0.9}],
    )
    with pytest.raises(AssessRejected) as exc_info:
        run_risk_agent(request, _evidence("debris_flow"))
    assert exc_info.value.error_code == AssessErrorCode.HAZARD_UNSUP


def test_graph_empty_observations() -> None:
    """证据包没有观测 → E_ASSESS_INSUFFICIENT（不是"无风险"）。"""
    with pytest.raises(AssessRejected) as exc_info:
        run_risk_agent(_request("debris_flow"), _evidence("debris_flow", observations=[]))
    assert exc_info.value.error_code == AssessErrorCode.INSUFFICIENT


def test_graph_missing_required_fields() -> None:
    """缺必需因子 → E_ASSESS_INSUFFICIENT，detail 带缺失信息。"""
    evidence = _evidence("debris_flow", observations=_observations("debris_flow", drop_fields=["rainfall_mm_h"]))
    with pytest.raises(AssessRejected) as exc_info:
        run_risk_agent(_request("debris_flow"), evidence)
    assert exc_info.value.error_code == AssessErrorCode.INSUFFICIENT
    assert "rainfall_mm_h" in exc_info.value.message or "必需因子" in exc_info.value.message


def test_graph_alias_cn() -> None:
    """中文别名经请求归一化不可直达契约（HazardType 枚举拒收），但注册表别名仍可解析。"""
    from pydantic import ValidationError

    assert normalize_hazard_type("泥石流") == "debris_flow"
    with pytest.raises(ValidationError):
        AssessRequest(region_code="540121", window="60m", hazard_type="泥石流")


def test_graph_r_zero_maps_to_none_not_unknown() -> None:
    """R==0 → provisional_level=none → 平台 NONE(5)；"算出来没风险"不能被写成"算不出来"。"""
    # 零值触发因子使 R==0：雨量 0、积雪 0（均为合法观测，不是缺失）
    zeros = _observations("debris_flow", overrides={"rainfall_mm_h": 0.0, "snow_depth_cm": 0.0})
    out = run_risk_agent(_request("debris_flow"), _evidence("debris_flow", observations=zeros))
    assert out.risk_level.value == 5
    assert out.assessment["R"] == 0


def test_nodes_log_instead_of_print(capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture) -> None:
    """图跑一轮：节点日志走 logging，stdout 不再冒出调试行。"""
    with caplog.at_level("INFO", logger="aegis.agents_ext.risk_agent.nodes"):
        _run_graph("debris_flow")

    captured = capsys.readouterr()
    assert "[RiskAssess]" not in captured.out
    assert "[RiskValidate]" not in captured.out
    assert "[RiskOutput]" not in captured.out
    names = {record.name for record in caplog.records}
    assert "aegis.agents_ext.risk_agent.nodes.risk_assess_node" in names
    assert "aegis.agents_ext.risk_agent.nodes.risk_validate_node" in names
    assert "aegis.agents_ext.risk_agent.nodes.risk_output_node" in names
