"""
Feedback Agent 独立测试（覆盖任务书第十节 14 项测试要求）

【离线确定性策略】测试全程 monkeypatch get_feedback_llm 抛异常，
  迫使反思/建议环节走确定性兜底 —— 这本身即测试项 9（LLM 不可用时
  仍能执行确定性检查）。LLM 通路的联网验证见 agents.feedback_agent.main
  用例3（已在开发过程中实测 used_llm=True 通过）。

【运行方式】
  cd C:\\Users\\lixin\\Desktop\\地政预警项目
  .venv\\Scripts\\python.exe -m agents.feedback_agent.test_feedback_agent
"""

import copy
import sys

import aegis.agents_ext.feedback_agent.llm_setup as feedback_llm_setup


# ---------- 全局禁用 LLM（测试确定性；单独用例说明见 test_09） ----------
def _no_llm():
    raise RuntimeError("测试模式：LLM 不可用（验证确定性兜底）")


feedback_llm_setup.get_feedback_llm = _no_llm
# 节点内部为延迟导入（from aegis.agents_ext.feedback_agent.llm_setup import get_feedback_llm），
# 调用时取模块属性，因此上面的替换对节点生效。

from aegis.agents_ext.feedback_agent.main import run_feedback_agent  # noqa: E402
from aegis.agents_ext.feedback_agent.test_data_builder import (  # noqa: E402
    build_consistent_pipeline_results,
)
from aegis.agents_ext.feedback_agent.tools.feedback_memory_tool import (  # noqa: E402
    STORAGE_PATH,
    load_feedback_records,
)

ALL_HAZARDS = ["landslide", "rockfall", "debris_flow", "ice_rock_snow_avalanche", "glacial_lake_outburst"]


def _categories(fb: dict) -> set:
    return {i.get("category") for i in fb.get("feedback_items", []) or []}


def _actionable(fb: dict) -> list:
    return [i for i in fb.get("feedback_items", []) or [] if i.get("severity") != "info"]


# ============================================================
# 1. 五种标准灾种都能正确识别
# ============================================================
def test_01_five_hazards_recognized():
    for h in ALL_HAZARDS:
        blocks = build_consistent_pipeline_results(h, event_id=f"EVT-T01-{h}")
        fb = run_feedback_agent("测试1：五灾种识别", hazard_type=h, event_id=f"EVT-T01-{h}", **blocks)
        assert fb.get("hazard_type") == h, f"{h} 灾种识别错误: {fb.get('hazard_type')}"
        assert fb.get("status") in ("no_issues", "issues_found", "needs_review"), f"{h} 未正常完成: {fb.get('status')}"
    print("  ✅ 用例1：五种标准灾种均正确识别（含中文别名链路由 hazard_registry 保证）")


# ============================================================
# 2. 未知灾种明确拒绝
# ============================================================
def test_02_unknown_hazard_rejected():
    for bad in ("earthquake", "火山泥流", ""):
        fb = run_feedback_agent(
            "测试2：未知灾种",
            hazard_type=bad,
            data_result={"status": "success"},
            risk_result={"status": "success", "hazard_type": "landslide"},
        )
        assert fb.get("status") == "failed", f"'{bad}' 应被拒绝，实际 {fb.get('status')}"
        assert "workflow_error" in _categories(fb), f"'{bad}' 缺少拒绝记录"
        reject = [i for i in fb["feedback_items"] if "不在五类标准灾种内" in i["description"]]
        assert reject, f"'{bad}' 拒绝原因未写明"
    print("  ✅ 用例2：未知灾种（earthquake/火山泥流/空）全部明确拒绝并给出原因")


# ============================================================
# 3. 正常输入不产生虚假问题
# ============================================================
def test_03_normal_input_no_false_issues():
    for h in ALL_HAZARDS:
        blocks = build_consistent_pipeline_results(h, event_id=f"EVT-T03-{h}")
        fb = run_feedback_agent("测试3：正常输入", hazard_type=h, event_id=f"EVT-T03-{h}", **blocks)
        actionable = _actionable(fb)
        assert not actionable, f"{h} 正常输入产生了虚假可行动问题: {[i['description'] for i in actionable]}"
        assert fb.get("status") == "no_issues", f"{h} 应为 no_issues，实际 {fb.get('status')}"
    print("  ✅ 用例3：五灾种正常链路均无虚假可行动问题（info 级提示不计）")


