"""
Data Agent 测试套件（面向西藏五类山地地质灾害）

【定位】回归测试。以后改 Data Agent 代码后，跑这个确认没改坏。
        与 main.py 的区别：main.py 是"看输出"，本文件是"自动断言对错"。

【两层设计】
  第一层 test_registry_* / test_clean_* / test_normalize_* / test_validate_* /
         test_retrieve_* ：注册表与工具层测试，不依赖 LLM，没配 API key 也能跑。
  第二层 test_graph_*：端到端图测试（data_plan 的 LLM 有 key 时真实调用，
         无 key / LLM 失败时走菜单默认值兜底——两种情况断言都必须通过）。

【运行】
  cd C:\\Users\\lixin\\Desktop\\地政预警项目
  .venv\\Scripts\\python.exe -m agents.data_agent.test_data_agent
"""

import sys

from aegis.agents_ext.data_agent.tools.hazard_data_registry import (
    HAZARD_DATA_REQUIREMENTS,
    build_query_param_model,
    get_data_source_tool,
    get_hazard_data_requirements,
    list_hazard_data_capabilities,
)
from aegis.agents_ext.data_agent.tools import (
    data_clean_tool,
    data_normalize_tool,
    data_validate_tool,
)
from aegis.agents_ext.hazard_registry import (
    SUPPORTED_HAZARDS,
    UnsupportedHazardError,
    normalize_hazard_type,
)

ALL_HAZARDS = ["landslide", "rockfall", "debris_flow",
               "ice_rock_snow_avalanche", "glacial_lake_outburst"]


# ============================================================
# 第一层：注册表与工具测试（不依赖 LLM，必跑）
# ============================================================

def test_registry_five_hazards():
    """注册表：恰含五个标准灾种键，不含地震。"""
    assert sorted(HAZARD_DATA_REQUIREMENTS.keys()) == sorted(ALL_HAZARDS)
    assert "earthquake" not in HAZARD_DATA_REQUIREMENTS
    for key in ALL_HAZARDS:
        req = HAZARD_DATA_REQUIREMENTS[key]
        assert req["observation_fields"], f"{key} 必须声明观测字段"
        assert req["required_observation_fields"], f"{key} 必须声明必需观测字段"
        assert req["data_source_tool"] is not None
        assert req["missing_capabilities"], f"{key} 必须诚实声明缺失能力"
    print("  √ test_registry_five_hazards")


def test_registry_normalization():
    """注册表：中文名归一化 / 未知灾种拒绝 / 必需字段合成正确。"""
    assert normalize_hazard_type("泥石流") == "debris_flow"
    assert normalize_hazard_type("山体滑坡") == "landslide"
    assert normalize_hazard_type("火山泥流") == ""
    req = get_hazard_data_requirements("泥石流")
    # 必需字段 = 公共必需(event_id/time/lat/lon) + 灾种必需观测
    assert set(req["required_fields"]) == {
        "event_id", "time", "latitude", "longitude",
        "rainfall_mm_h", "catchment_area_km2", "channel_gradient",
    }
    # 物理范围含公共坐标 + 观测字段
    assert req["valid_ranges"]["latitude"] == (-90.0, 90.0)
    assert req["valid_ranges"]["rainfall_mm_h"] == (0.0, 300.0)
    try:
        get_hazard_data_requirements("火山泥流")
        raise AssertionError("应当抛出 UnsupportedHazardError")
    except UnsupportedHazardError as e:
        assert "landslide" in str(e)
    print("  √ test_registry_normalization")


def test_registry_field_spec_per_hazard():
    """注册表：五灾种必需观测字段互不相同（按各自致灾机理声明）。"""
    req_sets = {
        k: frozenset(HAZARD_DATA_REQUIREMENTS[k]["required_observation_fields"])
        for k in ALL_HAZARDS
    }
    assert req_sets["landslide"] != req_sets["debris_flow"]
    assert req_sets["rockfall"] != req_sets["glacial_lake_outburst"]
    assert "lake_volume_m3" in req_sets["glacial_lake_outburst"]
    assert "release_volume_m3" in req_sets["ice_rock_snow_avalanche"]
    assert "block_volume_m3" in req_sets["rockfall"]
    print("  √ test_registry_field_spec_per_hazard")


