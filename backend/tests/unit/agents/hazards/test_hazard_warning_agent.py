"""
Warning Agent 测试套件（西藏五类山地地质灾害口径）

【两层设计】
  第一层：规则注册表 / 等级映射 / 决策门控 / 告警构建 / 消息校验——不依赖 LLM，必跑。
  第二层：端到端图测试；解释节点 LLM（GLM-4-Flash）失败走模板兜底，断言两种情况都通过。

【测试范围声明】只验证①流程运行 ②Mock 规则预设行为；
  不验证真实预警准确率（需历史案例，不在本期范围）。

【运行】
  cd C:\\Users\\lixin\\Desktop\\地政预警项目
  .venv\\Scripts\\python.exe -m agents.warning_agent.test_warning_agent
"""

import sys

from aegis.agents.hazards.hazard_registry import UnsupportedHazardError
from aegis.agents.hazards.warning_agent.nodes.alert_builder_node import alert_builder_node
from aegis.agents.hazards.warning_agent.nodes.hazard_warning_rule_node import hazard_warning_rule_node
from aegis.agents.hazards.warning_agent.nodes.warning_decision_node import warning_decision_node
from aegis.agents.hazards.warning_agent.nodes.warning_validator_node import warning_validator_node
from aegis.agents.hazards.warning_agent.tools.hazard_warning_rule_registry import (
    HAZARD_WARNING_RULES,
    get_hazard_warning_rules,
    get_suggested_actions,
    list_hazard_warning_capabilities,
)

ALL_HAZARDS = ["landslide", "rockfall", "debris_flow", "avalanche", "lake_outburst"]


def _observation(factor, value):
    """按 FactorObservation 口径构造一条观测（数值给 min/max/mean，类别给 class_value）。"""
    common = {"samples": 2, "observed_at": "2024-07-08T03:00:00.000Z", "sources": ["test"], "derivation": "test"}
    if isinstance(value, str):
        return {"factor": factor, "class_value": value, **common}
    return {
        "factor": factor,
        "min_value": float(value),
        "max_value": float(value),
        "mean_value": float(value),
        **common,
    }


def _observations(hazard, overrides=None):
    """按 Risk 注册表 sample_values 构造观测面（走真实 Risk 因子引擎）。"""
    values = dict(HAZARD_RISK_PROFILES_for_warning()[hazard]["sample_values"])
    if overrides:
        values.update(overrides)
    return [_observation(name, value) for name, value in values.items()]


def HAZARD_RISK_PROFILES_for_warning():
    """Warning 测试需要 Risk 的 sample_values 与因子引擎（跨包只读引用，仅测试用）。"""
    from aegis.agents.hazards.risk_agent.tools.hazard_risk_registry import HAZARD_RISK_PROFILES

    return HAZARD_RISK_PROFILES


def _risk_result(hazard, risk_status="success", overrides=None, drop_R=False, observations=None):
    """构造 Risk 最终交付物 Mock（走真实 Risk 因子引擎，字段契约一致）。"""
    profile = HAZARD_RISK_PROFILES_for_warning()[hazard]
    if observations is None:
        observations = _observations(hazard)
    assessment = profile["assessment_tool"](observations, "西藏")
    if drop_R:
        assessment["R"] = None
        assessment["assessment_status"] = "insufficient_data"
        assessment["insufficient_reasons"] = ["测试：人为置空 R"]
    out = {
        "status": risk_status,
        "hazard_type": hazard,
        "risk_level": assessment.get("provisional_level", "unknown"),
        "reasoning": "（Mock）因子引擎评估结果",
        "trigger_conditions": [f"{h['rule_id']}: {h['description']}" for h in assessment.get("rule_hits", [])],
        "assessment": assessment,
        "evidence": [],
        "uncertainties": ["（Mock）"],
        "validation": {"is_valid": True, "issues": [], "recommendation": "proceed"},
        "model_info": {"rule_version": assessment.get("rule_version", "MOCK-v0.1"), "implementation_status": "mock", "is_mock": True},
        "metadata": {"note": "Mock"},
    }
    if overrides:
        out.update(overrides)
    return out


def _decision_state(hazard, risk_result, simulation_result=None, data_result=None):
    return {
        "hazard_type": hazard,
        "risk_result": risk_result,
        "simulation_result": simulation_result or {},
        "data_result": data_result or {},
    }


# ============================================================
# 第一层
# ============================================================


