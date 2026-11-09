"""
Risk Agent 测试套件（西藏五类山地地质灾害，因子化口径，不含地震）

【定位】回归测试。以后改 Risk 代码后，跑这个确认没改坏。

【两层设计】
  第一层：注册表/权重 schema/因子引擎/保护规则/路由变体/跨灾种隔离——
         不依赖 LLM，没配 API key 也能跑。
  第二层：端到端图测试；解释节点 LLM（智谱 GLM-4-Flash）失败时走模板兜底，
         两种情况下断言都必须通过。

【测试范围声明】
  本套件只验证：①流程正确运行；②Mock 公式按预设行为工作。
  不验证：③专业模型效力；④真实预警准确率（需历史案例验证，不在本期范围）。

【运行】
  cd C:\\Users\\lixin\\Desktop\\地政预警项目
  .venv\\Scripts\\python.exe -m agents.risk_agent.test_risk_agent
"""

import sys

from aegis.agents_ext.hazard_registry import (
    UnsupportedHazardError,
    normalize_hazard_type,
    require_hazard,
)
from aegis.agents_ext.risk_agent.tools.hazard_risk_registry import (
    HAZARD_RISK_PROFILES,
    get_hazard_risk_profile,
    list_hazard_risk_capabilities,
)
from aegis.agents_ext.risk_agent.tools.factor_engine import (
    R_FORMULA_EXPRESSION,
    R_FORMULA_STATUS,
    compute_factor_assessment,
    normalize_value,
)
from aegis.agents_ext.risk_agent.tools.landslide_risk_tool import landslide_factor_assessment_tool
from aegis.agents_ext.risk_agent.tools.rockfall_risk_tool import rockfall_factor_assessment_tool
from aegis.agents_ext.risk_agent.tools.debris_flow_risk_tool import debris_flow_factor_assessment_tool
from aegis.agents_ext.risk_agent.tools.avalanche_risk_tool import avalanche_factor_assessment_tool
from aegis.agents_ext.risk_agent.tools.glacial_lake_risk_tool import glacial_lake_factor_assessment_tool
from aegis.agents_ext.risk_agent.tools.risk_evidence_tool import risk_evidence_tool

ALL_HAZARDS = ["landslide", "rockfall", "debris_flow",
               "ice_rock_snow_avalanche", "glacial_lake_outburst"]


def _events(hazard, n=2, overrides=None, drop_fields=None):
    """按注册表 sample_values 构造观测记录；可指定缺失字段。"""
    profile = HAZARD_RISK_PROFILES[hazard]
    out = []
    for i in range(1, n + 1):
        ev = {"event_id": f"{hazard[:2].upper()}-{i:03d}",
              "time": "2024-07-08T03:00:00Z",
              "latitude": 29.6 + i * 0.01, "longitude": 95.3 + i * 0.01,
              "region": "西藏"}
        ev.update(profile.get("sample_values", {}))
        out.append(ev)
    if overrides:
        for ev in out:
            ev.update(overrides)
    if drop_fields:
        for ev in out:
            for f in drop_fields:
                ev[f] = None
    return out


# ============================================================
# 第一层：注册表与因子引擎（不依赖 LLM，必跑）
# ============================================================

def test_registry_five_hazards():
    """注册表：恰含五个灾种（无地震）；全部 mock 状态并声明局限。"""
    assert sorted(HAZARD_RISK_PROFILES.keys()) == sorted(ALL_HAZARDS)
    assert "earthquake" not in HAZARD_RISK_PROFILES
    for c in list_hazard_risk_capabilities():
        assert c["implementation_status"] == "mock"
        assert c["rule_version"].startswith("MOCK-")
        assert c["current_limitations"]
    print("  √ test_registry_five_hazards")


def test_registry_normalization():
    """灾种归一化：中文别名 / 未知灾种 / 地震被拒绝。"""
    assert normalize_hazard_type("泥石流") == "debris_flow"
    assert normalize_hazard_type("冰湖") == "glacial_lake_outburst"
    assert normalize_hazard_type("火山泥流") == ""
    try:
        require_hazard("earthquake")
        raise AssertionError("地震不应是受支持灾种")
    except UnsupportedHazardError:
        pass
    print("  √ test_registry_normalization")


