"""
Simulation Agent 测试套件

【定位】回归测试。以后改 Simulation 代码后，跑这个确认没改坏。
        与 main.py 的区别：main.py 是"看输出"，本文件是"自动断言对错"。

【两层设计】
  第一层 test_*_tool / test_model_* / test_router_* ：纯工具与模型层测试，
          不依赖 LLM，没配 API key 也能跑。
  第二层 test_graph_* / test_pipeline_* ：端到端图测试。
          不依赖 LLM 的用例（通过 preset_scenario_configs 直传情景 + test_hooks
          注入故障）总能跑；依赖 LLM 的用例没配 OPENAI_API_KEY 会自动跳过。

【测试范围声明（重要）】
  本套件只覆盖：
    ① 仿真流程测试（图拓扑、条件分支、重试、超时、异常兜底）；
    ② 模型校验测试（参数校验、确定性、情景间指标变化、Mock 标识齐全）。
  【不】包含"真实灾害预警准确率测试"——Mock 玩具模型没有预测力，
  准确率无意义；接入经实际数据验证的专业模型后需另行设计该类测试。

【运行】
  cd C:\\Users\\lixin\\Desktop\\地政预警项目
  .venv\\Scripts\\python.exe -m agents.simulation_agent.test_simulation_agent
"""

import os
import sys

from aegis.agents_ext.simulation_agent.tools.hazard_model_router import (
    UnsupportedHazardError,
    get_model,
    get_model_requirements,
    is_supported_hazard,
    list_supported_hazards,
    normalize_hazard_type,
)
from aegis.agents_ext.simulation_agent.tools.scenario_validate_tool import validate_scenarios_tool
from aegis.agents_ext.simulation_agent.tools.default_scenario_tool import build_default_scenarios_tool
from aegis.agents_ext.simulation_agent.tools.scenario_comparison_tool import compare_scenarios_tool
from aegis.agents_ext.simulation_agent.tools.completeness_check_tool import check_results_completeness_tool
from aegis.agents_ext.simulation_agent.tools.simulation_executor import execute_scenario_with_timeout

#所有的灾种类型
ALL_HAZARDS = ["landslide", "rockfall", "debris_flow",
               "ice_rock_snow_avalanche", "glacial_lake_outburst"]

# 每个灾种一个"合法基准情景"（参数取约束区间中段，确定性），相当于一个基本的灾种模板
BASE_SCENARIOS = {
    "landslide": {"rainfall_mm_h": 80.0, "slope_angle_deg": 45.0},
    "rockfall": {"block_volume_m3": 8.0, "slope_height_m": 120.0},
    "debris_flow": {"rainfall_mm_h": 60.0, "catchment_area_km2": 10.0, "channel_gradient": 0.35},
    "ice_rock_snow_avalanche": {"release_volume_m3": 50000.0, "vertical_drop_m": 800.0},
    "glacial_lake_outburst": {"lake_volume_m3": 5000000.0, "dam_height_m": 60.0},
}

#**构造函数**（动态工厂，拿模板打底，再拼装完整情景对象，支持自定义覆盖参数）
def _make_scenario(hazard, sid="SC-01", severity="medium", **overrides):
    """构造合法基准情景（扁平 dict）。"""
    sc = {
        "scenario_id": sid, "hazard_type": hazard, "severity_level": severity,
        "trigger": "测试触发", "trigger_source_tag": "assumption",
        "duration_hours": 6.0, "notes": "测试情景", "test_hooks": {},
    }
    sc.update(BASE_SCENARIOS[hazard])
    sc.update(overrides)
    return sc


#封装单次仿真完整流程：**获取模型 → 参数校验 → 超时执行 → 收集指标结果**
def _run_model_once(hazard, scenario, attempt=1):
    """validate → run → collect 的完整单次调用（测试助手）。"""
    model = get_model(hazard)
    vr = model.validate_input(scenario)
    assert vr["is_valid"], f"基准情景应合法: {vr}"
    run = execute_scenario_with_timeout(model, vr["normalized"],
                                        {"attempt": attempt, "risk_result": {}, "data_summary": ""},
                                        timeout_seconds=30.0)
    return model.collect_results(run, vr["normalized"])