def test_mock_data_source_contract():
    """数据源工具：五个 Mock 构造器返回统一外层契约 + 正确的灾种来源标记。"""
    for key in ALL_HAZARDS:
        tool = get_data_source_tool(key)
        raw = tool({"region": "西藏", "time_range": "24h"})
        assert raw["data_source"] == "mock"
        assert raw["is_mock"] is True, f"{key} 数据源必须显式标记 Mock"
        assert raw["total_count"] == len(raw["events"]) > 0
        for ev in raw["events"]:
            assert "event_id" in ev and "latitude" in ev and "region" in ev
    # 灾种 → 工具路由正确（data_source_detail 一一对应）
    assert get_data_source_tool("landslide")({"x": 1})["data_source_detail"] == "mock_landslide_constructor"
    assert get_data_source_tool("glacial_lake_outburst")({"x": 1})["data_source_detail"] == "mock_glacial_lake_constructor"
    print("  √ test_mock_data_source_contract")


def test_clean_tool_parameterized():
    """清洗工具：按传入的灾种必需字段去重与标记缺失（以泥石流为例）。"""
    req = get_hazard_data_requirements("debris_flow")
    raw = get_data_source_tool("debris_flow")({"region": "西藏"})
    events = raw["events"] + [dict(raw["events"][0])]  # 复制第一条制造重复
    raw = {"events": events, "data_source": "mock"}
    r = data_clean_tool(raw, required_fields=req["required_fields"])
    assert r["cleaning_report"]["duplicates_removed"] == 1
    assert r["cleaning_report"]["total_output"] == 3
    # 第三条缺 time（公共必需），应被标记
    flagged = [e for e in r["events"] if e.get("_has_missing")]
    assert any(e.get("time") is None for e in flagged)
    print("  √ test_clean_tool_parameterized")


def test_normalize_tool_parameterized():
    """标准化工具：时间/坐标通用 + 观测字段按注册表精度处理（以滑坡为例）。"""
    req = get_hazard_data_requirements("landslide")
    raw = get_data_source_tool("landslide")({"region": "西藏"})
    cleaned = data_clean_tool(raw, required_fields=req["required_fields"])
    r = data_normalize_tool(cleaned, normalize_precision=req["normalize_precision_full"])
    ev1 = [e for e in r["events"] if e["event_id"] == "LS2024001"][0]
    assert ev1["time"] == "2024-07-08T03:00:00+00:00"      # ISO 8601
    assert ev1["rainfall_mm_h"] == 85.0                     # 1 位小数
    assert ev1["soil_moisture_ratio"] == 0.78               # 2 位小数
    assert "time_iso8601" in r["normalization_applied"]
    print("  √ test_normalize_tool_parameterized")


def test_validate_tool_ranges():
    """校验工具：物理范围异常被捕获 + 完整度阈值判定（以滑坡为例）。"""
    req = get_hazard_data_requirements("landslide")
    events = [
        {"event_id": "E1", "time": "t", "latitude": 29.0, "longitude": 95.0,
         "rainfall_mm_h": 999.0, "slope_angle_deg": 45.0},   # 降雨超物理范围
        {"event_id": "E2", "time": "t", "latitude": 29.0, "longitude": 95.0,
         "rainfall_mm_h": 30.0, "slope_angle_deg": 40.0},
    ]
    r = data_validate_tool(
        {"events": events},
        required_fields=req["required_fields"],
        valid_ranges=req["valid_ranges"],
    )
    assert any(a["field"] == "rainfall_mm_h" for a in r["anomalies"])
    assert r["completeness_ratio"] == 1.0        # 必需字段齐全
    assert r["is_valid"] is False                # 异常率 50% > 10%
    print("  √ test_validate_tool_ranges")


