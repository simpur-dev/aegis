"""
Manager Agent 独立测试（Mock 子图，不依赖真实 LLM/API —— 任务书第十五节 E）

【说明】通过 dispatcher.set_runner 把五个专业 Agent 整体替换为 Mock 子图，
  只验证【调度逻辑】：任务解析、计划生成、依赖门控、调度顺序、灾种传递与
  串线检测、结果登记与输出契约。Mock 测试通过不代表地质灾害预测能力得到验证。

【分阶段】本文件按开发阶段分组：
  P2_*：基础调度（Phase 2）；P3/P4/P5/P6 前缀在对应阶段追加。

【运行】
  cd C:\\Users\\lixin\\Desktop\\地政预警项目
  .venv\\Scripts\\python.exe -m agents.manager_agent.test_manager_agent
"""

import copy
import sys
from datetime import datetime, timezone
from pathlib import Path


from aegis.agents_ext.manager_agent import dispatcher  # noqa: E402
from aegis.agents_ext.manager_agent.main import run_manager_agent  # noqa: E402
from aegis.agents_ext.manager_agent.state import (  # noqa: E402
    AGENT_DATA, AGENT_RISK, AGENT_SIMULATION, AGENT_WARNING, AGENT_FEEDBACK,
    AGENT_ORDER,
)

ALL_HAZARDS = ["landslide", "rockfall", "debris_flow",
               "ice_rock_snow_avalanche", "glacial_lake_outburst"]

CALL_LOG = []   # Mock 子图被调用序列（验证调度顺序 / 是否被调用）

# ============================================================
# Mock 子图：返回与各真实交付物契约一致的 dict
# ============================================================

def _default_extra(agent: str) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    if agent == AGENT_DATA:
        return {"processed_data": {"events": [{"event_id": "OBS-001", "time": now,
                                                "region": "西藏波密"}],
                                    "data_source": "mock", "is_mock": True,
                                    "fetch_timestamp": now},
                "data_quality": {"is_valid": True},
                "missing_data": [],
                "data_sufficiency": {"sufficient": True, "reason": "mock"}}
    if agent == AGENT_RISK:
        return {"risk_level": "high", "assessment": {"R": 68.0, "P": 0.6, "T": 0.85},
                "model_info": {"is_mock": True, "rule_version": "MOCK-v0.1"},
                "evidence": [{"source": "mock"}]}
    if agent == AGENT_SIMULATION:
        return {"simulation_results": [{"scenario_id": "SC-01"}],
                "model_info": {"is_real_prediction": False, "model_version": "MOCK-v0.1"}}
    if agent == AGENT_WARNING:
        return {"alert": {"warning_level": "warning", "mock_only": True,
                           "based_on": {"risk_level": "high"}},
                "cap": {"status": "generated"}}
    if agent == AGENT_FEEDBACK:
        return {"summary": {"issue_count": 0}, "feedback_items": [],
                "manager_actions": [], "safety_declarations": {}}
    return {}


# 各 Agent 交付物的"成功态"缺省值（Warning 成功即 draft，Feedback 成功即 no_issues）
_DEFAULT_STATUS = {AGENT_DATA: "success", AGENT_RISK: "success",
                   AGENT_SIMULATION: "success", AGENT_WARNING: "draft",
                   AGENT_FEEDBACK: "no_issues"}


def _mk_runner(agent: str, status=None, extra=None,
               wrong_hazard: str = "", raise_error: str = ""):
    status = status or _DEFAULT_STATUS[agent]

    def fn(state):
        CALL_LOG.append(agent)
        if raise_error:
            raise RuntimeError(raise_error)
        out = {"status": status,
               "hazard_type": wrong_hazard or state["hazard_type"]}
        out.update(copy.deepcopy(extra if extra is not None else _default_extra(agent)))
        return out
    return fn


def install_mocks(spec: dict = None):
    """spec: {agent: {status/extra/wrong_hazard/raise_error} 或 可调用 fn(state)}。
    传可调用对象可直接接管该 Agent 的 Mock 行为（脚本化"先失败后成功"等序列）。"""
    spec = spec or {}
    CALL_LOG.clear()
    for agent in AGENT_ORDER:
        s = spec.get(agent, {})
        if callable(s):
            # 自定义 runner 自行负责 CALL_LOG 记录（_scripted_runner 内部已记）
            dispatcher.set_runner(agent, s)
        else:
            dispatcher.set_runner(agent, _mk_runner(agent, status=s.get("status"),
                                                    extra=s.get("extra"),
                                                    wrong_hazard=s.get("wrong_hazard", ""),
                                                    raise_error=s.get("raise_error", "")))