# ============================================================
# 第一层：路由 / 模型 / 工具测试（不依赖 LLM，必跑）
# ============================================================

"""
### 路由注册测试

校验灾种注册表：

- 支持的灾种数量必须等于 ALL_HAZARDS 共 5 种
- 每个灾种必须有中文名称
- Mock 模型版本号必须带`MOCK-`前缀
- 必须标记`is_real_prediction=False`，声明这不是真实预测模型
"""
def test_router_registry():
    """路由：支持清单恰含 5 个规范键，中文名一一对应。"""
    keys = [h["hazard_type"] for h in list_supported_hazards()]
    assert sorted(keys) == sorted(ALL_HAZARDS)
    for h in list_supported_hazards():
        assert h["hazard_type_cn"], "必须有中文名"
        assert h["model_version"].startswith("MOCK-"), "Mock 版本标识必须有 MOCK- 前缀"
        assert h["is_real_prediction"] is False, "★ 必须声明非真实预测"
    print("  √ test_router_registry")


"""
### 灾种名称归一化测试

测试别名翻译、大小写、中文映射：

- `"泥石流"` → `debris_flow`；`"滑坡"` → `landslide`；`GLOF` → 冰湖溃决
- 不支持灾种（火山泥流）返回空字符串

> 
> 作用：保证 LLM 输入中文灾种名，也能正确匹配到对应模型
"""
def test_router_normalize_alias():
    """路由：中文别名 / 大小写 / 连字符归一化；未知灾种返回空串。"""
    assert normalize_hazard_type("泥石流") == "debris_flow"
    assert normalize_hazard_type(" 滑坡 ") == "landslide"
    assert normalize_hazard_type("Debris-Flow") == "debris_flow"
    assert normalize_hazard_type("GLOF") == "glacial_lake_outburst"
    assert normalize_hazard_type("火山泥流") == ""
    assert normalize_hazard_type("") == ""
    assert is_supported_hazard("泥石流") is True
    assert is_supported_hazard("火山泥流") is False
    print("  √ test_router_normalize_alias")


"""
### 未知灾种异常测试
传入不支持灾种`火山泥流`，**必须抛出 `UnsupportedHazardError`**，不能静默失败或者自动切换别的模型。错误信息内要列出支持灾种清单。
"""
def test_router_unknown_raises():
    """路由：未知灾种 get_model 抛 UnsupportedHazardError，绝不擅自换模型。"""
    try:
        get_model("火山泥流")
        raise AssertionError("应当抛出 UnsupportedHazardError")
    except UnsupportedHazardError as e:
        assert "landslide" in str(e), "错误信息应包含支持列表"
    print("  √ test_router_unknown_raises")


"""
###  模型输出规范校验

遍历全部 5 个灾种，校验每个模型的元信息结构：

- 必须有`required_inputs`（必填输入参数）
- 必须有`required_metrics`（必填输出指标，包含`mock_hazard_index`、`mock_impact_index`两个 Mock 指标）
- 版本号 MOCK 前缀、免责声明、参数菜单必须齐全。
"""

def test_requirements_shape():
    """参数需求声明：5 个灾种的 required_inputs / required_metrics / 菜单齐全。"""
    for hz in ALL_HAZARDS:
        req = get_model_requirements(hz)
        assert req["required_inputs"], "必须有必需参数"
        assert "mock_hazard_index" in req["required_metrics"]
        assert "mock_impact_index" in req["required_metrics"]
        assert req["model_version"].startswith("MOCK-")
        assert req["disclaimer"], "必须有免责声明"
        assert req["param_menu"], "必须有参数菜单（LLM 取值唯一来源）"
    print("  √ test_requirements_shape")




"""
### Mock 模型确定性测试

> 
> Mock 模型的重要特性：**相同输入，每次运行输出指标完全一模一样，无随机数**

- 同一情景连续跑两次，`indicators`指标字典必须完全相等
- 校验 Mock 标识、免责声明、中间步骤记录`steps`
"""
def test_models_deterministic():
    """模型：同一情景两次运行 → 指标逐位相等（纯确定性，无随机）；Mock 标识齐全。"""
    for hz in ALL_HAZARDS:
        r1 = _run_model_once(hz, _make_scenario(hz))
        r2 = _run_model_once(hz, _make_scenario(hz))
        assert r1["indicators"] == r2["indicators"], f"{hz} 指标应可复现"
        assert r1["model_version"].startswith("MOCK-")
        assert r1["is_real_prediction"] is False
        assert "MOCK" in r1["disclaimer"]
        assert len(r1["steps"]) == get_model_requirements(hz)["n_steps"], "必须记录逐步中间状态"
    print("  √ test_models_deterministic")


