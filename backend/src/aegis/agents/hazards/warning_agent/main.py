"""
Warning Agent 入口 / 独立测试（面向西藏五类山地地质灾害）

【灾种显式必填】hazard_type 必须显式传入，且与 risk_result.hazard_type 一致。

【诚实声明】
  第一版只生成与校验预警草稿（alert_status=draft, mock_only=True），
  不执行真实发布；等级映射为 MOCK-v0.1 测试规则。

【对外契约】
  run_warning_agent() 返回的 dict = Warning 最终 warning_result
    # 给 Manager / 通知模块（预留）/ Feedback Agent 读

【运行方式】
  cd C:\\Users\\lixin\\Desktop\\地政预警项目
  .venv\\Scripts\\python.exe -m agents.warning_agent.main
"""

import logging
import sys

from aegis.agents.hazards.warning_agent.graph import create_warning_agent
from aegis.agents.hazards.warning_agent.state import WarningAgentState

log = logging.getLogger("aegis.agents.hazards.warning_agent.main")


def run_warning_agent(
    task: str,
    hazard_type: str,
    risk_result: dict,
    simulation_result: dict | None = None,
    data_result: dict | None = None,
    max_retries: int = 3,
) -> dict:
    """运行 Warning Agent，返回结构化预警结果（warning_result）。"""
    agent = create_warning_agent()

    initial_state: WarningAgentState = {
        "task": task,
        "hazard_type": hazard_type,
        "risk_result": risk_result or {},
        "simulation_result": simulation_result or {},
        "data_result": data_result or {},
        "decision_result": {},
        "warning_rule_result": {},
        "alert_object": {},
        "explanation": {},
        "validation_result": {},
        "retry_count": 0,
        "max_retries": max_retries,
        "next": "",
        "error_message": "",
        "status": "",
        "warning_result": {},
    }

    log.info("=" * 60)
    log.info(f"[Warning Agent] 任务: {task}")
    log.info(f"[Warning Agent] 灾种: {hazard_type}")
    log.info("=" * 60)

    final_state = agent.invoke(initial_state)
    return final_state.get("warning_result", {})


def _mock_risk_result(hazard_type: str = "debris_flow"):
    """**Risk Agent 的 Mock 构造函数**，用来**伪造一套格式完全和真实风险引擎输出一模一样的风险评估返回结构体**，"""
    """构造最小 Risk 最终交付物（走真实 Risk 因子引擎，保证字段契约一致）。"""
    from aegis.agents.hazards.risk_agent.tools.hazard_risk_registry import get_hazard_risk_profile, sample_observations

    profile = get_hazard_risk_profile(hazard_type)
    assessment = profile["assessment_tool"](sample_observations(hazard_type), "西藏")
    return {
        "status": "success" if assessment["assessment_status"] == "scored" else "insufficient_data",
        "hazard_type": hazard_type,
        "risk_level": assessment.get("provisional_level", "unknown"),
        "reasoning": "（Mock）因子引擎评估结果，详见 assessment",
        "trigger_conditions": [f"{h['rule_id']}: {h['description']}" for h in assessment.get("rule_hits", [])],
        "assessment": assessment,
        "evidence": [],
        "uncertainties": ["（Mock）评估结果"],
        "validation": {"is_valid": True, "issues": [], "recommendation": "proceed"},
        "model_info": {"rule_version": assessment.get("rule_version", "MOCK-v0.1"), "implementation_status": "mock", "is_mock": True},
        "metadata": {"note": "Mock 风险评估结果"},
    }


if __name__ == "__main__":
    # Windows 控制台中文输出保障
    # 【并入主线时的改动】`sys.stdout` 的静态类型是 TextIO，而 `reconfigure` 是 TextIOWrapper 的方法；
    # 直接调用过不了 mypy。用 getattr 取、取不到就跳过——这正是"没有这个方法时也不该炸"的语义。
    _reconfigure = getattr(sys.stdout, "reconfigure", None)
    if _reconfigure is not None:
        try:
            _reconfigure(encoding="utf-8")
        except Exception:
            pass

    # ---------- 用例1：泥石流正常预警（draft） ----------
    w1 = run_warning_agent("生成波密地区泥石流预警（Mock）", hazard_type="debris_flow", risk_result=_mock_risk_result("debris_flow"))
    print(
        "\n【用例1 泥石流】status =",
        w1.get("status"),
        "| level =",
        (w1.get("alert") or {}).get("warning_level"),
        "| alert_id =",
        (w1.get("alert") or {}).get("alert_id"),
    )

    # ---------- 用例2：中文别名 ----------
    w2 = run_warning_agent("生成泥石流预警（中文灾种名）", hazard_type="泥石流", risk_result=_mock_risk_result("debris_flow"))
    print("\n【用例2 中文别名】status =", w2.get("status"), "| hazard_type =", w2.get("hazard_type"))

    # ---------- 用例3：未知灾种 → insufficient_data ----------
    """
    测试目标：**灾种本身不在支持列表，直接拒绝预警**
"""
    w3 = run_warning_agent("生成预警", hazard_type="火山泥流", risk_result=_mock_risk_result("debris_flow"))
    print("\n【用例3 未知灾种】status =", w3.get("status"), "| reasons =", w3.get("reasons"))

    # ---------- 用例4：灾种不一致 → insufficient_data ----------
    w4 = run_warning_agent("生成滑坡预警（但 Risk 结果是泥石流）", hazard_type="landslide", risk_result=_mock_risk_result("debris_flow"))
    print("\n【用例4 灾种不一致】status =", w4.get("status"), "| reasons =", w4.get("reasons"))