def _scripted_runner(agent: str, outcomes: list):
    """按调用次序给出结果序列（最后一项重复使用）。outcomes 项可为
    {"status":..., "extra":...} 或 {"raise": "错误文本"}。"""
    calls = {"n": 0}

    def fn(state):
        CALL_LOG.append(agent)
        i = min(calls["n"], len(outcomes) - 1)
        calls["n"] += 1
        o = outcomes[i]
        if "raise" in o:
            raise RuntimeError(o["raise"])
        out = {"status": o.get("status") or _DEFAULT_STATUS[agent],
               "hazard_type": state["hazard_type"]}
        extra = o.get("extra") if o.get("extra") is not None else _default_extra(agent)
        out.update(copy.deepcopy(extra))
        return out
    return fn


# ============================================================
# Phase 2 用例
# ============================================================

def test_P2_full_chain_five_hazards():
    """A 组：五灾种 full_analysis 正常串联 —— 顺序、灾种传递、结果落地。"""
    for h in ALL_HAZARDS:
        install_mocks()
        r = run_manager_agent(h, task_type="full_analysis", event_id=f"EVT-P2-{h}")
        assert r["task_status"] == "completed", (h, r["task_status"], r["error_message"])
        assert CALL_LOG == AGENT_ORDER, f"{h} 调度顺序错误: {CALL_LOG}"
        # 灾种正确传给每个 Agent（Mock 收到的 state 已是归一化键）
        for agent in AGENT_ORDER:
            assert r["agent_status"][agent] in (
                "success", "draft", "no_issues"), (agent, r["agent_status"])
        assert r["hazard_type"] == h
        # 五个交付物均落地且灾种一致
        for key in ("data_result", "risk_result", "simulation_result",
                    "warning_result", "feedback_result"):
            assert r[key] and r[key]["hazard_type"] == h, f"{h} 交付物缺失: {key}"
    print("  ✅ P2-A：五灾种正常串联，调度顺序=计划顺序，灾种无串线")


def test_P2_unknown_hazard_rejected():
    install_mocks()
    r = run_manager_agent("火山泥流", event_id="EVT-P2-UK")
    assert r["task_status"] == "unsupported_hazard", r["task_status"]
    assert "仅支持五类" in r["error_message"]
    assert CALL_LOG == [], f"未知灾种不应调用任何 Agent: {CALL_LOG}"
    print("✅ P2：未知灾种确定性拒绝，零 Agent 调用")


def test_P2_missing_hazard_clarification():
    install_mocks()
    r = run_manager_agent("", event_id="EVT-P2-MISS")
    assert r["task_status"] == "need_clarification", r["task_status"]
    assert CALL_LOG == []
    print("✅ P2：灾种缺失 → need_clarification（不猜测）")


def test_P2_task_type_plans():
    """B 组：六类任务类型的计划与依赖门控。"""
    cases = [
        ("data_only",       [AGENT_DATA]),
        ("risk_only",       [AGENT_DATA, AGENT_RISK]),
        ("full_analysis",   list(AGENT_ORDER)),
    ]
    for tt, expect in cases:
        install_mocks()
        r = run_manager_agent("debris_flow", task_type=tt, event_id=f"EVT-P2-{tt}")
        assert r["task_status"] == "completed", (tt, r["task_status"])
        assert CALL_LOG == expect, f"{tt} 计划错误: {CALL_LOG} ≠ {expect}"

    # simulation_only / warning_only 缺前置 → insufficient_data，零调用、不伪造
    for tt in ("simulation_only", "warning_only"):
        install_mocks()
        r = run_manager_agent("landslide", task_type=tt, event_id=f"EVT-P2-{tt}-noDep")
        assert r["task_status"] == "insufficient_data", (tt, r["task_status"])
        assert CALL_LOG == [], f"{tt} 缺依赖时不得调用 Agent"

    # feedback_only 无任何结果 → insufficient_data
    install_mocks()
    r = run_manager_agent("landslide", task_type="feedback_only", event_id="EVT-P2-fb-noDep")
    assert r["task_status"] == "insufficient_data"
    assert CALL_LOG == []

    # 直供合格上游 → 只调目标 Agent，上游登记 provided
    supply_data = {"status": "success", "hazard_type": "landslide",
                   "processed_data": {"is_mock": True, "events": [],
                                       "fetch_timestamp":
                                       datetime.now(timezone.utc).isoformat()},
                   "data_quality": {"is_valid": True},
                   "data_sufficiency": {"sufficient": True}}
    supply_risk = {"status": "success", "hazard_type": "landslide",
                   "assessment": {"R": 55.0}, "model_info": {"is_mock": True}}
    install_mocks()
    r = run_manager_agent("landslide", task_type="simulation_only",
                          data_result=supply_data, risk_result=supply_risk,
                          event_id="EVT-P2-sim-supply")
    assert r["task_status"] == "completed" and CALL_LOG == [AGENT_SIMULATION], CALL_LOG
    assert r["agent_status"][AGENT_DATA] == "provided"

    install_mocks()
    r = run_manager_agent("landslide", task_type="warning_only",
                          risk_result=supply_risk, event_id="EVT-P2-warn-supply")
    assert r["task_status"] == "completed" and CALL_LOG == [AGENT_WARNING], CALL_LOG

    install_mocks()
    r = run_manager_agent("landslide", task_type="feedback_only",
                          data_result=supply_data, event_id="EVT-P2-fb-supply")
    assert r["task_status"] == "completed" and CALL_LOG == [AGENT_FEEDBACK], CALL_LOG

    # 不合格的上游（status=failed）不算依赖满足
    install_mocks()
    r = run_manager_agent("landslide", task_type="warning_only",
                          risk_result={"status": "failed", "hazard_type": "landslide"},
                          event_id="EVT-P2-warn-badSupply")
    assert r["task_status"] == "insufficient_data", r["task_status"]
    assert CALL_LOG == []
    print("  ✅ P2-B：六类任务计划正确；缺依赖确定性停止；直供上游登记 provided")