def test_warning_registry():
    """规则注册表：五灾种独立声明，全部 Mock；地震不存在。"""
    assert sorted(HAZARD_WARNING_RULES.keys()) == sorted(ALL_HAZARDS)
    assert "earthquake" not in HAZARD_WARNING_RULES
    for c in list_hazard_warning_capabilities():
        assert c["is_mock"] is True and c["mapping_version"].startswith("MOCK-")
    try:
        get_hazard_warning_rules("火山泥流")
        raise AssertionError("应抛 UnsupportedHazardError")
    except UnsupportedHazardError:
        pass
    print("  √ test_warning_registry")


def test_decision_gating():
    """决策门控：正常可预警；灾种不一致/风险失败/评分未过/数据不足 → 拒绝。"""
    rr = _risk_result("debris_flow")
    d1 = warning_decision_node(_decision_state("debris_flow", rr))
    assert d1["decision_result"]["can_warn"] is True

    # 灾种不一致
    d2 = warning_decision_node(_decision_state("landslide", rr))
    assert d2["decision_result"]["can_warn"] is False
    assert any("不一致" in r for r in d2["decision_result"]["reasons"])

    # 风险失败
    d3 = warning_decision_node(_decision_state("debris_flow", _risk_result("debris_flow", risk_status="failed")))
    assert d3["decision_result"]["can_warn"] is False

    # 因子引擎未评分
    rr4 = _risk_result("debris_flow", drop_R=True)
    d4 = warning_decision_node(_decision_state("debris_flow", rr4))
    assert d4["decision_result"]["can_warn"] is False

    # 未知灾种
    d5 = warning_decision_node(_decision_state("火山泥流", rr))
    assert d5["decision_result"]["can_warn"] is False

    # 数据"部分可用" + Risk 已评分 → 不硬性拒绝（分层：Risk 覆盖率门控为准）
    partial_data = {"status": "partial", "data_sufficiency": {"sufficient": False, "reason": "部分可用"}}
    d6 = warning_decision_node(_decision_state("debris_flow", rr, data_result=partial_data))
    assert d6["decision_result"]["can_warn"] is True
    assert d6["decision_result"]["checks"]["data_sufficiency"]["note"]

    # Data 完全失败（status=failed）→ 拒绝
    failed_data = {"status": "failed", "data_sufficiency": {"sufficient": False, "reason": "获取失败"}}
    d7 = warning_decision_node(_decision_state("debris_flow", rr, data_result=failed_data))
    assert d7["decision_result"]["can_warn"] is False
    print("  √ test_decision_gating")


def test_rule_mapping_and_escalation():
    """等级映射：阈值分级 + 保护规则升级通道 + 五灾种独立参数。"""
    # 高 R → warning
    rr = _risk_result("debris_flow")
    n = hazard_warning_rule_node({"hazard_type": "debris_flow", "risk_result": rr})
    level = n["warning_rule_result"]["warning_level"]
    assert level in ("advisory", "watch", "warning")

    # 保护规则升级：泥石流上游溃决联动 → 强制 warning
    rr_hits = _risk_result("debris_flow", observations=_observations_override_hit("debris_flow"))
    n2 = hazard_warning_rule_node({"hazard_type": "debris_flow", "risk_result": rr_hits})
    assert n2["warning_rule_result"]["escalation_applied"] is True
    assert n2["warning_rule_result"]["warning_level"] == "warning"

    # R=0 且 risk_level=unknown → none
    rr0 = _risk_result("debris_flow")
    rr0["risk_level"] = "unknown"
    rr0["assessment"]["R"] = 0.0
    n3 = hazard_warning_rule_node({"hazard_type": "debris_flow", "risk_result": rr0})
    assert n3["warning_rule_result"]["warning_level"] == "none"
    print("  √ test_rule_mapping_and_escalation")


def _observations_override_hit(hazard):
    """构造命中保护规则的观测（泥石流：上游溃决联动=yes）。"""
    flag_fields = {"debris_flow": "upstream_breach_flag", "lake_outburst": "ice_avalanche_into_lake"}
    field = flag_fields.get(hazard)
    return _observations(hazard, {field: "yes"} if field else None)