def test_query_param_model_menu_constraint():
    """动态参数菜单模型：缺省可构造；越界值被 pydantic 拒绝（LLM 无法发明参数）。"""
    Model = build_query_param_model("debris_flow")
    ok = Model()  # 全默认
    assert ok.region == "西藏" and ok.time_range == "24h" and ok.min_rainfall_mm_h == 0.0
    try:
        Model(min_rainfall_mm_h=999.0)   # 超出菜单范围 (0, 300)
        raise AssertionError("越界参数应被拒绝")
    except Exception:
        pass
    print("  √ test_query_param_model_menu_constraint")


def test_validate_class_field():
    """校验工具：分类字段取值不在 allowed 集合 → 异常（以泥石流水源类型为例）。"""
    req = get_hazard_data_requirements("debris_flow")
    events = [{"event_id": "E1", "time": "t", "latitude": 29.0, "longitude": 95.0,
               "rainfall_mm_h": 30.0, "catchment_area_km2": 5.0, "channel_gradient": 0.2,
               "water_source_type": "volcano"}]   # 非法分类取值
    r = data_validate_tool(
        {"events": events},
        required_fields=req["required_fields"],
        valid_ranges=req["valid_ranges"],
        allowed_values=req["allowed_values"],
    )
    assert any(a["field"] == "water_source_type" for a in r["anomalies"])
    # 合法分类值不应报异常
    events[0]["water_source_type"] = "rainstorm"
    r2 = data_validate_tool({"events": events}, req["required_fields"],
                             req["valid_ranges"], req["allowed_values"])
    assert not any(a["field"] == "water_source_type" for a in r2["anomalies"])
    print("  √ test_validate_class_field")


def test_validate_expiry():
    """校验工具：数据过期检查（显式 as_of，避免依赖当前时钟）。"""
    from datetime import datetime, timezone
    req = get_hazard_data_requirements("landslide")
    events = [{"event_id": "E1", "time": "2024-01-01T00:00:00Z", "latitude": 29.0,
               "longitude": 95.0, "rainfall_mm_h": 10.0, "slope_angle_deg": 30.0}]
    as_of = datetime(2024, 1, 10, tzinfo=timezone.utc)   # 距记录 9 天 ≈ 216h
    r = data_validate_tool(
        {"events": events}, req["required_fields"], req["valid_ranges"],
        max_age_hours=72.0, as_of=as_of,
    )
    assert r["expired_records"] == 1
    assert any("数据过期" in a["reason"] for a in r["anomalies"])
    # 不启用过期检查（默认）→ 不产生过期异常
    r2 = data_validate_tool({"events": events}, req["required_fields"], req["valid_ranges"])
    assert r2["expired_records"] == 0
    print("  √ test_validate_expiry")


def test_registry_class_fields_declared():
    """注册表：分类字段声明了 allowed 集合与监测方式（docx 监测矩阵对齐）。"""
    assert get_hazard_data_requirements("rockfall")["allowed_values"]["failure_mode"] == \
        ["sliding", "toppling", "falling"]
    assert get_hazard_data_requirements("debris_flow")["allowed_values"]["water_source_type"] == \
        ["rainstorm", "snowmelt", "outburst", "spring"]
    gl = get_hazard_data_requirements("glacial_lake_outburst")["observation_fields"]
    assert gl["ice_avalanche_into_lake"]["type"] == "class"
    assert gl["ice_avalanche_into_lake"]["allowed"] == ["no", "yes"]
    # 新增观测字段的监测方式声明存在（docx 监测矩阵）
    assert get_hazard_data_requirements("landslide")["observation_fields"]["displacement_mm_day"]["monitor"]
    print("  √ test_registry_class_fields_declared")