"""
### 危险指标单调性测试

> 
> 业务逻辑约束：灾害严重等级越高，灾害指数`mock_hazard_index`**不能下降**

- low 低等级情景 和 extreme 极端情景对比
- extreme 的 mock_hazard_index >= low
"""
def test_models_severity_monotonic():
    """模型：severity 升高 → 指标按声明规则不降（可解释的单调变化）。"""
    for hz in ALL_HAZARDS:
        low = _run_model_once(hz, _make_scenario(hz, severity="low"))
        high = _run_model_once(hz, _make_scenario(hz, severity="extreme"))
        assert high["indicators"]["mock_hazard_index"] >= low["indicators"]["mock_hazard_index"], \
            f"{hz} 的 hazard 指标应随 severity 单调"
    print("  √ test_models_severity_monotonic")


"""
###  参数校验测试

测试校验器：

1. 缺少必填参数（滑坡只传 id 和灾种，缺降雨、坡度）→ 校验不通过，识别缺失字段
2. 参数越界：坡度 120°（合理范围 5~80°）→ 识别非法字段，校验失败

> 
> 保证非法参数不会进入仿真计算。
"""
def test_models_validate_missing_and_invalid():
    """模型：参数缺失 / 越界 → 明确拒绝，不编造输出。"""
    vr = get_model("landslide").validate_input({"scenario_id": "SC-X", "hazard_type": "landslide"})
    assert vr["is_valid"] is False
    assert "rainfall_mm_h" in vr["missing_fields"]

    bad = _make_scenario("landslide", slope_angle_deg=120.0)  # 越界（5~80）
    vr2 = get_model("landslide").validate_input(bad)
    assert vr2["is_valid"] is False
    assert any(b["field"] == "slope_angle_deg" for b in vr2["invalid_fields"])
    print("  √ test_models_validate_missing_and_invalid")


"""
### 批量情景校验工具测试

混合 3 条情景：合法、参数越界、重复 ID
断言：合法情景数量 = 1，非法明细包含 SC-02、SC-01 重复 ID。
"""
def test_validate_scenarios_tool():
    """校验工具：合法+非法混合 → 合法子集与不通过明细正确。"""
    mixed = [
        _make_scenario("debris_flow", sid="SC-01"),
        _make_scenario("debris_flow", sid="SC-02", rainfall_mm_h=999.0),  # 越界
        _make_scenario("debris_flow", sid="SC-01"),                        # id 重复
    ]
    r = validate_scenarios_tool(mixed, "debris_flow")
    assert r["valid_count"] == 1
    assert r["all_invalid"] is False
    ids = {d["scenario_id"] for d in r["invalid_details"]}
    assert "SC-02" in ids and "SC-01" in ids
    print("  √ test_validate_scenarios_tool")


"""
### 兜底情景生成工具

每个灾种自动生成至少 2 套不同严重等级的情景，并且全部参数合法。用于兜底，当 LLM 无法设计情景时直接使用。
"""
def test_default_scenarios_tool():
    """兜底工具：5 个灾种各生成 3 个默认情景，且全部通过各自校验。"""
    for hz in ALL_HAZARDS:
        scenarios = build_default_scenarios_tool(hz)
        assert len(scenarios) >= 2
        r = validate_scenarios_tool(scenarios, hz)
        assert r["valid_count"] == len(scenarios), f"{hz} 兜底情景应全部合法: {r['invalid_details']}"
        severities = {s["severity_level"] for s in scenarios}
        assert len(severities) == len(scenarios), "兜底情景应覆盖不同 severity 以便对比"
    print("  √ test_default_scenarios_tool")