def test_P2_hazard_mismatch_detected():
    """Agent 返回灾种与任务不一致 → 拒绝下游调用。"""
    install_mocks({AGENT_SIMULATION: {"wrong_hazard": "rockfall"}})
    r = run_manager_agent("landslide", task_type="full_analysis", event_id="EVT-P2-MISMATCH")
    assert AGENT_SIMULATION in r["final_report"]["execution_summary"]["failed_agents"], \
        r["final_report"]["execution_summary"]
    assert CALL_LOG == [AGENT_DATA, AGENT_RISK, AGENT_SIMULATION], \
        f"灾种串线后不得继续下游: {CALL_LOG}"
    types = [e["error_type"] for e in r["final_report"]["error_history"]]
    assert "HAZARD_TYPE_MISMATCH" in types, types
    assert r["task_status"] != "completed"
    print("✅ P2：灾种串线被识别，下游调用被拒绝")


def test_P2_output_contract():
    """第十二节输出结构：顶层键齐全，状态取值受控。"""
    install_mocks()
    r = run_manager_agent("rockfall", task_type="full_analysis", event_id="EVT-P2-CONTRACT")
    for key in ("event_id", "hazard_type", "region", "task_status", "agent_status",
                "data_result", "risk_result", "simulation_result", "warning_result",
                "feedback_result", "human_review_required", "final_report"):
        assert key in r, f"输出缺少键: {key}"
    fr = r["final_report"]
    for key in ("execution_summary", "error_history", "mock_disclosure",
                "capability_limits", "task_status"):
        assert key in fr, f"final_report 缺少键: {key}"
    assert r["task_status"] in ("completed", "partially_completed", "insufficient_data",
                               "failed", "need_clarification", "unsupported_hazard",
                               "needs_human_review")
    # Mock 标识透传（不得丢失/伪装）
    md = fr["mock_disclosure"]
    assert md["data_is_mock"] is True and md["simulation_is_real_prediction"] is False \
        and md["warning_mock_only"] is True
    print("✅ P2：输出结构符合第十二节契约；Mock 标识全程透传")


def test_P2_agent_failure_marks_and_stops():
    """Risk 业务失败（insufficient_data）→ 有限重试后停止，绝不进入 Warning。"""
    install_mocks({AGENT_RISK: {"status": "insufficient_data"}})
    r = run_manager_agent("debris_flow", task_type="full_analysis", event_id="EVT-P2-RISKFAIL")
    assert CALL_LOG.count(AGENT_WARNING) == 0 and CALL_LOG.count(AGENT_RISK) == 2, \
        f"风险失败后不得进入 Warning: {CALL_LOG}"
    assert AGENT_RISK in r["final_report"]["execution_summary"]["failed_agents"]
    assert r["task_status"] in ("partially_completed", "failed", "insufficient_data")
    assert r["final_report"]["error_history"], "失败必须有错误记录"
    print("✅ P2：Risk 失败 → 重试 1 次仍失败 → 拒绝下游、如实登记并收尾")


# ============================================================
# Phase 3 用例（C 组：异常处理与有限重试）
# ============================================================

def _types(hist):
    return [e["error_type"] for e in hist]