def test_cap_mapping_and_errors():
    """CAP：正常映射 → 结构通过 + XML 含关键字段；缺字段/转换异常被捕获为明确错误。"""
    from aegis.agents.hazards.warning_agent.tools.cap_format_tool import (
        internal_alert_to_cap,
        serialize_cap_xml,
        validate_cap_dict,
    )

    rr = _risk_result("debris_flow")
    rule = hazard_warning_rule_node({"hazard_type": "debris_flow", "risk_result": rr})
    state = {
        "hazard_type": "debris_flow",
        "warning_rule_result": rule["warning_rule_result"],
        "risk_result": rr,
        "simulation_result": {},
        "data_result": {},
    }
    alert = alert_builder_node(state)["alert_object"]

    cap = internal_alert_to_cap(alert, explanation_text="Mock 模拟说明")
    assert cap["status"] == "Exercise", "mock_only=True 时 CAP status 必须为 Exercise"
    issues = validate_cap_dict(cap)
    assert issues == [], f"正常映射不应有结构问题: {issues}"
    xml = serialize_cap_xml(cap)
    assert alert["alert_id"] in xml and "Mock" in xml
    # 概念分离：CAP urgency 与内部 warning_level 不是同一概念
    assert cap["info"][0]["urgency"] != alert["warning_level"]
    assert cap["_mapping_version"].startswith("MOCK-")

    # 错误捕获：缺 identifier → 结构校验给出明确问题
    broken = dict(cap)
    broken["identifier"] = ""
    issues2 = validate_cap_dict(broken)
    assert any("identifier" in i for i in issues2)
    try:
        serialize_cap_xml(broken)
        raise AssertionError("缺必需字段应抛 ValueError")
    except ValueError as e:
        assert "identifier" in str(e)

    # none 等级 → 跳过 CAP
    none_alert = dict(alert)
    none_alert["warning_level"] = "none"
    assert internal_alert_to_cap(none_alert)["status"] == "skipped"
    print("  √ test_cap_mapping_and_errors")


def test_graph_cap_generated():
    """图级：正常流程 warning_result.cap.status=generated 且 XML 可用。"""
    final = _run_graph("生成泥石流预警", "debris_flow", _risk_result("debris_flow"))
    cap = final["warning_result"]["cap"]
    assert cap["status"] == "generated"
    assert "<alert>" in cap["xml"] and "Exercise" in cap["xml"]
    assert cap["validation_issues"] == []
    print("  √ test_graph_cap_generated")


def test_alert_builder():
    """告警构建：生命周期/Mock 标记/预留接口字段齐全，建议行动与等级匹配。"""
    rr = _risk_result("landslide")
    rule = hazard_warning_rule_node({"hazard_type": "landslide", "risk_result": rr})
    state = {
        "hazard_type": "landslide",
        "warning_rule_result": rule["warning_rule_result"],
        "risk_result": rr,
        "simulation_result": {},
        "data_result": {},
    }
    alert = alert_builder_node(state)["alert_object"]
    assert alert["alert_status"] == "draft" and alert["mock_only"] is True
    assert alert["official_grade"] is None
    assert alert["alert_id"].startswith("ALT-LANDSLIDE-")
    assert alert["delivery"]["send_status"] == "not_implemented"
    assert alert["cap"]["status"] == "not_implemented"
    assert alert["suggested_actions"] == get_suggested_actions("landslide", alert["warning_level"])
    print("  √ test_alert_builder")


def test_validator_checks():
    """消息校验：必要字段/官方等级禁用/Mock 披露/建议行动。"""
    rr = _risk_result("debris_flow")
    rule = hazard_warning_rule_node({"hazard_type": "debris_flow", "risk_result": rr})
    state = {
        "hazard_type": "debris_flow",
        "warning_rule_result": rule["warning_rule_result"],
        "risk_result": rr,
        "simulation_result": {},
        "data_result": {},
    }
    alert = alert_builder_node(state)["alert_object"]
    level = alert["warning_level"]
    level_cn = {"warning": "警报", "watch": "关注", "advisory": "提示", "none": "无预警建议"}[level]
    good_explanation = {
        "text": (
            f"泥石流（debris_flow）当前为{level_cn}（Mock 模拟），"
            f"依据 R={alert.get('based_on', {}).get('R')}（原型候选公式）。"
            "数据为 Mock 构造，结果具有不确定性，禁止用于实际发布。"
        ),
        "cautions": ["Mock 模拟产物"],
    }
    v1 = warning_validator_node(
        {"alert_object": alert, "explanation": good_explanation, "decision_result": {"can_warn": True}, "retry_count": 0, "max_retries": 3}
    )
    assert v1["validation_result"]["is_valid"] is True
    assert v1["validation_result"]["recommendation"] == "proceed"

    # 官方等级措辞 → 问题
    bad = dict(good_explanation)
    bad["text"] = "泥石流红色预警！立即转移！"
    v2 = warning_validator_node(
        {"alert_object": alert, "explanation": bad, "decision_result": {"can_warn": True}, "retry_count": 0, "max_retries": 3}
    )
    assert v2["validation_result"]["recommendation"] in ("retry", "fail")
    assert any("官方等级" in i for i in v2["validation_result"]["issues"])

    # 缺少 Mock 披露 → 问题
    bad2 = dict(good_explanation)
    bad2["text"] = "泥石流警报（确定性结论，不存在不确定性）"
    v3 = warning_validator_node(
        {"alert_object": alert, "explanation": bad2, "decision_result": {"can_warn": True}, "retry_count": 0, "max_retries": 3}
    )
    assert v3["validation_result"]["recommendation"] in ("retry", "fail")
    assert any("Mock" in i or "披露" in i for i in v3["validation_result"]["issues"])
    print("  √ test_validator_checks")


