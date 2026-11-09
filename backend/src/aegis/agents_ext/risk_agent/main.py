"""
Risk Agent 入口 / 独立测试（面向西藏五类山地地质灾害）

【为什么能独立跑】
  不依赖 Data Agent 真实运行，用 Mock 的 data_result（结构 = Data 最终 State 平铺，
  观测字段来自 hazard_risk_registry 的 sample_values）即可验证 Risk 全流程。
  接入 Manager 时只需把真 Data 输出塞进 data_result。

【灾种显式必填】
  hazard_type 必须显式传入（标准键或明确中文名），
  缺失/无法识别 → status=insufficient_data 并给出明确原因，绝不让 LLM 猜测灾种。

【诚实声明】
  五灾种风险规则全部为 MOCK 玩具规则（MOCK-v0.1），
  输出不是真实地质灾害风险预测。

【对外契约】
  run_risk_agent() 返回的 dict = Risk 最终 risk_result
    # 给 Simulation / 给测试断言

【运行方式】
  cd C:\\Users\\lixin\\Desktop\\地政预警项目
  .venv\\Scripts\\python.exe -m agents.risk_agent.main
"""

import json
import sys

from aegis.agents_ext.risk_agent.graph import create_risk_agent
from aegis.agents_ext.risk_agent.state import RiskAgentState
from aegis.agents_ext.risk_agent.tools.hazard_risk_registry import get_hazard_risk_profile


def run_risk_agent(task: str, data_result: dict, hazard_type: str, max_retries: int = 3) -> dict:
    """运行 Risk Agent，返回结构化风险结果（risk_result）。"""
    agent = create_risk_agent()

    initial_state: RiskAgentState = {
        "task": task,
        "hazard_type": hazard_type,  # ★ 必填 ★ 灾种
        "data_result": data_result,  # >>> 交接自 Data（Manager 填；测试用 Mock）
        "risk_factors": [],
        "calculation_result": {},
        "risk_result": {},
        "evidence": [],
        "uncertainties": [],
        "validation_result": {},
        "retry_count": 0,
        "max_retries": max_retries,
        "next": "",
        "error_message": "",
        "status": "",
    }

    print("=" * 60)
    print(f"[Risk Agent] 任务: {task}")
    print(f"[Risk Agent] 灾种: {hazard_type}")
    print(f"[Risk Agent] 输入观测记录数: {len(data_result.get('processed_data', {}).get('events', []))}")
    print("=" * 60)

    final_state = agent.invoke(initial_state)
    return final_state.get("risk_result", {})


# ============================================================
# Mock 构造：严格对齐 Data 最终 State 的【平铺】结构
# 观测字段取值来自 hazard_risk_registry 的 sample_values（与 data_agent 的
# 滑坡等 Mock 数据口径一致），保证字段名不漂移。
# ============================================================


def _mock_data_result(hazard_type: str = "debris_flow", complete: bool = True) -> dict:
    """构造某灾种的 Mock data_result（平铺结构）。

    complete=True  → 2 条必需字段齐全的观测记录（正常评估路径）
    complete=False → 2 条必需观测字段全部缺失的记录（数据不足路径）
    """
    profile = get_hazard_risk_profile(hazard_type)
    sample = profile.get("sample_values", {})

    events = []
    for i in range(1, 3):
        ev = {
            "event_id": f"MOCK-{profile['hazard_type_cn']}-{i:03d}",
            "time": "2024-07-08T03:00:00Z",
            "latitude": 29.6 + i * 0.01,
            "longitude": 95.3 + i * 0.01,
            "region": "西藏",
            "source": "mock",
        }
        for field, value in sample.items():
            ev[field] = value if complete else None  # 数据不足时置空必需观测字段
        events.append(ev)

    return {
        "processed_data": {
            "events": events,
            "data_source": "mock",
            "hazard_type": profile["hazard_type"],
            "is_mock": True,
            "clean_report": {},
            "normalization_report": {},
        },
        "data_quality": {"is_valid": True, "completeness_ratio": 1.0, "validation_reason": "Mock 合格"},
        "missing_data": [],
        "status": "success",
        "error_message": "",
        "hazard_type": profile["hazard_type"],
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

    # ---------- 用例1：泥石流正常评估 ----------
    r1 = run_risk_agent("评估西藏波密地区泥石流危险性", _mock_data_result("debris_flow"), hazard_type="debris_flow")
    print("\n【用例1 泥石流正常】\n", json.dumps(r1, ensure_ascii=False, indent=2, default=str))

    # ---------- 用例2：中文别名（"泥石流" → debris_flow） ----------
    r2 = run_risk_agent("评估泥石流风险（中文灾种名）", _mock_data_result("debris_flow"), hazard_type="泥石流")
    print("\n【用例2 中文别名】hazard_type =", r2.get("hazard_type"), "| risk_level =", r2.get("risk_level"))

    # ---------- 用例3：未知灾种 → insufficient_data + 明确原因 ----------
    r3 = run_risk_agent("评估火山泥流", _mock_data_result("debris_flow"), hazard_type="火山泥流")
    print("\n【用例3 未知灾种】status =", r3.get("status"), "| 原因 =", r3.get("missing_info"))

    # ---------- 用例4：必需观测字段缺失 → 数据不足，不强制给等级 ----------
    r4 = run_risk_agent("评估滑坡风险（缺必需观测字段）", _mock_data_result("landslide", complete=False), hazard_type="landslide")
    print("\n【用例4 数据不足】status =", r4.get("status"), "| risk_level =", r4.get("risk_level"))

    # ---------- 用例5：冰湖溃决正常评估 ----------
    r5 = run_risk_agent("评估西藏定结冰湖溃决危险性", _mock_data_result("glacial_lake_outburst"), hazard_type="glacial_lake_outburst")
    print(
        "\n【用例5 冰湖溃决】hazard_type =",
        r5.get("hazard_type"),
        "| risk_level =",
        r5.get("risk_level"),
        "| 规则版本 =",
        r5.get("model_info", {}).get("rule_version"),
    )