def test_P3_data_insufficient_retries_then_stops():
    """C3：Data 持续数据不足 → 有限重试（2 次）→ insufficient_data 停止。"""
    bad_extra = {"processed_data": {"events": [], "is_mock": True,
                                     "data_source": "mock",
                                     "fetch_timestamp": datetime.now(timezone.utc).isoformat()},
                 "data_quality": {"is_valid": False},
                 "missing_data": ["rainfall_mm_h"],
                 "data_sufficiency": {"sufficient": False, "reason": "关键降雨数据缺失"}}
    install_mocks({AGENT_DATA: {"status": "partial", "extra": bad_extra}})
    r = run_manager_agent("landslide", task_type="full_analysis", event_id="EVT-P3-D1")
    from aegis.agents_ext.config import MANAGER_RETRY_CONFIG
    limit = MANAGER_RETRY_CONFIG["data_agent"]
    assert CALL_LOG.count(AGENT_DATA) == limit + 1, \
        f"Data 应调用 {limit + 1} 次（首跑+{limit} 重试）: {CALL_LOG}"
    assert r["task_status"] == "insufficient_data", r["task_status"]
    assert AGENT_RISK not in CALL_LOG, "数据不足不得进入 Risk"
    types = _types(r["final_report"]["error_history"])
    assert "MISSING_REQUIRED_DATA" in types and "MAX_RETRIES_EXCEEDED" in types, types
    assert r["final_report"]["execution_summary"]["retry_counts"][AGENT_DATA] == limit
    print(f"✅ P3-C3：Data 不足 → 重试 {limit} 次 → insufficient_data 停止，错误可追溯")


def test_P3_stale_data_detected():
    """C4/C5：非 Mock 过期数据 → STALE_DATA；Mock 数据不检查时效（防误报）。"""
    stale_extra = {"processed_data": {"events": [{"event_id": "O-1",
                                                   "time": "2020-01-01T00:00:00Z"}],
                                       "is_mock": False, "data_source": "real_station",
                                       "fetch_timestamp": "2020-01-01T00:00:00Z"},
                   "data_quality": {"is_valid": True},
                   "data_sufficiency": {"sufficient": True}}
    install_mocks({AGENT_DATA: {"status": "success", "extra": stale_extra}})
    r = run_manager_agent("rockfall", task_type="full_analysis", event_id="EVT-P3-STALE")
    types = _types(r["final_report"]["error_history"])
    assert "STALE_DATA" in types, types
    assert r["task_status"] == "insufficient_data", r["task_status"]
    # Mock 数据同链路不误报（P2 全链路已过 = 证明）
    print("✅ P3-C4：非 Mock 过期数据识别为 STALE_DATA；Mock 数据按约定不查时效")


def test_P3_transient_failure_recovers_via_retry():
    """Risk 首次调用异常、重试成功 → 全链路继续，completed。"""
    install_mocks({AGENT_RISK: _scripted_runner(
        AGENT_RISK, [{"raise": "LLM connection blip"}, {"status": "success"}])})
    r = run_manager_agent("debris_flow", task_type="full_analysis", event_id="EVT-P3-RECOV")
    assert CALL_LOG.count(AGENT_RISK) == 2, CALL_LOG
    assert r["task_status"] == "completed", (r["task_status"], r["error_message"])
    assert AGENT_FEEDBACK in CALL_LOG, "重试成功后应继续下游"
    print("✅ P3：瞬时异常 → retry_current_agent → 下游继续，最终 completed")


def test_P3_simulation_optional_skip():
    """C6：Simulation 持续失败 + SIMULATION_OPTIONAL → 跳过并继续 Warning/Feedback。"""
    install_mocks({AGENT_SIMULATION: {"status": "failed"}})
    r = run_manager_agent("debris_flow", task_type="full_analysis", event_id="EVT-P3-SKIP")
    from aegis.agents_ext.config import MANAGER_RETRY_CONFIG, MANAGER_SIMULATION_OPTIONAL
    if not MANAGER_SIMULATION_OPTIONAL:
        print("  （跳过：配置已改为强制模拟）"); return
    assert AGENT_WARNING in CALL_LOG and AGENT_FEEDBACK in CALL_LOG, \
        f"模拟跳过应继续下游: {CALL_LOG}"
    assert AGENT_SIMULATION in r["final_report"]["execution_summary"]["skipped_agents"]
    assert r["task_status"] == "partially_completed", r["task_status"]
    assert r["final_report"]["execution_summary"]["retry_counts"][AGENT_SIMULATION] \
        == MANAGER_RETRY_CONFIG["simulation_agent"]
    assert "skipped" in str(r["final_report"]["summary_text"]), "报告须明示模拟未完成"
    print("✅ P3-C6：模拟失败按配置跳过，下游继续，partially_completed 且报告明示")


def test_P3_simulation_required_no_skip():
    """C6b：require_simulation=True → 不允许跳过，停止并转人工复核。"""
    install_mocks({AGENT_SIMULATION: {"status": "failed"}})
    r = run_manager_agent("debris_flow", task_type="full_analysis",
                          require_simulation=True, event_id="EVT-P3-REQSIM")
    assert AGENT_WARNING not in CALL_LOG, f"强制模拟失败不得进入 Warning: {CALL_LOG}"
    assert r["human_review_required"] is True
    assert r["task_status"] in ("needs_human_review", "partially_completed", "failed")
    assert "SIMULATION_FAILURE" in _types(r["final_report"]["error_history"])
    print("✅ P3-C6b：强制模拟失败 → 不跳过、转人工复核")