"""
### 情景对比工具测试

构造 low、high 两组泥石流情景，对比工具需要：

- 以第一个情景为基线
- high 危险度更高，排序排在前面
- 指标差值`delta`为正数（高等级相比低等级风险上升）
"""
def test_comparison_tool():
    """对比工具：排序 / 极值 / 与基线的逐指标差异（scenario_difference）正确。"""
    r_low = _run_model_once("debris_flow", _make_scenario("debris_flow", "SC-01", "low"))
    r_high = _run_model_once("debris_flow", _make_scenario("debris_flow", "SC-02", "high"))
    comp = compare_scenarios_tool([r_low, r_high])
    assert comp["baseline_scenario_id"] == "SC-01"
    assert comp["ranking"][0]["scenario_id"] == "SC-02", "high 情景危险度应更高"
    top = comp["ranking"][0]["mock_hazard_index"]
    assert top >= comp["ranking"][1]["mock_hazard_index"]
    diffs = [d for d in comp["scenario_differences"] if d["metric"] == "mock_hazard_index"]
    assert diffs and diffs[0]["delta"] > 0, "high 相对 low 的 hazard 差值应为正"
    print("  √ test_comparison_tool")


"""
### 结果完整性校验（和你前面`required_metrics`强关联）

三种 case：

1. 全部指标齐全 + 分析文本齐全 → `is_complete=True`
2. 模型指标缺失（删掉 mock_impact_index）→ `is_complete=False`，**fixable=False**（模型丢的指标，单纯重跑分析补不回来，必须重跑仿真）
3. 模型指标齐全，只是分析文本缺失 → `is_complete=False`，**fixable=True**（重新跑一次分析节点就可以补全）
"""
def test_completeness_tool():
    """完整性工具：指标层缺失不可修复；分析层缺失可修复；完整则 is_complete。"""
    r = _run_model_once("debris_flow", _make_scenario("debris_flow", "SC-01", "low"))
    req = get_model_requirements("debris_flow")["required_metrics"]

    full = compare_scenarios_tool([r]) | {
        "key_findings": [{"text": "x", "source_tag": "simulation"}],
        "narrative_summary": "x",
    }
    ok = check_results_completeness_tool([r], full, req)
    assert ok["is_complete"] is True and ok["fixable"] is False

    # drop_metric 模拟指标缺失 → 不可修复（重跑分析补不回模型指标）
    dropped = dict(r)
    dropped["indicators"] = {k: v for k, v in r["indicators"].items() if k != "mock_impact_index"}
    c1 = check_results_completeness_tool([dropped], full, req)
    assert c1["is_complete"] is False and c1["fixable"] is False

    # 仅分析字段缺失 → 可修复
    c2 = check_results_completeness_tool([r], compare_scenarios_tool([r]), req)
    assert c2["is_complete"] is False and c2["fixable"] is True
    print("  √ test_completeness_tool")


"""
### 执行器故障注入 & 超时测试

`test_hooks={"fail_first_attempts":3}`：前 3 次仿真强制抛出瞬态故障异常

- 正常情景执行返回 ok=true
- 带故障钩子情景执行，触发异常

> 
> 用来测试仿真执行节点的重试分支。
"""
def test_executor_hooks_and_timeout():
    """执行器：test_hooks 瞬态故障抛异常；正常执行透传结果。"""
    model = get_model("landslide")
    ok_sc = _make_scenario("landslide", "SC-01", "low")
    run = execute_scenario_with_timeout(model, ok_sc, {"attempt": 1, "risk_result": {}, "data_summary": ""}, 10.0)
    assert run["ok"] is True

    fail_sc = _make_scenario("landslide", "SC-02", "low", test_hooks={"fail_first_attempts": 3})
    try:
        execute_scenario_with_timeout(model, fail_sc, {"attempt": 1, "risk_result": {}, "data_summary": ""}, 10.0)
        raise AssertionError("attempt=1 <= fail_first_attempts=3，应抛异常")
    except Exception as e:
        assert "瞬态故障" in str(e)
    print("  √ test_executor_hooks_and_timeout")


# ============================================================
# 第二层：端到端图测试（preset 直传 + test_hooks 可无 LLM 跑）
# ============================================================

def _has_llm_key():
    return bool(os.environ.get("OPENAI_API_KEY"))