# ============================================================
# 第二层：端到端图测试（解释 LLM 失败走模板兜底，断言两种情况都通过）
# ============================================================


def _run_graph(task, hazard_type, risk_result, simulation_result=None, data_result=None):
    from aegis.agents.hazards.warning_agent.graph import create_warning_agent

    state = {
        "task": task,
        "hazard_type": hazard_type,
        "risk_result": risk_result,
        "simulation_result": simulation_result or {},
        "data_result": data_result or {},
        "decision_result": {},
        "warning_rule_result": {},
        "alert_object": {},
        "explanation": {},
        "validation_result": {},
        "retry_count": 0,
        "max_retries": 3,
        "next": "",
        "error_message": "",
        "status": "",
        "warning_result": {},
    }
    return create_warning_agent().invoke(state)


def test_graph_normal_per_hazard():
    """五灾种正常：status=draft、alert 生命周期与 Mock 标记齐全、cap 预留。"""
    for key in ALL_HAZARDS:
        final = _run_graph(f"生成{key}预警", key, _risk_result(key))
        out = final["warning_result"]
        assert final["status"] == "draft", f"{key} 应产出草稿: {out.get('reasons')}"
        alert = out["alert"]
        assert alert["hazard_type"] == key
        assert alert["mock_only"] is True and alert["alert_status"] == "draft"
        assert alert["official_grade"] is None
        assert alert["based_on"]["R"] is not None
        assert out["metadata"]["note"]
    print("  √ test_graph_normal_per_hazard（5 灾种）")


def test_graph_insufficient():
    """未知灾种 / Risk 状态失败 → insufficient_data，不生成告警对象。"""
    final = _run_graph("生成预警", "火山泥流", _risk_result("debris_flow"))
    assert final["status"] == "insufficient_data"
    assert final["warning_result"]["alert_object"] is None

    final2 = _run_graph("生成预警", "debris_flow", _risk_result("debris_flow", risk_status="failed"))
    assert final2["status"] == "insufficient_data"
    print("  √ test_graph_insufficient")


def test_graph_explanation_llm():
    """[可能依赖 LLM] 解释节点真实调用；失败自动模板兜底。"""
    final = _run_graph("生成泥石流预警", "debris_flow", _risk_result("debris_flow"))
    out = final["warning_result"]
    assert out["explanation"]["text"]
    assert out["explanation"]["used_llm"] in (True, False)
    print(f"  √ test_graph_explanation_llm  used_llm={out['explanation']['used_llm']}")


# ============================================================
# 运行入口
# ============================================================

if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    print("=" * 60)
    print("Warning Agent 测试套件（西藏五类山地灾害口径）")
    print("范围：①流程运行 ②Mock 规则预设行为；不含真实预警准确率验证")
    print("=" * 60)

    print("\n[第一层] 规则注册表 / 决策门控 / 等级映射 / 告警构建 / 消息校验")
    test_warning_registry()
    test_decision_gating()
    test_rule_mapping_and_escalation()
    test_alert_builder()
    test_validator_checks()

    print("\n[第二层] 端到端图测试")
    test_cap_mapping_and_errors()
    test_graph_normal_per_hazard()
    test_graph_insufficient()
    test_graph_explanation_llm()
    test_graph_cap_generated()

    print("\n" + "=" * 60)
    print("全部测试通过 ✅")
    print("=" * 60)