def test_weight_schema():
    """权重 schema：每灾种 P/T 组权重非负、和为 1，且来源四要素齐全。"""
    for key in ALL_HAZARDS:
        config = HAZARD_RISK_PROFILES[key]
        # 通过评估工具所在模块取 FACTOR_CONFIG：直接调工具模块常量
        import importlib
        module_name = {
            "landslide": "aegis.agents_ext.risk_agent.tools.landslide_risk_tool",
            "rockfall": "aegis.agents_ext.risk_agent.tools.rockfall_risk_tool",
            "debris_flow": "aegis.agents_ext.risk_agent.tools.debris_flow_risk_tool",
            "ice_rock_snow_avalanche": "aegis.agents_ext.risk_agent.tools.avalanche_risk_tool",
            "glacial_lake_outburst": "aegis.agents_ext.risk_agent.tools.glacial_lake_risk_tool",
        }[key]
        cfg = importlib.import_module(module_name).FACTOR_CONFIG
        for group, factors in cfg["groups"].items():
            total = 0.0
            for f in factors:
                w = f["weight"]
                assert w >= 0, f"{key}.{group}.{key} 权重非负"
                total += w
                ws = f["weight_source"]
                assert set(ws.keys()) >= {"source_reference", "method",
                                           "valid_region", "validation_status"}, \
                    f"{key}.{group}.{f['key']} 权重来源四要素必须齐全"
            assert abs(total - 1.0) < 1e-9, f"{key}.{group} 权重之和应为 1，实际 {total}"
    print("  √ test_weight_schema（5 灾种 P/T 组）")


def test_normalize_methods():
    """因子引擎：五种归一化方法的边界行为。"""
    # linear_pos
    assert normalize_value(150.0, {"method": "linear_pos", "L": 0.0, "U": 150.0})[0] == 1.0
    assert normalize_value(75.0, {"method": "linear_pos", "L": 0.0, "U": 150.0})[0] == 0.5
    assert normalize_value(-5.0, {"method": "linear_pos", "L": 0.0, "U": 150.0})[0] == 0.0
    # linear_neg（地下水埋深越浅越危险）
    assert normalize_value(0.0, {"method": "linear_neg", "L": 0.0, "U": 50.0})[0] == 1.0
    assert normalize_value(50.0, {"method": "linear_neg", "L": 0.0, "U": 50.0})[0] == 0.0
    # log_pos：(log10(100)-log10(10)) / (log10(10000)-log10(10)) = 1/3
    z, _ = normalize_value(100.0, {"method": "log_pos", "L": 10.0, "U": 10000.0})
    assert abs(z - 1 / 3) < 1e-9
    # sqrt_pos
    assert normalize_value(125.0, {"method": "sqrt_pos", "U": 500.0})[0] == 0.5
    # band_pos（文献候选带：带内=1，带外线性衰减）
    assert normalize_value(38.0, {"method": "band_pos", "L": 20.0, "band_lo": 30.0,
                                   "band_hi": 45.0, "U": 60.0})[0] == 1.0
    z_out, _ = normalize_value(20.0, {"method": "band_pos", "L": 20.0, "band_lo": 30.0,
                                       "band_hi": 45.0, "U": 60.0})
    assert z_out == 0.0
    # class_map：映射内/外
    assert normalize_value("yes", {"method": "class_map", "class_map": {"no": 0.0, "yes": 1.0}})[0] == 1.0
    z_bad, note = normalize_value("volcano", {"method": "class_map", "class_map": {"no": 0.0}})
    assert z_bad is None and "不在分类映射" in note
    print("  √ test_normalize_methods")


def test_engine_formula_declaration():
    """公式身份声明：原型候选公式，非规范条款、非任务书公式。"""
    assert R_FORMULA_EXPRESSION == "R_h(t) = 100 × P_h × T_h(t)"
    assert R_FORMULA_STATUS == "prototype_candidate"
    env = landslide_factor_assessment_tool(_events("landslide"), "西藏")
    assert env["formula"]["status"] == "prototype_candidate"
    assert "不是课题任务书规定的公式" in env["formula"]["note"]
    print("  √ test_engine_formula_declaration")