#构造完整初始状态字典
def _initial_state(task, hazard, data_result, risk_result, presets=None, max_retries=3):
    """完整初始 State（全部键，对齐 SimulationAgentState）。"""
    return {
        "simulation_task": task,
        "data_result": data_result, "risk_result": risk_result,
        "hazard_type": hazard, "preset_scenario_configs": presets or [],
        "scenario_design_result": {}, "scenario_configs": [],
        "assumptions": [], "uncertainties": [],
        "validated_scenarios": [], "validation_result": {}, "routing_result": {},
        "simulation_results": [], "execution_result": {}, "comparison_result": {},
        "retry_count": 0, "max_retries": max_retries, "timeout_seconds": 60.0,
        "next": "", "error_message": "", "status": "", "simulation_result": {},
    }


#上游 Data Agent 的 Mock 输出结果
def _mock_data_result(status="success"):
    """上游 Data 输出的最小 Mock（平铺结构，泥石流灾种口径观测数据）。"""
    return {
        "task": "mock", "data_requirement": {}, "processing_plan": "", "raw_data": {},
        "processed_data": {"events": [{"event_id": "DF-001", "rainfall_mm_h": 65.0,
                                        "catchment_area_km2": 12.0, "channel_gradient": 0.35}],
                            "data_source": "mock", "hazard_type": "debris_flow", "is_mock": True},
        "data_quality": {"is_valid": True}, "missing_data": [],
        "retry_count": 0, "next": "end", "error_message": "", "status": status,
        "hazard_type": "debris_flow",
    }

#上游 Risk Agent 的 Mock 输出结果
def _mock_risk_result(status="success", risk_level="high"):
    """上游 Risk 输出的最小 Mock（risk_output_node 的 final_output 结构）。"""
    return {
        "status": status, "risk_level": risk_level, "risk_factors": [], "reasoning": "mock",
        "calculation_result": {}, "evidence": [], "uncertainties": [],
        "validation": {"is_valid": True, "issues": [], "recommendation": "proceed"},
        "metadata": {"retry_count": 0, "error_message": "", "data_source": "mock", "note": "mock"},
    }

#导入并创建 Simulation Agent 图，调用 invoke 运行整个图
def _run_graph(state):
    from aegis.agents_ext.simulation_agent.graph import create_simulation_agent
    return create_simulation_agent().invoke(state)


"""
### 正常全流程（主链路）

预置 2 个泥石流情景（low/high），直接送入图，不走 LLM 情景设计。
断言：

- 状态成功 /uncertain
- Mock 标识齐全
- 返回 2 套仿真结果，有情景排序、免责声明。
"""
def test_graph_happy_path():
    """情况 1 正常流程（preset 直传，无 LLM）→ 结果完整 + Mock 标识齐全。"""
    presets = [
        _make_scenario("debris_flow", "SC-01", "low"),
        _make_scenario("debris_flow", "SC-02", "high"),
    ]
    final = _run_graph(_initial_state("推演泥石流", "debris_flow",
                                      _mock_data_result(), _mock_risk_result(), presets))
    out = final["simulation_result"]
    assert final["status"] in ("success", "uncertain")   # 无 LLM 时分析走兜底 → uncertain
    assert out["model_info"]["is_real_prediction"] is False
    assert "MOCK" in out["model_info"]["model_version"]
    assert len(out["simulation_results"]) == 2
    assert all("MOCK" in r["disclaimer"] for r in out["simulation_results"])
    assert out["comparison"]["ranking"], "必须有情景排序"
    assert out["metadata"]["note"], "必须有全局免责声明"
    assert out["hazard_type"] == "debris_flow"
    print(f"  √ test_graph_happy_path  status={final['status']}")


"""
###  上游数据缺失分支

Data 结果为空，模拟上游 Data Agent 失败、没有数据。
预期走向：`scenario_design`判断数据缺失，直接 early exit 到`simulation_output`。
断言：status=`insufficient_data`，仿真结果为空，返回缺失字段清单。
"""
def test_graph_missing_data():
    """情况 2 上游数据缺失 → insufficient_data + 缺失字段及原因。"""
    final = _run_graph(_initial_state("推演滑坡", "landslide", {}, {}))
    out = final["simulation_result"]
    assert final["status"] == "insufficient_data"
    assert out["missing_inputs"], "必须返回缺失字段清单"
    assert all(("field" in m and "reason" in m) for m in out["missing_inputs"])
    assert out["simulation_results"] == [], "数据缺失不应执行仿真"
    print("  √ test_graph_missing_data")