# ============================================================
# 4. 缺少关键字段能够识别
# ============================================================
def test_04_missing_key_fields_detected():
    blocks = build_consistent_pipeline_results("debris_flow", event_id="EVT-T04")
    del blocks["data_result"]["data_quality"]  # 缺数据质量字段
    blocks["risk_result"]["assessment"]["R"] = None  # 风险结果缺计算依据
    blocks["risk_result"]["assessment"]["factor_scores"] = {}
    fb = run_feedback_agent("测试4：缺关键字段", hazard_type="debris_flow", event_id="EVT-T04", **blocks)
    cats = _categories(fb)
    assert "data_quality_issue" in cats, f"未识别缺失 data_quality: {cats}"
    assert "risk_calculation_issue" in cats, f"未识别 R 缺失: {cats}"
    assert fb.get("status") == "issues_found", fb.get("status")
    print("  ✅ 用例4：缺失关键字段（data_quality / assessment.R）均被识别")


# ============================================================
# 5. 数据过期能够识别
# ============================================================
def test_05_stale_data_detected():
    blocks = build_consistent_pipeline_results("landslide", event_id="EVT-T05")
    processed = blocks["data_result"]["processed_data"]
    processed["is_mock"] = False  # 模拟真实数据源接入
    processed["data_source"] = "real_station"
    for e in processed["events"]:
        e["time"] = "2020-01-01T00:00:00Z"  # 远早于时效阈值
    fb = run_feedback_agent("测试5：数据过期", hazard_type="landslide", event_id="EVT-T05", **blocks)
    stale = [i for i in fb.get("feedback_items", []) if "过期" in i["description"] or "时效" in i["description"]]
    assert stale, f"未识别数据过期: {_categories(fb)}"
    # Mock 数据默认跳过过期检查（不误报）已由用例3证明
    print("  ✅ 用例5：非 Mock 数据过期被识别；Mock 数据按约定跳过时效检查防误报")


# ============================================================
# 6. Mock 结果被错误描述为真实结果时能够识别
# ============================================================
def test_06_mock_as_real_detected():
    # 6a：alert 声称 mock_only=False
    blocks = build_consistent_pipeline_results("debris_flow", event_id="EVT-T06A")
    blocks["warning_result"]["alert"]["mock_only"] = False
    fb = run_feedback_agent("测试6a：mock冒充真实", hazard_type="debris_flow", event_id="EVT-T06A", **blocks)
    hits = [i for i in fb["feedback_items"] if i["category"] == "warning_consistency_issue" and "mock" in i["description"].lower()]
    assert hits, "未识别 mock_only=False 冒充真实预警"

    # 6b：解释文本宣称"真实观测/正式发布"
    blocks = build_consistent_pipeline_results("debris_flow", event_id="EVT-T06B")
    blocks["warning_result"]["explanation"]["text"] = "本预警基于真实观测数据生成，已正式发布，请照常执行。"
    fb = run_feedback_agent("测试6b：文本冒充真实", hazard_type="debris_flow", event_id="EVT-T06B", **blocks)
    hits = [
        i for i in fb["feedback_items"] if "真实观测" in i["description"] or ("真实" in i["description"] and "红线" in i["description"])
    ]
    assert hits, "未识别解释文本把 Mock 描述为真实/已发布"

    # 6c：负向防误报 —— 含否定词的合规声明不得误报
    blocks = build_consistent_pipeline_results("debris_flow", event_id="EVT-T06C")
    blocks["warning_result"]["explanation"]["text"] = "本预警为 Mock 模拟产物，非真实预测，禁止用于实际发布决策。"
    fb = run_feedback_agent("测试6c：合规否定句防误报", hazard_type="debris_flow", event_id="EVT-T06C", **blocks)
    hits = [i for i in fb["feedback_items"] if "把模拟" in i["description"]]
    assert not hits, "合规的 Mock 声明被误报"

    # 6d：simulation 声称真实预测（红线）
    blocks = build_consistent_pipeline_results("rockfall", event_id="EVT-T06D")
    blocks["simulation_result"]["model_info"]["is_real_prediction"] = True
    fb = run_feedback_agent("测试6d：仿真冒充真实", hazard_type="rockfall", event_id="EVT-T06D", **blocks)
    hits = [i for i in fb["feedback_items"] if "is_real_prediction" in str(i["fields"])]
    assert hits, "未识别 is_real_prediction=True 红线违规"
    print("  ✅ 用例6：Mock 被冒充真实的四种注入均识别；合规否定句不误报")