def test_capabilities_declaration():
    """能力清单：五灾种全部显式声明 Mock 与缺失能力。"""
    caps = list_hazard_data_capabilities()
    assert len(caps) == 5
    for c in caps:
        assert c["is_mock"] is True
        assert c["missing_capabilities"]
    print("  √ test_capabilities_declaration")


# ============================================================
# 第二层：端到端图测试（data_plan 的 LLM 失败时走菜单默认值兜底，
#          两种情况下断言都必须通过）
# ============================================================

def _run_graph(task, hazard_type):
    from aegis.agents_ext.data_agent.graph import build_data_agent_graph
    initial_state = {
        "task": task,
        "hazard_type": hazard_type,
        "data_requirement": {}, "processing_plan": "",
        "raw_data": {}, "processed_data": {},
        "data_quality": {}, "missing_data": [], "data_sufficiency": {},
        "retry_count": 0, "next": "", "error_message": "", "status": "pending",
    }
    return build_data_agent_graph().invoke(initial_state)


def test_graph_normal_per_hazard():
    """五灾种正常流程：数据可获取、灾种写入输出、Mock 标记存在。"""
    for key in ALL_HAZARDS:
        final = _run_graph(f"获取{key}观测数据", key)
        assert final["status"] in ("success", "partial"), \
            f"{key} 正常流程不应 failed: {final.get('error_message')}"
        assert final["hazard_type"] == key
        pd = final["processed_data"]
        assert pd.get("hazard_type") == key
        assert pd.get("is_mock") is True
        assert pd.get("events"), f"{key} 应有观测记录"
        assert "data_sufficiency" in final
        req = get_hazard_data_requirements(key)
        assert final["data_requirement"]["required_fields"] == req["required_fields"]
    print("  √ test_graph_normal_per_hazard（5 灾种）")


def test_graph_alias_cn():
    """中文别名："泥石流" 与 "debris_flow" 归一化结果一致。"""
    a = _run_graph("获取泥石流观测数据", "泥石流")
    b = _run_graph("获取泥石流观测数据", "debris_flow")
    assert a["hazard_type"] == "debris_flow" == b["hazard_type"]
    print("  √ test_graph_alias_cn")


def test_graph_missing_hazard():
    """缺少灾种 → failed + 明确原因（支持清单）。"""
    final = _run_graph("获取观测数据", "")
    assert final["status"] == "failed"
    assert "landslide" in final["error_message"], "原因应包含支持清单"
    assert final["data_requirement"] == {}
    print("  √ test_graph_missing_hazard")


def test_graph_unknown_hazard():
    """未知灾种 → failed + 明确原因，不调用任何数据源工具。"""
    final = _run_graph("获取观测数据", "火山泥流")
    assert final["status"] == "failed"
    assert "火山泥流" in final["error_message"]
    assert not final["processed_data"].get("events"), "未知灾种不应获取任何数据"
    print("  √ test_graph_unknown_hazard")


# ============================================================
# 运行入口
# ============================================================

if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    print("=" * 60)
    print("Data Agent 测试套件（西藏五类山地地质灾害口径，不含地震）")
    print("=" * 60)

    print("\n[第一层] 注册表与工具测试（不依赖 LLM）")
    test_registry_five_hazards()
    test_registry_normalization()
    test_registry_field_spec_per_hazard()
    test_mock_data_source_contract()
    test_clean_tool_parameterized()
    test_normalize_tool_parameterized()
    test_validate_tool_ranges()
    test_validate_class_field()
    test_validate_expiry()
    test_registry_class_fields_declared()
    test_query_param_model_menu_constraint()
    test_capabilities_declaration()

    print("\n[第二层] 端到端图测试（LLM 失败走兜底，断言两种情况都通过）")
    test_graph_normal_per_hazard()
    test_graph_alias_cn()
    test_graph_missing_hazard()
    test_graph_unknown_hazard()

    print("\n" + "=" * 60)
    print("全部测试通过 ✅")
    print("=" * 60)