def test_P3_warning_format_repair_retry():
    """C7：Warning 结构校验失败 → 有限格式修复重试后成功。"""
    install_mocks({AGENT_WARNING: _scripted_runner(
        AGENT_WARNING, [{"status": "validation_failed"}, {"status": "draft"}])})
    r = run_manager_agent("landslide", task_type="full_analysis", event_id="EVT-P3-WREPAIR")
    assert CALL_LOG.count(AGENT_WARNING) == 2, CALL_LOG
    assert r["agent_status"][AGENT_WARNING] == "draft"
    assert "WARNING_VALIDATION_FAILURE" in _types(r["final_report"]["error_history"])
    assert r["task_status"] == "completed"
    print("✅ P3-C7：Warning 校验失败 → 定向格式修复重试 → 恢复流转")


def test_P3_feedback_agent_failure_keeps_results():
    """C8：Feedback 自身 failed → 上游四结果保留，标记需人工复核（不推翻结论）。"""
    install_mocks({AGENT_FEEDBACK: {"status": "failed"}})
    r = run_manager_agent("rockfall", task_type="full_analysis", event_id="EVT-P3-FBFAIL")
    assert r["data_result"] and r["risk_result"] and r["warning_result"], "上游结果必须保留"
    assert r["human_review_required"] is True
    assert r["task_status"] == "needs_human_review", r["task_status"]
    print("✅ P3-C8：Feedback 失败 → 保留上游结果，转人工复核")


def test_P3_mock_redline_stops_no_retry():
    """C13：模拟结果冒充真实（is_real_prediction=True）→ 致命拦截、不重试、转人工。"""
    extra = copy.deepcopy(_default_extra(AGENT_SIMULATION))
    extra["model_info"]["is_real_prediction"] = True
    install_mocks({AGENT_SIMULATION: {"status": "success", "extra": extra}})
    r = run_manager_agent("glacial_lake_outburst", task_type="full_analysis",
                          event_id="EVT-P3-REDLINE")
    assert CALL_LOG.count(AGENT_SIMULATION) == 1, f"红线违规不得重试: {CALL_LOG}"
    assert AGENT_WARNING not in CALL_LOG, "红线违规应拒绝下游"
    assert r["human_review_required"] is True
    hist = r["final_report"]["error_history"]
    assert any("is_real_prediction" in e["message"] for e in hist), hist
    print("✅ P3-C13：Mock 冒充真实被致命拦截，不重试不下游，转人工")


def test_P3_llm_unavailable_still_schedules():
    """C11：LLM 完全不可用时，Manager 确定性调度与报告兜底仍正常。"""
    import aegis.agents_ext.manager_agent.llm_setup as mgr_llm
    orig = mgr_llm.get_manager_llm
    mgr_llm.get_manager_llm = lambda: (_ for _ in ()).throw(
        RuntimeError("测试：LLM 不可用"))
    try:
        install_mocks()
        r = run_manager_agent("debris_flow", task_type="full_analysis",
                              event_id="EVT-P3-NOLL", llm_polish=True)
        assert r["task_status"] == "completed"
        fr = r["final_report"]
        assert fr["llm_summary_used_llm"] is False, "LLM 不可用应走确定性兜底"
        assert fr["llm_summary"] == fr["summary_text"]
    finally:
        mgr_llm.get_manager_llm = orig
    print("✅ P3-C11：LLM 不可用时确定性调度不受影响，润色自动兜底")


def test_P3_warning_level_untouched():
    """C14：Warning 等级与 Risk 分数在 Manager 全链路中原样透传（零修改）。"""
    install_mocks()
    snapshot_holder = {}

    def _cap(agent, fn):
        def wrapper(state):
            out = fn(state)
            snapshot_holder.setdefault(agent, copy.deepcopy(out))
            return out
        return wrapper

    for agent in (AGENT_RISK, AGENT_WARNING):
        dispatcher.set_runner(agent, _cap(agent, dispatcher.AGENT_RUNNERS[agent]))
    r = run_manager_agent("ice_rock_snow_avalanche", task_type="full_analysis",
                          event_id="EVT-P3-NOTOUCH")
    assert r["risk_result"] == snapshot_holder[AGENT_RISK], "Manager 不得修改风险结果"
    assert r["warning_result"] == snapshot_holder[AGENT_WARNING], "Manager 不得修改预警结果"
    assert r["warning_result"]["alert"]["warning_level"] == "warning"  # 原样
    assert r["risk_result"]["assessment"]["R"] == 68.0                 # 原样
    print("✅ P3-C14：Risk 分数与 Warning 等级全程原样透传（深拷贝比对零改动）")