# ============================================================
# 7. Risk 与 Simulation 的灾种不一致能够识别
# ============================================================
def test_07_risk_simulation_hazard_mismatch():
    blocks = build_consistent_pipeline_results("landslide", event_id="EVT-T07")
    blocks["simulation_result"]["hazard_type"] = "rockfall"
    blocks["simulation_result"]["model_info"]["hazard_type"] = "rockfall"
    fb = run_feedback_agent("测试7：灾种不一致", hazard_type="landslide", event_id="EVT-T07", **blocks)
    hits = [i for i in fb["feedback_items"] if i["category"] == "hazard_type_mismatch" and i["agent"] == "simulation"]
    assert hits, f"未识别 Risk/Simulation 灾种不一致: {_categories(fb)}"
    assert fb.get("status") == "issues_found"
    print("  ✅ 用例7：Simulation 灾种与事件/Risk 不一致被识别为 hazard_type_mismatch")


# ============================================================
# 8. Warning 内容与上游结果不一致能够识别
# ============================================================
def test_08_warning_inconsistency_detected():
    blocks = build_consistent_pipeline_results("glacial_lake_outburst", event_id="EVT-T08")
    alert = blocks["warning_result"]["alert"]
    real_r = alert["based_on"]["R"]
    alert["based_on"]["R"] = (real_r or 50.0) + 233.0  # 依据链 R 被篡改
    alert["based_on"]["risk_level"] = "high-不存在等级"  # 依据链等级被篡改
    fb = run_feedback_agent("测试8：预警不一致", hazard_type="glacial_lake_outburst", event_id="EVT-T08", **blocks)
    hits = [i for i in fb["feedback_items"] if i["category"] == "warning_consistency_issue"]
    assert len(hits) >= 2, f"未充分识别 Warning 与上游不一致: {hits}"
    # 且必须完成反思与建议（进入过 reflection）
    assert fb["metadata"]["iterations_used"] >= 1, "高置信问题应触发反思"
    sugg = fb.get("improvement_suggestions") or []
    assert sugg and all(s.get("target_agent") in ("data", "risk", "simulation", "warning", "manager") for s in sugg)
    print(f"  ✅ 用例8：Warning 依据链不一致（R / risk_level）均识别，反思轮次={fb['metadata']['iterations_used']}，建议具体到组件")


# ============================================================
# 9. LLM 不可用时仍能执行确定性检查
# ============================================================
def test_09_works_without_llm():
    # 本测试文件全程 monkeypatch get_feedback_llm 抛异常（见文件头）。
    blocks = build_consistent_pipeline_results("debris_flow", event_id="EVT-T09")
    blocks["warning_result"]["alert"]["mock_only"] = False  # 注入一个确定可查的问题
    fb = run_feedback_agent("测试9：LLM 不可用", hazard_type="debris_flow", event_id="EVT-T09", **blocks)
    assert fb.get("status") == "issues_found", fb.get("status")
    assert _categories(fb) >= {"warning_consistency_issue"}
    refl = fb.get("reflection_result") or {}
    assert refl.get("used_llm") is False, "应走确定性兜底反思"
    assert refl.get("reflections"), "兜底反思应产出逐条分析"
    sugg = fb.get("improvement_suggestions") or []
    assert sugg and all(s.get("suggested_action") for s in sugg), "兜底建议应齐全"
    print("  ✅ 用例9：LLM 完全不可用时，检查/反思/建议/存档全链路仍确定性执行")


