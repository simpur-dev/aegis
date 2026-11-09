"""
Manager 真实集成测试（决策点 5）：Manager 调度【真实】Simulation / Warning Agent

【与 test_manager_agent.py 的区别】
  test_manager_agent.py 用 Mock 子图整体替换五 Agent，只验证调度骨架；
  本文件把 dispatcher 注册表恢复为【真实 Agent 实现】（restore_runners），
  验证 Manager 对真实专业 Agent 的调用 / 状态更新 / 结果传递 / 下游衔接。

【为什么直供上游（重要）】
  真实 Data Agent 内部调用 LLM（used_llm=True），其数据完整性/充分性判定
  非确定性 —— 直接跑 full_analysis 会因某轮 Data 自报 sufficient=False 而
  被 Manager 正确判为 insufficient_data，导致真实链路走不到 Simulation。
  这不是缺陷，而是"诚实拦截"（不拿不足数据伪装下游）。
  因此本集成测试用【确定性契约 fixture】直供 data_result / risk_result
  （risk 走真实因子引擎 _mock_risk_result，非 LLM），并用
  preset_scenario_configs 让真实 Simulation 跳过 LLM 情景设计，
  从而稳定验证 Manager 对真实 Simulation / Warning 的调度与传参。

★【诚实边界（决策点 5）】★
  本测试验证 Manager 对 Simulation Agent 的调用/状态/传参及 Warning 后续执行，
  Simulation 内部仍是 MOCK-v0.1 玩具模型（is_real_prediction=False）；
  测试通过 ≠ 真实地质模拟模型的预测效力得到验证。

【运行】
  cd C:\\Users\\lixin\\Desktop\\地政预警项目
  .venv\\Scripts\\python.exe -m agents.manager_agent.test_manager_integration
"""

import sys
from pathlib import Path


from aegis.agents_ext.manager_agent import dispatcher  # noqa: E402
from aegis.agents_ext.manager_agent.main import run_manager_agent  # noqa: E402
from aegis.agents_ext.manager_agent.state import (  # noqa: E402
    AGENT_DATA, AGENT_SIMULATION, AGENT_WARNING,
)

# 两个确定性直传情景（medium/high）：非空时真实 Simulation 跳过 LLM 情景设计，
# 结果可复现（口径与 e2e_pipeline_test 的 preset 一致）。
_PRESET = [
    {"scenario_id": "SC-01", "severity_level": "medium", "trigger": "中等降雨",
     "trigger_source_tag": "assumption", "rainfall_mm_h": 40.0,
     "catchment_area_km2": 5.0, "channel_gradient": 0.3, "duration_hours": 6.0,
     "notes": "Manager 集成测试情景"},
    {"scenario_id": "SC-02", "severity_level": "high", "trigger": "强降雨",
     "trigger_source_tag": "assumption", "rainfall_mm_h": 90.0,
     "catchment_area_km2": 8.0, "channel_gradient": 0.5, "duration_hours": 6.0,
     "notes": "Manager 集成测试情景"},
]


def _real_upstream(hazard: str = "debris_flow"):
    """确定性契约上游：复用各 Agent 既有 fixture（不重写业务逻辑）。

    data  = simulation_agent 的对齐真实 Data 交付物 fixture（is_mock=True）；
    risk  = warning_agent 走【真实因子引擎】构造的 Risk 交付物（无 LLM，确定性）。
    """
    from aegis.agents_ext.simulation_agent.main import _mock_data_result
    from aegis.agents_ext.warning_agent.main import _mock_risk_result
    data = _mock_data_result()
    data["hazard_type"] = hazard
    risk = _mock_risk_result(hazard)
    return data, risk


def _use_real_agents():
    dispatcher.restore_runners()          # 撤销任何 Mock，恢复真实 Agent 适配层


def _order(final_report):
    return [c["agent"] for c in final_report["execution_summary"]["agent_call_log"]]