def test_engine_gating_required_missing():
    """数据门控：必需因子缺失 → insufficient_data；R 不输出（缺失不填 0），P/T 保留供诊断。"""
    events = _events("landslide", drop_fields=["rainfall_mm_h"])
    env = landslide_factor_assessment_tool(events, "西藏")
    assert env["assessment_status"] == "insufficient_data"
    assert env["R"] is None, "数据不足时不得输出组合指数"
    assert "rainfall_mm_h" in env["missing_factors"]
    assert any("必需因子缺失" in r for r in env["insufficient_reasons"])
    print("  √ test_engine_gating_required_missing")


def test_engine_coverage_renormalization():
    """覆盖门控：可选因子缺失 → 按剩余权重重归一，得分连续变化且披露缺失。"""
    full = _events("debris_flow")
    partial = _events("debris_flow", drop_fields=["sediment_supply_index"])
    env_full = debris_flow_factor_assessment_tool(full, "西藏")
    env_part = debris_flow_factor_assessment_tool(partial, "西藏")
    assert env_full["assessment_status"] == "scored"
    assert env_part["assessment_status"] == "scored"          # 非必需缺失不门控
    assert "sediment_supply_index" in env_part["missing_factors"]
    assert env_part["completeness"]["P"] < 1.0                 # 覆盖率如实披露
    assert env_part["P"] > 0 and env_part["P"] <= 1.0
    print("  √ test_engine_coverage_renormalization")


def test_engine_protected_rules():
    """保护规则：扳机命中独立于指数（冰崩入湖 / 溃决联动 / 严重渗漏）。"""
    gl = _events("glacial_lake_outburst")   # sample_values 冰崩入湖=yes
    env = glacial_lake_factor_assessment_tool(gl, "西藏")
    hit_ids = [h["rule_id"] for h in env["rule_hits"]]
    assert "GL-ICE-AVALANCHE-SURGE" in hit_ids
    assert all(h["validation_status"] == "mock" for h in env["rule_hits"])

    df = _events("debris_flow", overrides={"upstream_breach_flag": "yes"})
    env2 = debris_flow_factor_assessment_tool(df, "西藏")
    assert "DF-OUTBURST-LINK" in [h["rule_id"] for h in env2["rule_hits"]]

    # 未命中：全部 no → 无规则命中
    df_no = _events("debris_flow", overrides={"upstream_breach_flag": "no",
                                               "spring_surge_flag": "no"})
    assert debris_flow_factor_assessment_tool(df_no, "西藏")["rule_hits"] == []
    print("  √ test_engine_protected_rules")


def test_engine_route_variants():
    """路由变体：崩塌同数据不同破坏方式 → 触发权重不同 → T/R 不同；缺路由走默认。"""
    base = _events("rockfall")
    env_slide = rockfall_factor_assessment_tool(
        _events("rockfall", overrides={"failure_mode": "sliding"}), "西藏")
    env_topple = rockfall_factor_assessment_tool(
        _events("rockfall", overrides={"failure_mode": "toppling"}), "西藏")
    assert env_slide["route_value"] == "sliding"
    assert env_topple["T"] != env_slide["T"] or env_topple["R"] != env_slide["R"], \
        "不同破坏方式的触发权重变体应产生不同触发评分"
    # 缺路由因子 → 默认权重 + 警示
    no_route = _events("rockfall", drop_fields=["failure_mode"])
    env_default = rockfall_factor_assessment_tool(no_route, "西藏")
    assert env_default["route_value"] is None
    assert env_default["warnings"], "缺路由因子应有警示说明"
    print("  √ test_engine_route_variants")