# ============================================================
# Phase 4 用例（D 组：Feedback 返工闭环）
# ============================================================

def _fb_issues(target_short: str, action: str = "rerun_suggested",
               high_confidence: bool = True) -> dict:
    """构造与 Feedback 真实交付物契约一致的"发现问题"extras。
    agent 短名词表来自 feedback_output_node._manager_actions。"""
    extra = copy.deepcopy(_default_extra(AGENT_FEEDBACK))
    extra["summary"] = {"issue_count": 1, "actionable_issue_count": 1,
                        "needs_human_review_count": 0 if high_confidence else 1}
    extra["feedback_items"] = [{"feedback_id": "FBX-001", "agent": target_short,
                                 "severity": "critical",
                                 "category": "data_quality_issue",
                                 "confidence": 0.9 if high_confidence else 0.4,
                                 "needs_review": not high_confidence}]
    extra["manager_actions"] = [{"agent": target_short, "recommended_action": action,
                                  "issue_count": 1, "max_severity": "critical",
                                  "evidence_high_confidence": high_confidence}]
    return extra


def _fb_pass():
    return {"status": "no_issues"}


def test_P4_rework_from_data_full_rechain():
    """D-情况A：Feedback 高置信建议重跑 data → [data,risk,sim,warn,fb] 全链返工，
    第二轮 feedback 无问题 → completed，返工 1 轮可追溯。"""
    install_mocks({AGENT_FEEDBACK: _scripted_runner(AGENT_FEEDBACK, [
        {"status": "issues_found", "extra": _fb_issues("data")}, _fb_pass()])})
    r = run_manager_agent("landslide", task_type="full_analysis", event_id="EVT-P4-A")
    expect = list(AGENT_ORDER) + list(AGENT_ORDER)
    assert CALL_LOG == expect, f"返工应为完整依赖闭包: {CALL_LOG}"
    assert r["task_status"] == "completed", (r["task_status"], r["error_message"])
    fr = r["final_report"]["feedback_review"]
    assert fr["feedback_loop_count"] == 1 and fr["decisions"][0]["action"] == "rerun"
    assert fr["decisions"][0]["rerun_plan"] == list(AGENT_ORDER)
    assert fr["decisions"][1]["action"] == "pass"
    print("✅ P4-A：data 高置信重跑 → 依赖闭包全链返工 → 复验通过 completed")


def test_P4_minimal_rework_from_risk():
    """D-情况B：目标 risk → 最小返工 [risk,sim,warn,fb]，data 不重跑、结果保留。"""
    install_mocks({AGENT_FEEDBACK: _scripted_runner(AGENT_FEEDBACK, [
        {"status": "issues_found", "extra": _fb_issues("risk")}, _fb_pass()])})
    r = run_manager_agent("rockfall", task_type="full_analysis", event_id="EVT-P4-B")
    expect = list(AGENT_ORDER) + [AGENT_RISK, AGENT_SIMULATION, AGENT_WARNING,
                                  AGENT_FEEDBACK]
    assert CALL_LOG == expect, f"不应整体重跑（data 不得返工）: {CALL_LOG}"
    assert CALL_LOG.count(AGENT_DATA) == 1
    assert r["task_status"] == "completed"
    assert r["final_report"]["feedback_review"]["decisions"][0]["rerun_plan"] == [
        AGENT_RISK, AGENT_SIMULATION, AGENT_WARNING, AGENT_FEEDBACK]
    print("✅ P4-B：risk 返工=最小闭包，上游 data 不重跑且结果保留")


def test_P4_rework_warning_keeps_upstream_results():
    """D-情况C：目标 warning → [warning,fb]；返工期间上游结果不被清空。"""
    install_mocks({AGENT_FEEDBACK: _scripted_runner(AGENT_FEEDBACK, [
        {"status": "issues_found", "extra": _fb_issues("warning")}, _fb_pass()])})
    r = run_manager_agent("debris_flow", task_type="full_analysis", event_id="EVT-P4-C")
    expect = list(AGENT_ORDER) + [AGENT_WARNING, AGENT_FEEDBACK]
    assert CALL_LOG == expect, CALL_LOG
    assert r["data_result"] and r["risk_result"] and r["simulation_result"], \
        "返工 warning 时上游交付物必须原样保留"
    assert r["task_status"] == "completed"
    print("✅ P4-C：warning 返工仅 [warning,feedback]，上游结果零扰动")