def test_INT_manager_calls_real_simulation():
    """决策点 5 核心：Manager 真实调用 Simulation，状态更新 + 结果传递 + preset 传参。"""
    _use_real_agents()
    sup_data, sup_risk = _real_upstream("debris_flow")
    r = run_manager_agent("debris_flow", task_type="simulation_only",
                          region="西藏波密", event_id="EVT-INT-SIM",
                          data_result=sup_data, risk_result=sup_risk,
                          preset_scenario_configs=_PRESET)
    # 真实 Simulation 被 Manager 调用并成功
    assert r["agent_status"][AGENT_SIMULATION] in ("success", "uncertain"), r["agent_status"]
    assert _order(r["final_report"]) == [AGENT_SIMULATION], _order(r["final_report"])
    assert r["agent_status"][AGENT_DATA] == "provided", "直供上游应登记 provided"
    # 结果传递：交付物落地、灾种一致、Mock 红线诚实标注
    sim = r["simulation_result"]
    assert sim and sim["hazard_type"] == "debris_flow"
    assert sim["model_info"]["is_real_prediction"] is False, "★ 必须诚实标注非真实预测"
    assert len(sim.get("simulation_results", [])) >= 1, "真实模拟应产出情景结果"
    md = r["final_report"]["mock_disclosure"]
    assert md["data_is_mock"] is True and md["simulation_is_real_prediction"] is False
    print(f"✅ INT-1：Manager 真实调用 Simulation（{len(sim['simulation_results'])} 情景）"
          f"｜状态更新、传参、Mock 红线透传全部正确")


def test_INT_real_simulation_to_warning_handoff():
    """决策点 5 下游衔接：Manager 产出的真实 simulation_result 交给真实 Warning 消费。"""
    _use_real_agents()
    sup_data, sup_risk = _real_upstream("debris_flow")
    # 第一步：Manager 调度真实 Simulation
    step1 = run_manager_agent("debris_flow", task_type="simulation_only",
                              event_id="EVT-INT-HANDOFF-SIM",
                              data_result=sup_data, risk_result=sup_risk,
                              preset_scenario_configs=_PRESET)
    assert step1["task_status"] == "completed", (step1["task_status"], step1["error_message"])
    produced_sim = step1["simulation_result"]
    # 第二步：Manager 调度真实 Warning，消费上一步 Manager 产出的模拟交付物
    step2 = run_manager_agent("debris_flow", task_type="warning_only",
                              event_id="EVT-INT-HANDOFF-WARN",
                              risk_result=sup_risk, data_result=sup_data,
                              simulation_result=produced_sim)
    assert step2["agent_status"][AGENT_WARNING] == "draft", step2["agent_status"]
    assert _order(step2["final_report"]) == [AGENT_WARNING]
    alert = step2["warning_result"]["alert"]
    assert alert["hazard_type"] == "debris_flow"
    assert alert["mock_only"] is True and alert["official_grade"] is None, \
        "等级唯一由 Warning 规则引擎决定，且不伪装真实"
    print("✅ INT-2：Manager 产出的真实 simulation_result 成功交付真实 Warning（衔接正确）")


def test_INT_real_warning_independent_and_capability_note():
    """真实 Warning 可在直供 Risk 上独立运行；能力限制（region 过滤未实现）如实呈现。"""
    _use_real_agents()
    _, sup_risk = _real_upstream("debris_flow")
    r = run_manager_agent("debris_flow", task_type="warning_only",
                          region="西藏波密", event_id="EVT-INT-WARN",
                          risk_result=sup_risk)
    assert r["task_status"] == "completed", (r["task_status"], r["error_message"])
    assert _order(r["final_report"]) == [AGENT_WARNING]
    assert r["warning_result"]["status"] == "draft"
    caps = " ".join(r["final_report"]["capability_limits"])
    assert "region/analysis_time" in caps and "尚未实现" in caps, caps
    print("✅ INT-3：真实 Warning 独立运行；region 过滤未实现如实声明（决策点 3）")


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    print("=" * 60)
    print("Manager 真实集成测试（真实 Simulation/Warning + preset 确定性）")
    print("范围：调度调用/状态更新/结果传递/下游衔接；不含真实地质模型效力验证")
    print("=" * 60)
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
    print(f"\n===== Manager 真实集成测试：{len(tests) - failed}/{len(tests)} 通过 =====")
    sys.exit(1 if failed else 0)