# ============================================================
# 10. 证据不足时不会编造问题原因
# ============================================================
def test_10_no_fabricated_cause_when_evidence_lacks():
    # 10a：只给 Data，缺 Risk/Simulation/Warning → insufficient_evidence，不反思
    data_only = build_consistent_pipeline_results("rockfall", event_id="EVT-T10A")
    fb = run_feedback_agent(
        "测试10a：证据不足",
        hazard_type="rockfall",
        event_id="EVT-T10A",
        data_result=data_only["data_result"],
        risk_result={},
        simulation_result={},
        warning_result={},
    )
    assert fb.get("status") == "insufficient_evidence", fb.get("status")
    assert "insufficient_evidence" in _categories(fb)
    assert not (fb.get("reflection_result") or {}).get("reflections"), "证据不足时不应产生归因结论（不编造原因）"

    # 10b：低置信问题（证据链为空）→ 必须标 needs_review、不得进入"高置信"行动建议
    blocks = build_consistent_pipeline_results("landslide", event_id="EVT-T10B")
    blocks["risk_result"]["evidence"] = []  # 触发 confidence=0.5 的 minor 条目
    blocks["risk_result"]["status"] = "success"
    fb = run_feedback_agent("测试10b：低置信", hazard_type="landslide", event_id="EVT-T10B", **blocks)
    low = [i for i in fb["feedback_items"] if i.get("confidence", 1) < 0.7]
    assert low and all(i.get("needs_review") for i in low), "低置信条目必须标记待复核"
    for act in fb.get("manager_actions", []):
        if act["agent"] == "risk" and not act["evidence_high_confidence"]:
            assert act["recommended_action"] == "manual_review_first", "低置信问题只能建议人工复核，不得建议直接重跑"

    # 10c：外部反馈"未发生灾害"不得被反推为风险错误
    ext = {
        "observed_at": "2026-01-10T00:00:00Z",
        "data_source": "现场核查",
        "region": "西藏波密",
        "observation_content": "预警窗口内未发生泥石流",
        "reliability": "high",
        "review_status": "verified",
        "related_event_id": "EVT-T10C",
    }
    blocks = build_consistent_pipeline_results("debris_flow", event_id="EVT-T10C")
    fb = run_feedback_agent("测试10c：外部反馈", hazard_type="debris_flow", event_id="EVT-T10C", external_feedback=ext, **blocks)
    wrong_calls = [i for i in fb["feedback_items"] if "未发生" in i["description"] and "risk_calculation" in i["category"]]
    assert not wrong_calls, "不得把'未发生灾害'判定为风险评估错误"
    print("  ✅ 用例10：证据不足直接输出不编造原因；低置信仅建议人工复核；'未灾害'不反推'风险错误'")


# ============================================================
# 11. 反馈记录可以保存并读取
# ============================================================
def test_11_memory_save_and_load():
    blocks = build_consistent_pipeline_results("debris_flow", event_id="EVT-T11")
    blocks["risk_result"]["model_info"]["rule_version"] = "OLD-v0.0"  # 触发版本问题
    fb = run_feedback_agent("测试11：记忆存取", hazard_type="debris_flow", event_id="EVT-T11", **blocks)
    mem = fb.get("memory") or {}
    assert mem.get("saved_count", 0) >= 1, "问题经验未入库"
    assert STORAGE_PATH.exists(), "经验存储文件不存在"
    loaded = load_feedback_records(event_id="EVT-T11")
    assert loaded, "按 event_id 读取不到已保存记录"
    required_fields = [
        "feedback_id",
        "event_id",
        "hazard_type",
        "problem_type",
        "problem_description",
        "reflection",
        "suggested_action",
        "evidence",
        "model_version",
        "rule_version",
        "validation_status",
        "created_at",
    ]
    missing = [f for f in required_fields if f not in loaded[0]]
    assert not missing, f"经验记录缺少任务书字段: {missing}"
    assert loaded[0]["validation_status"] in ("auto_validated", "pending_review")
    # 再次运行同灾种 → 历史经验应被检索（只读参考）
    fb2 = run_feedback_agent("测试11续：经验复用", hazard_type="debris_flow", event_id="EVT-T11-REUSE", **blocks)
    hist_ids = {h.get("event_id") for h in (fb2.get("historical_feedback_reference") or [])}
    assert "EVT-T11" in hist_ids, "历史经验未被检索复用"
    print("  ✅ 用例11：反馈记录保存/读取正常，任务书字段齐全，经验可复用")


# ============================================================
# 12. 反思循环有最大次数限制
# ============================================================
def test_12_reflection_loop_bounded():
    blocks = build_consistent_pipeline_results("rockfall", event_id="EVT-T12")
    blocks["risk_result"]["model_info"]["rule_version"] = "OLD-v0.0"  # 高置信问题确保进反思
    fb = run_feedback_agent(
        "测试12：回环限界",
        hazard_type="rockfall",
        event_id="EVT-T12",
        max_iterations=2,
        test_hooks={"force_needs_another_round": True},
        **blocks,
    )
    # 强制请求额外轮次时，iteration_count 也必须恰好停在 max_iterations（有限步终止）
    assert fb["metadata"]["iterations_used"] == 2, f"反思轮次应受限为 2，实际 {fb['metadata']['iterations_used']}"
    assert fb["metadata"]["max_iterations"] == 2
    assert fb.get("status") in ("issues_found", "needs_review"), "图必须正常终止并输出"
    print("  ✅ 用例12：强制回环请求下反思轮次仍被限界在 max_iterations=2，无无限循环")