"""
###  参数非法分支

预置情景：滑坡坡度 120°（越界）。情景校验节点判定失败，直接 early exit 输出。
断言：status=`failed`，不运行仿真，返回校验错误明细。
"""
def test_graph_invalid_params():
    """情况 3 参数不合法 → 拒绝执行 + 校验明细（确实没跑仿真）。"""
    presets = [_make_scenario("landslide", "SC-01", "high", slope_angle_deg=120.0)]  # 越界
    final = _run_graph(_initial_state("推演滑坡", "landslide",
                                      _mock_data_result(), _mock_risk_result(), presets))
    out = final["simulation_result"]
    assert final["status"] == "failed"
    assert out["validation"]["scenario_validation"]["invalid_details"], "必须有校验明细"
    assert out["simulation_results"] == [], "参数不合法必须拒绝执行"
    print("  √ test_graph_invalid_params")



"""
### 不支持灾种分支

灾种传入`火山泥流`，路由识别不支持灾种，提前退出。
断言：status=`failed`，仿真结果为空，错误信息列出支持灾种。
"""
def test_graph_unknown_hazard():
    """情况 4 未知灾种 → failed + 支持列表，不调用任何模型。"""
    final = _run_graph(_initial_state("推演火山泥流", "火山泥流",
                                      _mock_data_result(), _mock_risk_result()))
    out = final["simulation_result"]
    assert final["status"] == "failed"
    assert "landslide" in out["unsupported_reason"], "原因应包含支持列表"
    assert out["simulation_results"] == []
    print("  √ test_graph_unknown_hazard")


"""
### 中文灾种别名测试

分别传入中文 "泥石流" 和英文`debris_flow`，最终模型输出指标完全一致。验证归一化函数在图链路中正常工作。
"""
def test_graph_alias_cn():
    """中文别名与标准键路由到同一模型，同参数结果一致。"""
    presets = [_make_scenario("debris_flow", "SC-01", "medium")]
    a = _run_graph(_initial_state("t", "泥石流", _mock_data_result(), _mock_risk_result(), presets))
    b = _run_graph(_initial_state("t", "debris_flow", _mock_data_result(), _mock_risk_result(), presets))
    assert a["simulation_result"]["hazard_type"] == "debris_flow"
    assert a["simulation_result"]["hazard_type"] == b["simulation_result"]["hazard_type"]
    ia = a["simulation_result"]["simulation_results"][0]["indicators"]
    ib = b["simulation_result"]["simulation_results"][0]["indicators"]
    assert ia == ib, "同参数同模型，指标必须一致"
    print("  √ test_graph_alias_cn")


"""
### 瞬态故障重试成功

`test_hooks={"fail_first_attempts":1}`：第一次仿真抛出异常，第二次正常。max_retries=3。
预期：重试 1 次，`retry_count=1`，最终成功输出结果。对应你代码里`simulation_execute`自环重试分支。
"""
def test_graph_retry_transient():
    """情况 5a 瞬态故障（test_hooks）→ 重试 1 次后成功，retry_count 正确自增。"""
    presets = [_make_scenario("landslide", "SC-01", "low",
                              test_hooks={"fail_first_attempts": 1})]
    final = _run_graph(_initial_state("t", "landslide",
                                      _mock_data_result(), _mock_risk_result(), presets))
    out = final["simulation_result"]
    assert final["status"] in ("success", "uncertain")
    assert final["retry_count"] == 1, "瞬态故障应恰好重试一次"
    assert len(out["simulation_results"]) == 1, "重试后应产出结果"
    assert out["metadata"]["retry_count"] == 1
    print(f"  √ test_graph_retry_transient  retry_count={final['retry_count']}")