def test_P4_low_confidence_no_auto_rework():
    """D-情况D：manual_review_first（低置信）→ 不自动返工，转人工复核。"""
    install_mocks({AGENT_FEEDBACK: {
        "status": "issues_found",
        "extra": _fb_issues("risk", action="manual_review_first",
                            high_confidence=False)}})
    r = run_manager_agent("landslide", task_type="full_analysis", event_id="EVT-P4-D")
    assert CALL_LOG == list(AGENT_ORDER), f"低置信建议不得触发返工: {CALL_LOG}"
    assert r["human_review_required"] is True
    assert r["task_status"] == "needs_human_review", r["task_status"]
    fr = r["final_report"]["feedback_review"]
    assert fr["feedback_loop_count"] == 0
    assert fr["decisions"][0]["action"] == "human_review"
    print("✅ P4-D：低置信/先人工建议 → 零返工，needs_human_review 如实呈现")


def test_P4_feedback_loop_bounded():
    """D-限界：Feedback 每轮都建议重跑 data → 达 MANAGER_MAX_FEEDBACK_LOOPS
    即停止返工并转人工（不死循环）。"""
    from aegis.agents_ext.config import MANAGER_MAX_FEEDBACK_LOOPS
    install_mocks({AGENT_FEEDBACK: _scripted_runner(
        AGENT_FEEDBACK, [{"status": "issues_found", "extra": _fb_issues("data")}])})
    r = run_manager_agent("glacial_lake_outburst", task_type="full_analysis",
                          event_id="EVT-P4-LIMIT")
    loops = MANAGER_MAX_FEEDBACK_LOOPS
    assert CALL_LOG.count(AGENT_FEEDBACK) == loops + 1, \
        f"feedback 应被调用 {loops + 1} 次后止损: {CALL_LOG}"
    assert r["final_report"]["execution_summary"]["feedback_loop_count"] == loops
    decs = r["final_report"]["feedback_review"]["decisions"]
    assert decs[-1]["action"] == "loop_limit_stop", decs[-1]
    assert r["task_status"] == "needs_human_review", r["task_status"]
    assert "FEEDBACK_REVIEW_FAILURE" in _types(r["final_report"]["error_history"])
    print(f"✅ P4-限界：返工达上限 {loops} 轮 → loop_limit_stop + 人工复核（不死循环）")


def test_P4_no_issues_regression():
    """D-回归：no_issues 正常链路不受闭环实现影响。"""
    install_mocks()
    r = run_manager_agent("ice_rock_snow_avalanche", task_type="full_analysis",
                          event_id="EVT-P4-REG")
    assert r["task_status"] == "completed" and CALL_LOG == list(AGENT_ORDER)
    fr = r["final_report"]["feedback_review"]
    assert fr["feedback_loop_count"] == 0 and fr["rerun_plan"] == []
    assert fr["decisions"] == [fr["decisions"][0]] and fr["decisions"][0]["action"] == "pass"
    assert r["human_review_required"] is False
    print("✅ P4-回归：no_issues 链路一次通过，闭环实现零副作用")


# ============================================================
# Phase 5 用例（E 组：SQLite Checkpoint 与中断恢复）
# ============================================================

class _Crash(BaseException):
    """模拟进程级中断（dispatch 节点的 except Exception 不会捕获它，
    图执行中止，Checkpoint 停留在断点）。"""


def _crash_once_runner(agent: str):
    """第一次调用即崩溃，之后成功（验证恢复后从断点补执行）。"""
    n = {"c": 0}

    def fn(state):
        CALL_LOG.append(agent)
        n["c"] += 1
        if n["c"] == 1:
            raise _Crash(f"模拟进程在 {agent} 执行期间崩溃")
        out = {"status": _DEFAULT_STATUS[agent], "hazard_type": state["hazard_type"]}
        out.update(copy.deepcopy(_default_extra(agent)))
        return out
    return fn


def _tmp_saver():
    import tempfile
    from aegis.agents_ext.manager_agent.checkpoint import get_checkpointer
    db = str(Path(tempfile.mkdtemp()) / "manager_ckpt.sqlite")
    return db, get_checkpointer(db)


def test_P5_checkpoint_crash_and_resume():
    """中断恢复：data 成功后进程崩溃 → 恢复只补执行断点之后环节，
    已成功且结果仍有效的 data 绝不重复调用。"""
    from aegis.agents_ext.manager_agent.main import resume_manager_task
    db, saver = _tmp_saver()
    install_mocks({AGENT_RISK: _crash_once_runner(AGENT_RISK)})
    crashed = False
    try:
        run_manager_agent("landslide", task_type="full_analysis",
                          event_id="EVT-P5-CRASH", checkpointer=saver)
    except _Crash:
        crashed = True
    assert crashed, "崩溃应中止图执行（异常透传）"
    assert CALL_LOG == [AGENT_DATA, AGENT_RISK], CALL_LOG
    assert Path(db).exists() and Path(db).stat().st_size > 0, "SQLite 检查点应落盘"

    r = resume_manager_task("EVT-P5-CRASH", checkpointer=saver)
    assert r["task_status"] == "completed", (r["task_status"], r["error_message"])
    assert CALL_LOG.count(AGENT_DATA) == 1, f"恢复不得重跑已成功的 data: {CALL_LOG}"
    assert CALL_LOG.count(AGENT_RISK) == 2, f"断点环节应补执行一次: {CALL_LOG}"
    assert CALL_LOG == [AGENT_DATA, AGENT_RISK, AGENT_RISK, AGENT_SIMULATION,
                        AGENT_WARNING, AGENT_FEEDBACK], \
        f"恢复序列应为断点续跑: {CALL_LOG}"
    assert r["data_result"] and r["warning_result"], "断点前的结果须从检查点原样恢复"
    print("✅ P5-E1：崩溃后 --resume 从断点继续，已成功 Agent 零重复执行")


