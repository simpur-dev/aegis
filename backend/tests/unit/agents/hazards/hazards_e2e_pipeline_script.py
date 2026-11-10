"""
端到端管线测试：Data Agent → Risk Agent → Simulation Agent → Warning Agent

【定位】验证四 Agent 在显式 hazard_type 下能依次串联运行，
       各级交付物（data_result / risk_result / simulation_result / warning_result）
       的灾种标识一致、关键字段齐全。
       ★ 只验证【流程运行】与【Mock 公式预设行为】，
         不验证专业模型效力，不验证真实预警准确率。

【运行】
  cd C:\\Users\\lixin\\Desktop\\地政预警项目
  .venv\\Scripts\\python.exe -m agents.e2e_pipeline_test
"""

import sys

ALL_HAZARDS = ["landslide", "rockfall", "debris_flow", "avalanche", "lake_outburst"]


def _preset_scenarios(hazard: str, sample_values: dict) -> list:
    """按灾种观测字段构造两个情景（medium/high），供 Simulation 的情景对比。"""
    scenarios = []
    for i, severity in enumerate(("medium", "high"), start=1):
        sc = {
            "scenario_id": f"SC-{i:02d}",
            "hazard_type": hazard,
            "severity_level": severity,
            "trigger": "端到端测试触发（Mock）",
            "trigger_source_tag": "assumption",
            "duration_hours": 6.0,
            "notes": "e2e 端到端测试情景",
            "test_hooks": {},
        }
        sc.update(sample_values)
        scenarios.append(sc)
    return scenarios


def run_e2e(hazard_type: str) -> dict:
    """串联运行四 Agent，返回各阶段结果摘要。"""

    from aegis.agents.hazards.data_agent.main import run_data_agent
    from aegis.agents.hazards.risk_agent.legacy import run_risk_agent_legacy
    from aegis.agents.hazards.risk_agent.tools.hazard_risk_registry import get_hazard_risk_profile
    from aegis.agents.hazards.simulation_agent.main import run_simulation_agent
    from aegis.agents.hazards.warning_agent.main import run_warning_agent

    task = f"端到端测试：{hazard_type} 灾种的数据获取、风险评估、情景推演与预警生成"

    # ---------- 1. Data Agent ----------
    data_final = run_data_agent(task, hazard_type=hazard_type)
    assert data_final["status"] in ("success", "partial"), f"[E2E][Data] {hazard_type} status={data_final['status']}"
    assert data_final["hazard_type"] == hazard_type
    assert data_final["processed_data"].get("is_mock") is True

    # ---------- 2. Risk Agent ----------
    risk_result = run_risk_agent_legacy(task, data_result=data_final, hazard_type=hazard_type)
    assert risk_result["hazard_type"] == hazard_type
    assert risk_result["status"] in ("success", "uncertain"), f"[E2E][Risk] {hazard_type} status={risk_result['status']}"
    assessment = risk_result.get("assessment", {})
    assert assessment.get("assessment_status") == "scored", (
        f"[E2E][Risk] {hazard_type} 因子引擎未评分: {assessment.get('insufficient_reasons')}"
    )
    assert assessment.get("formula", {}).get("status") == "prototype_candidate"
    assert assessment.get("R") is not None

    # ---------- 3. Simulation Agent ----------
    sample_values = get_hazard_risk_profile(hazard_type).get("sample_values", {})
    sim_result = run_simulation_agent(
        task,
        data_result=data_final,
        risk_result=risk_result,
        hazard_type=hazard_type,
        preset_scenario_configs=_preset_scenarios(hazard_type, sample_values),
    )
    assert sim_result["status"] in ("success", "uncertain"), f"[E2E][Simulation] {hazard_type} status={sim_result['status']}"
    assert sim_result["hazard_type"] == hazard_type
    assert sim_result["model_info"]["is_real_prediction"] is False

    # ---------- 4. Warning Agent ----------
    warning_result = run_warning_agent(
        task,
        hazard_type=hazard_type,
        risk_result=risk_result,
        simulation_result=sim_result,
        data_result=data_final,
    )
    assert warning_result["status"] == "draft", f"[E2E][Warning] {hazard_type} status={warning_result['status']}"
    alert = warning_result["alert"]
    assert alert["hazard_type"] == hazard_type
    assert alert["mock_only"] is True and alert["alert_status"] == "draft"
    assert alert["official_grade"] is None
    assert warning_result["cap"]["status"] == "generated"
    assert "<alert>" in warning_result["cap"]["xml"]

    return {
        "hazard_type": hazard_type,
        "data_status": data_final["status"],
        "risk_status": risk_result["status"],
        "risk_level": risk_result.get("risk_level"),
        "R": assessment.get("R"),
        "simulation_status": sim_result["status"],
        "warning_status": warning_result["status"],
        "warning_level": alert.get("warning_level"),
        "alert_id": alert.get("alert_id"),
    }


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    print("=" * 60)
    print("端到端管线测试：Data → Risk → Simulation → Warning（五灾种）")
    print("范围：流程运行 + Mock 公式预设行为；不含真实预警准确率验证")
    print("=" * 60)

    results = []
    for key in ALL_HAZARDS:
        print(f"\n{'#' * 60}\n# 灾种：{key}\n{'#' * 60}")
        results.append(run_e2e(key))

    print("\n" + "=" * 60)
    print("端到端结果汇总")
    print("=" * 60)
    for r in results:
        print(
            f"  {r['hazard_type']:<26} data={r['data_status']:<8} "
            f"risk={r['risk_status']:<9} level={r['risk_level']:<7} R={r['R']:<6} "
            f"sim={r['simulation_status']:<9} warning={r['warning_status']:<7} "
            f"level={r['warning_level']}"
        )

    print("\n端到端测试全部通过 ✅")