"""
### 重试预算耗尽终止

`fail_first_attempts=99`，持续报错，max_retries=3。
预期：重试 3 次后停止，`retry_count=3`，status=failed，无仿真结果。**验证防止死循环的核心机制**。
"""
def test_graph_retry_exhausted():
    """情况 5b 持续故障 → 重试预算耗尽后终止，status=failed。"""
    presets = [_make_scenario("landslide", "SC-01", "low",
                              test_hooks={"fail_first_attempts": 99})]
    final = _run_graph(_initial_state("t", "landslide",
                                      _mock_data_result(), _mock_risk_result(), presets,
                                      max_retries=3))
    out = final["simulation_result"]
    assert final["status"] == "failed"
    assert final["retry_count"] == 3, "预算耗尽时 retry_count 应等于 max_retries"
    assert out["simulation_results"] == []
    assert out["metadata"]["error_message"], "必须保留失败原因"
    print("  √ test_graph_retry_exhausted")


"""
### 仿真指标缺失，不可修复

钩子`drop_metric`删掉必填指标`mock_impact_index`。
预期：`is_complete=False`，`fixable=False`；状态`uncertain`，不确定性描述写明缺失指标；不会重试`results_analysis`
"""
def test_graph_incomplete_output():
    """情况 6 输出不完整（drop_metric 剔除必需指标）→ 不可修复 → uncertain + 明确缺失项。"""
    presets = [_make_scenario("debris_flow", "SC-01", "medium",
                              test_hooks={"drop_metric": "mock_impact_index"})]
    final = _run_graph(_initial_state("t", "debris_flow",
                                      _mock_data_result(), _mock_risk_result(), presets))
    out = final["simulation_result"]
    assert final["status"] == "uncertain"
    comp = out["comparison_result"] if "comparison_result" in out else out["comparison"]
    assert comp["completeness"]["is_complete"] is False
    assert comp["completeness"]["fixable"] is False, "模型剔除的指标重跑分析补不回"
    assert any("mock_impact_index" in m for m in comp["completeness"]["missing_items"])
    assert any("mock_impact_index" in u for u in out["uncertainties"]), "不确定性必须写明缺失项"
    print("  √ test_graph_incomplete_output")


"""
### LLM 端到端情景设计（可选）

没有 OPENAI_API_KEY 自动跳过。
作用：测试不传入 preset 情景，交给 LLM 自主完成 scenario_design 情景设计，再走完整仿真链路。
"""
def test_pipeline_normal_llm():
    """[依赖 LLM] 端到端：LLM 设计情景 + LLM 分析 → 关键发现带来源标签。"""
    if not _has_llm_key():
        print("  ⚠ 跳过 test_pipeline_normal_llm（未设 OPENAI_API_KEY）")
        return
    final = _run_graph(_initial_state("推演强降雨下的泥石流风险", "debris_flow",
                                      _mock_data_result(), _mock_risk_result()))
    out = final["simulation_result"]
    assert final["status"] in ("success", "uncertain", "insufficient_data", "failed")
    if final["status"] in ("success", "uncertain"):
        findings = out.get("key_findings", [])
        assert all("source_tag" in f for f in findings), "每条发现必须带来源标签"
        assert out["assumptions"], "必须有情景假设清单"
    print(f"  √ test_pipeline_normal_llm  status={final['status']}")


# ============================================================
# 运行入口
# ============================================================

if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    print("=" * 60)
    print("Simulation Agent 测试套件（范围：仿真流程测试 + 模型校验测试；")
    print("真实灾害预警准确率测试不在本期范围，Mock 模型无预测力）")
    print("=" * 60)

    print("\n[第一层] 路由 / 模型 / 工具测试（不依赖 LLM）")
    test_router_registry()
    test_router_normalize_alias()
    test_router_unknown_raises()
    test_requirements_shape()
    test_models_deterministic()
    test_models_severity_monotonic()
    test_models_validate_missing_and_invalid()
    test_validate_scenarios_tool()
    test_default_scenarios_tool()
    test_comparison_tool()
    test_completeness_tool()
    test_executor_hooks_and_timeout()

    print("\n[第二层] 端到端图测试（preset 直传 + test_hooks，无 LLM 也能跑）")
    test_graph_happy_path()
    test_graph_missing_data()
    test_graph_invalid_params()
    test_graph_unknown_hazard()
    test_graph_alias_cn()
    test_graph_retry_transient()
    test_graph_retry_exhausted()
    test_graph_incomplete_output()

    print("\n[第二层·附] 依赖 LLM 的端到端（无 key 自动跳过）")
    test_pipeline_normal_llm()

    print("\n" + "=" * 60)
    print("全部测试通过 ✅")
    print("=" * 60)