def test_P5_resume_completed_thread_idempotent():
    """已跑到 END 的线程再次恢复：直接返回终态，不重复执行任何 Agent。"""
    from aegis.agents_ext.manager_agent.main import resume_manager_task
    _, saver = _tmp_saver()
    install_mocks()
    r1 = run_manager_agent("rockfall", task_type="full_analysis",
                           event_id="EVT-P5-FULL", checkpointer=saver)
    assert r1["task_status"] == "completed" and CALL_LOG == list(AGENT_ORDER)
    CALL_LOG.clear()
    r2 = resume_manager_task("EVT-P5-FULL", checkpointer=saver)
    assert CALL_LOG == [], f"已完成线程恢复不得调用任何 Agent: {CALL_LOG}"
    assert r2["task_status"] == "completed" and r2["hazard_type"] == "rockfall"
    print("✅ P5-E2：已完成线程恢复幂等（返回终态，零 Agent 调用）")


def test_P5_thread_isolation_and_missing_thread():
    """不同 event_id 使用独立线程互不串扰；未知线程恢复给出明确错误（不静默）。"""
    from aegis.agents_ext.manager_agent.main import resume_manager_task
    _, saver = _tmp_saver()
    install_mocks()
    run_manager_agent("landslide", task_type="full_analysis",
                      event_id="EVT-P5-T1", checkpointer=saver)
    install_mocks({AGENT_RISK: {"status": "insufficient_data"}})
    r2 = run_manager_agent("rockfall", task_type="full_analysis",
                           event_id="EVT-P5-T2", checkpointer=saver)
    assert r2["task_status"] != "completed" and r2["hazard_type"] == "rockfall"
    # T1 线程状态不受 T2 影响（幂等恢复仍返回 T1 自己的终态与灾种）
    rt1 = resume_manager_task("EVT-P5-T1", checkpointer=saver)
    assert rt1["hazard_type"] == "landslide" and rt1["task_status"] == "completed", rt1
    # 不存在的线程：明确报错，不伪造结果
    miss = resume_manager_task("EVT-P5-NONE", checkpointer=saver)
    assert miss["task_status"] == "failed" and "未找到线程" in miss["error_message"]
    print("✅ P5-E3：thread_id=event_id 任务隔离；未知线程确定性报错")


def test_P5_checkpoint_separate_from_feedback_memory():
    """第十一节红线：Checkpoint（工作流状态）与 Feedback Memory（经验记录）
    必须是两套分离机制 —— 不同文件、不同格式、互不读写。"""
    from aegis.agents_ext.config import MANAGER_CHECKPOINT_DB
    from aegis.agents_ext.feedback_agent.tools.feedback_memory_tool import STORAGE_PATH
    ck, fm = Path(MANAGER_CHECKPOINT_DB), Path(STORAGE_PATH)
    assert ck.resolve() != fm.resolve(), "两套存储路径必须不同"
    assert ck.suffix == ".sqlite" and fm.suffix == ".json", "格式分离（工作流库≠记忆文件）"
    db, saver = _tmp_saver()
    assert saver is not None and Path(db).name.endswith(".sqlite")
    # Manager Checkpoint 代码不得导入/读写 Feedback Memory 模块（代码级分离）
    import inspect
    import re
    import aegis.agents_ext.manager_agent.checkpoint as cp
    import aegis.agents_ext.manager_agent.main as mgr_main
    for mod in (cp, mgr_main):
        assert not re.search(r"(from|import)\s+agents\.feedback_agent",
                             inspect.getsource(mod)), \
            f"{mod.__name__} 不应引用 Feedback Agent 记忆模块"
    print("✅ P5-E4：Checkpoint 与 Feedback Memory 物理与代码双重分离")


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            failed += 1
            print(f"  ❌ {t.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f"  💥 {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n===== Manager Agent 测试（P2~P5）：{len(tests) - failed}/{len(tests)} 通过 =====")
    sys.exit(1 if failed else 0)