def test_per_hazard_assessment_normal():
    """五灾种正常评估：scored、R=100·P·T、因子 schema 完整、Mock 声明齐全。"""
    for key in ALL_HAZARDS:
        profile = HAZARD_RISK_PROFILES[key]
        env = profile["assessment_tool"](_events(key), "西藏")
        assert env["assessment_status"] == "scored", f"{key} 正常数据不应 insufficient"
        assert 0 <= env["P"] <= 1 and 0 <= env["T"] <= 1
        assert env["R"] == round(100.0 * env["P"] * env["T"], 2)
        assert env["provisional_level"] in ("low", "medium", "high")
        for spec in env["factor_scores"].values():
            assert "weight_source" in spec and spec["weight_source"]
            assert spec["data_status"] in ("mock", "literature", "real", "pending")
        assert env["rule_version"].startswith("MOCK-")
        assert "MOCK" in env["disclaimer"]
    print("  √ test_per_hazard_assessment_normal（5 灾种）")


def test_cross_hazard_isolation():
    """灾种隔离：冰湖数据喂滑坡因子配置 → 缺必需因子 → insufficient，不产错误等级。"""
    gl_events = _events("glacial_lake_outburst")
    env = landslide_factor_assessment_tool(gl_events, "西藏")
    assert env["assessment_status"] == "insufficient_data"
    assert "rainfall_mm_h" in env["missing_factors"]
    ls_events = _events("landslide")
    env2 = glacial_lake_factor_assessment_tool(ls_events, "西藏")
    assert env2["assessment_status"] == "insufficient_data"
    print("  √ test_cross_hazard_isolation")


def test_evidence_tool_generic():
    """证据工具：观测/推导/局限三分类 + 公式声明 + 缺失因子披露。"""
    env = glacial_lake_factor_assessment_tool(_events("glacial_lake_outburst"), "西藏")
    limits = HAZARD_RISK_PROFILES["glacial_lake_outburst"]["current_limitations"]
    ev = risk_evidence_tool(env, hazard_limitations=limits)
    types = [e["type"] for e in ev]
    assert "observation" in types and "limitation" in types
    assert any("候选组合公式" in e["statement"] and "R=" in e["statement"]
               for e in ev if e["type"] == "observation")
    assert any("mock" in e.get("caveat", "").lower() for e in ev if e["type"] == "derived")
    print("  √ test_evidence_tool_generic")


# ============================================================
# 第二层：端到端图测试（LLM 解释失败走模板兜底，两种情况断言都通过）
# ============================================================

def _mock_data_result(hazard, complete=True):
    """上游 Data 输出的最小 Mock（平铺结构，观测字段取注册表 sample_values）。"""
    profile = HAZARD_RISK_PROFILES[hazard]
    events = []
    for i in range(1, 3):
        ev = {"event_id": f"MOCK-{i:03d}", "time": "2024-07-08T03:00:00Z",
              "latitude": 29.6 + i * 0.01, "longitude": 95.3 + i * 0.01, "region": "西藏"}
        for f, v in profile.get("sample_values", {}).items():
            ev[f] = v if complete else None
        events.append(ev)
    return {
        "processed_data": {"events": events, "data_source": "mock",
                            "hazard_type": hazard, "is_mock": True},
        "data_quality": {"is_valid": True},
        "missing_data": [], "status": "success", "error_message": "",
        "hazard_type": hazard,
    }


def _run_graph(task, hazard_type, data_result):
    from aegis.agents_ext.risk_agent.graph import create_risk_agent
    state = {
        "task": task, "hazard_type": hazard_type, "data_result": data_result,
        "risk_factors": [], "calculation_result": {}, "risk_result": {},
        "evidence": [], "uncertainties": [], "validation_result": {},
        "retry_count": 0, "max_retries": 3, "next": "", "error_message": "", "status": "",
    }
    return create_risk_agent().invoke(state)