# ============================================================
# 13. 反馈建议不会直接修改上游 Agent 的结果
# ============================================================
def test_13_upstream_results_untouched():
    blocks = build_consistent_pipeline_results("debris_flow", event_id="EVT-T13")
    blocks["risk_result"]["model_info"]["rule_version"] = "OLD-v0.0"
    blocks["warning_result"]["alert"]["mock_only"] = False
    snapshot = copy.deepcopy(blocks)  # 注入异常后、运行前快照
    fb = run_feedback_agent("测试13：不修改上游", hazard_type="debris_flow", event_id="EVT-T13", **blocks)
    for key in ("data_result", "risk_result", "simulation_result", "warning_result"):
        assert blocks[key] == snapshot[key], f"Feedback 运行中修改了上游 {key}！"
    decl = fb.get("safety_declarations") or {}
    assert decl.get("modified_upstream_results") is False
    assert decl.get("risk_scores_changed") is False
    assert decl.get("warning_levels_changed") is False
    assert decl.get("thresholds_or_weights_changed") is False
    print("  ✅ 用例13：上游四类结果逐字段比对零改动；安全声明自证未越权")


# ============================================================
# 14. 输出结构符合 Manager Agent 的输入要求
# ============================================================
def test_14_manager_contract():
    blocks = build_consistent_pipeline_results("avalanche" and "ice_rock_snow_avalanche", event_id="EVT-T14")
    blocks["simulation_result"]["hazard_type"] = "landslide"  # 制造问题走完整链路
    fb = run_feedback_agent("测试14：Manager 契约", hazard_type="ice_rock_snow_avalanche", event_id="EVT-T14", **blocks)
    required_top = [
        "feedback_id",
        "event_id",
        "hazard_type",
        "generated_at",
        "status",
        "summary",
        "feedback_items",
        "evaluation_result",
        "analysis_notes",
        "reflection_result",
        "improvement_suggestions",
        "memory",
        "manager_actions",
        "safety_declarations",
        "metadata",
    ]
    missing = [f for f in required_top if f not in fb]
    assert not missing, f"feedback_result 缺少 Manager 所需字段: {missing}"
    assert fb["status"] in ("no_issues", "issues_found", "needs_review", "insufficient_evidence", "failed")
    assert fb["metadata"]["iterations_used"] <= fb["metadata"]["max_iterations"]
    for item in fb["feedback_items"]:
        assert item["category"] in {
            "data_quality_issue",
            "risk_calculation_issue",
            "simulation_issue",
            "warning_consistency_issue",
            "hazard_type_mismatch",
            "model_version_issue",
            "insufficient_evidence",
            "workflow_error",
        }
        assert item["agent"] in ("data", "risk", "simulation", "warning", "manager")
        assert item["severity"] in ("info", "minor", "major", "critical")
    for act in fb["manager_actions"]:
        assert act["recommended_action"] in (
            "rerun_suggested",
            "rerun_investigation_suggested",
            "manual_review_first",
            "pipeline_fix_suggested",
        )
    print("  ✅ 用例14：输出结构、枚举与 Manager 交互契约一致")


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    tests = [
        test_01_five_hazards_recognized,
        test_02_unknown_hazard_rejected,
        test_03_normal_input_no_false_issues,
        test_04_missing_key_fields_detected,
        test_05_stale_data_detected,
        test_06_mock_as_real_detected,
        test_07_risk_simulation_hazard_mismatch,
        test_08_warning_inconsistency_detected,
        test_09_works_without_llm,
        test_10_no_fabricated_cause_when_evidence_lacks,
        test_11_memory_save_and_load,
        test_12_reflection_loop_bounded,
        test_13_upstream_results_untouched,
        test_14_manager_contract,
    ]

    print("=" * 60)
    print("Feedback Agent 测试（任务书第十节 14 项，LLM 离线兜底模式）")
    print("=" * 60)
    failed = []
    for t in tests:
        name = t.__name__
        try:
            t()
        except Exception as e:
            failed.append((name, e))
            print(f"  ❌ {name}: {e}")
    print("=" * 60)
    if failed:
        print(f"存在 {len(failed)} 个失败用例 ❌")
        sys.exit(1)
    print(f"全部 {len(tests)} 项测试通过 ✅")