def test_graph_normal_per_hazard():
    """五灾种正常输入：status 合法、hazard_type 正确、R 与公式声明存在、Mock 标记齐全。"""
    for key in ALL_HAZARDS:
        final = _run_graph(f"评估{key}风险", key, _mock_data_result(key))
        out = final["risk_result"]      # ★ 图完成后 risk_result = output 节点 final_output
        assert final["status"] in ("success", "uncertain", "insufficient_data", "failed")
        assert out["hazard_type"] == key
        assert out["provisional_level_status"] == "mock_thresholds"
        assert out["model_info"]["is_mock"] is True
        assert out["assessment"]["formula"]["status"] == "prototype_candidate"
        assert out["assessment"]["R"] is not None, f"{key} 正常数据应有 R 值"
        assert "MOCK" in out["metadata"]["note"]
        assert out["unimplemented_capabilities"]
    print("  √ test_graph_normal_per_hazard（5 灾种）")


def test_graph_missing_hazard():
    """缺少灾种 → insufficient_data + 原因含支持清单。"""
    final = _run_graph("评估风险", "", _mock_data_result("debris_flow"))
    out = final["risk_result"]
    assert final["status"] == "insufficient_data"
    assert any("hazard_type" in m for m in out["missing_info"])
    assert not final["evidence"]
    print("  √ test_graph_missing_hazard")


def test_graph_unknown_hazard():
    """未知灾种 → insufficient_data + 原因含支持列表。"""
    final = _run_graph("评估火山泥流", "火山泥流", _mock_data_result("debris_flow"))
    assert final["status"] == "insufficient_data"
    assert "landslide" in final["risk_result"]["reasoning"]
    print("  √ test_graph_unknown_hazard")


def test_graph_missing_required_fields():
    """缺少必需观测字段 → insufficient_data，risk_level=unknown（不强制给等级）。"""
    final = _run_graph("评估滑坡风险", "landslide", _mock_data_result("landslide", complete=False))
    out = final["risk_result"]
    assert final["status"] == "insufficient_data"
    assert out["risk_level"] == "unknown"
    assert out["assessment"]["assessment_status"] == "insufficient_data"
    print("  √ test_graph_missing_required_fields")


def test_graph_alias_cn():
    """中文别名："泥石流" 与 "debris_flow" 输出一致的归一化灾种。"""
    a = _run_graph("t", "泥石流", _mock_data_result("debris_flow"))
    b = _run_graph("t", "debris_flow", _mock_data_result("debris_flow"))
    assert a["risk_result"]["hazard_type"] == "debris_flow" == b["risk_result"]["hazard_type"]
    print("  √ test_graph_alias_cn")


def test_graph_protected_rule_hit():
    """图级保护规则：冰湖 Mock 数据冰崩入湖=yes → 触发条件含 GL-ICE-AVALANCHE-SURGE。"""
    final = _run_graph("评估冰湖风险", "glacial_lake_outburst",
                        _mock_data_result("glacial_lake_outburst"))
    out = final["risk_result"]
    hit_ids = [h["rule_id"] for h in out["rule_hits"]]
    assert "GL-ICE-AVALANCHE-SURGE" in hit_ids
    assert any("GL-ICE-AVALANCHE-SURGE" in t for t in out["trigger_conditions"])
    print("  √ test_graph_protected_rule_hit")


# ============================================================
# 运行入口
# ============================================================

if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    print("=" * 60)
    print("Risk Agent 测试套件（西藏五类山地灾害，因子化口径，不含地震）")
    print("范围：①流程运行 ②Mock 公式预设行为；不含真实预警准确率验证")
    print("=" * 60)

    print("\n[第一层] 注册表 / 权重 schema / 因子引擎（不依赖 LLM）")
    test_registry_five_hazards()
    test_registry_normalization()
    test_weight_schema()
    test_normalize_methods()
    test_engine_formula_declaration()
    test_engine_gating_required_missing()
    test_engine_coverage_renormalization()
    test_engine_protected_rules()
    test_engine_route_variants()
    test_per_hazard_assessment_normal()
    test_cross_hazard_isolation()
    test_evidence_tool_generic()

    print("\n[第二层] 端到端图测试（LLM 解释失败走模板兜底）")
    test_graph_normal_per_hazard()
    test_graph_missing_hazard()
    test_graph_unknown_hazard()
    test_graph_missing_required_fields()
    test_graph_alias_cn()
    test_graph_protected_rule_hit()

    print("\n" + "=" * 60)
    print("全部测试通过 ✅")
    print("=" * 60)
