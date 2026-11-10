"""
Simulation Agent 入口 / 独立测试

【为什么能独立跑】
  不依赖 Data / Risk Agent 真实运行：用 Mock 的 data_result / risk_result
  （结构严格对齐两 Agent 的真实最终输出）即可验证 Simulation 全流程。
  接入 Manager 时只需把真 Agent 的输出塞进对应字段。

【灾种显式必填（用户拍板）】
  hazard_type 必须显式传入（标准键或明确中文名，如 "debris_flow" / "泥石流"），
  缺失或无法识别 → status=failed 并给出明确原因，绝不用 LLM 猜测灾种。

【对外契约】
  run_simulation_agent() 返回的 dict = Simulation Agent 最终交付物
    # 给 Warning Agent 读 simulation_results / comparison / uncertainties
    # 给 Manager 读 status 判断流程
    # ★ 注意：全部结果是 MOCK 玩具模型输出，不是真实地质灾害预测

【运行方式】
  cd C:\\Users\\lixin\\Desktop\\地政预警项目
  .venv\\Scripts\\python.exe -m agents.simulation_agent.main
"""

import json
import sys

from aegis.agents.hazards.simulation_agent.graph import create_simulation_agent
from aegis.agents.hazards.simulation_agent.state import SimulationAgentState

# ============================================================
# 入口函数
# ============================================================


def run_simulation_agent(
    task: str,
    data_result: dict,
    risk_result: dict,
    hazard_type: str,
    preset_scenario_configs: list | None = None,
    max_retries: int = 3,
    timeout_seconds: float = 60.0,
) -> dict:
    """运行 Simulation Agent，返回结构化推演结果（simulation_result）。

    参数:
        task: 推演任务描述（由 Manager 传入）
        data_result: Data Agent 的最终完整 State（平铺结构）
        risk_result: Risk Agent 的最终 risk_result（risk_output_node 组装的那份）
        hazard_type: ★ 必填 ★ 灾种（标准键或明确中文名）
        preset_scenario_configs: 可选，外部直传情景（非空时跳过 LLM 情景设计）
        max_retries: 全局重试上限，默认 3
        timeout_seconds: 单情景仿真超时（秒），默认 60

    返回:
        simulation_result（最终交付物 dict，见 simulation_output_node）
    """
    agent = create_simulation_agent()

    initial_state: SimulationAgentState = {
        # === 输入 ===
        "simulation_task": task,
        "data_result": data_result or {},
        "risk_result": risk_result or {},
        "hazard_type": hazard_type,
        "preset_scenario_configs": list(preset_scenario_configs or []),
        # === 情景设计 ===
        "scenario_design_result": {},
        "scenario_configs": [],
        "assumptions": [],
        "uncertainties": [],
        # === 校验 / 路由 ===
        "validated_scenarios": [],
        "validation_result": {},
        "routing_result": {},
        # === 执行 / 分析 ===
        "simulation_results": [],
        "execution_result": {},
        "comparison_result": {},
        # === 控制流 ===
        "retry_count": 0,
        "max_retries": max_retries,
        "timeout_seconds": timeout_seconds,
        "next": "",
        "error_message": "",
        # === 输出 ===
        "status": "",
        "simulation_result": {},
    }

    print("=" * 60)
    print(f"[Simulation Agent] 任务: {task}")
    print(f"[Simulation Agent] 灾种: {hazard_type} | 直传情景数: {len(preset_scenario_configs or [])}")
    print("=" * 60)

    final_state = agent.invoke(initial_state)
    return final_state.get("simulation_result", {})


# ============================================================
# Mock 构造：严格对齐上游 Agent 的真实输出结构
# ============================================================


def _mock_data_result(events=None, status="success"):
    """Data Agent 最终 State 的平铺结构（对齐 data_agent/state.py）。

    注意：Data Agent 现已面向五类山地灾害，此处 Mock 为泥石流（debris_flow）
    口径观测数据——Simulation Agent 只把它当作"上游确实给过数据"的上下文，
    情景参数另行来自情景假设。
    """
    if events is None:
        events = [
            {
                "event_id": "DF2024001",
                "time": "2024-09-03T20:00:00Z",
                "latitude": 29.86,
                "longitude": 95.77,
                "region": "西藏波密",
                "rainfall_mm_h": 65.0,
                "catchment_area_km2": 12.0,
                "channel_gradient": 0.35,
                "sediment_supply_index": 0.7,
                "source": "mock_debris_flow",
            },
            {
                "event_id": "DF2024002",
                "time": "2024-09-04T02:00:00Z",
                "latitude": 29.9,
                "longitude": 95.8,
                "region": "西藏波密",
                "rainfall_mm_h": 28.0,
                "catchment_area_km2": 4.5,
                "channel_gradient": 0.22,
                "sediment_supply_index": 0.7,
                "source": "mock_debris_flow",
            },
        ]
    return {
        "task": "获取西藏波密地区最近24小时泥石流沟域观测数据（Mock）",
        "data_requirement": {},
        "processing_plan": "",
        "raw_data": {},
        "processed_data": {
            "events": events,
            "data_source": "mock",
            "hazard_type": "debris_flow",
            "is_mock": True,
            "cleaning_report": {},
            "normalization_applied": [],
        },
        "data_quality": {"is_valid": True, "completeness_ratio": 1.0, "validation_reason": "Mock 合格"},
        "missing_data": [],
        "data_sufficiency": {"sufficient": True, "reason": "Mock 合格"},
        "hazard_type": "debris_flow",
        "retry_count": 0,
        "next": "end",
        "error_message": "",
        "status": status,
    }


def _mock_risk_result(risk_level="high", status="success"):
    """Risk Agent 最终 risk_result（对齐 risk_output_node 组装的 final_output 结构）。"""
    return {
        "status": status,
        "hazard_type": "debris_flow",
        "risk_level": risk_level,
        "risk_factors": ["短时强降雨", "沟域物源丰富", "沟道纵比降大"],
        "reasoning": "24 小时最大降雨 65mm，沟道纵比降 0.35，沟域泥石流危险性升高（Mock 研判）",
        "calculation_result": {
            "record_count": 2,
            "complete_record_count": 2,
            "metrics": {"rainfall_mm_h": {"label": "最大小时降雨量", "value": 65.0, "unit": "mm/h"}},
            "note": "Mock 统计",
        },
        "evidence": [
            {"statement": "最大小时降雨量 65mm/h", "source": "mock_debris_flow", "type": "observation"},
            {"statement": "MOCK 规则判级 high", "source": "debris_flow_rule_tool MOCK-v0.1", "type": "derived"},
            {"statement": "无承灾体暴露度数据", "source": "Data Agent", "type": "limitation"},
        ],
        "uncertainties": ["评估为事件固有危险性，非综合灾害风险（Mock）"],
        "validation": {"is_valid": True, "issues": [], "recommendation": "proceed"},
        "metadata": {
            "retry_count": 0,
            "error_message": "",
            "data_source": "mock",
            "note": "风险等级基于 MOCK 玩具规则与 LLM 综合判断，非真实地质灾害风险预测",
        },
    }


# ============================================================
# 演示用例
# ============================================================

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

    # ---------- 用例 1：泥石流正常推演（LLM 设计；无 key 自动走确定性兜底）----------
    r1 = run_simulation_agent(
        "推演强降雨诱发的沟域泥石流情景，比较不同雨强下的危险性",
        _mock_data_result(),
        _mock_risk_result(),
        hazard_type="debris_flow",
    )
    print(
        "\n【用例1 泥石流正常】\n",
        json.dumps(
            {k: r1[k] for k in ("status", "hazard_type", "model_info", "key_findings", "missing_inputs", "unsupported_reason") if k in r1},
            ensure_ascii=False,
            indent=2,
            default=str,
        ),
    )

    # ---------- 用例 2：中文别名归一化（"泥石流" → debris_flow）----------
    r2 = run_simulation_agent(
        "推演泥石流风险（中文灾种名）",
        _mock_data_result(),
        _mock_risk_result(),
        hazard_type="泥石流",
        # 直传情景：跳过 LLM，参数自定（确定性可复现）
        preset_scenario_configs=[
            {
                "scenario_id": "SC-01",
                "severity_level": "medium",
                "trigger": "中等降雨",
                "rainfall_mm_h": 40.0,
                "catchment_area_km2": 5.0,
                "channel_gradient": 0.3,
            },
            {
                "scenario_id": "SC-02",
                "severity_level": "high",
                "trigger": "强降雨",
                "rainfall_mm_h": 90.0,
                "catchment_area_km2": 8.0,
                "channel_gradient": 0.5,
            },
        ],
    )
    print("\n【用例2 中文别名】status =", r2.get("status"), "| 归一化灾种 =", r2.get("hazard_type"))

    # ---------- 用例 3：未知灾种 → failed，明确原因，绝不擅自换模型 ----------
    r3 = run_simulation_agent(
        "推演火山泥流",
        _mock_data_result(),
        _mock_risk_result(),
        hazard_type="火山泥流",
    )
    print("\n【用例3 未知灾种】status =", r3.get("status"))
    print("  原因 =", r3.get("unsupported_reason"))

    # ---------- 用例 4：上游数据缺失 → insufficient_data + 缺失字段清单 ----------
    r4 = run_simulation_agent("推演滑坡风险", {}, {}, hazard_type="landslide")
    print("\n【用例4 数据缺失】status =", r4.get("status"))
    print("  缺失项 =", json.dumps(r4.get("missing_inputs", []), ensure_ascii=False))

    # ---------- 用例 5：直传非法参数 → 拒绝执行，返回校验结果 ----------
    r5 = run_simulation_agent(
        "推演滑坡（参数非法：坡度 120°）",
        _mock_data_result(),
        _mock_risk_result(),
        hazard_type="landslide",
        preset_scenario_configs=[
            {"scenario_id": "SC-01", "severity_level": "high", "rainfall_mm_h": 60.0, "slope_angle_deg": 120.0},  # 越界（合法 5~80）
        ],
    )
    print("\n【用例5 参数非法】status =", r5.get("status"), "| 已执行情景数 =", len(r5.get("simulation_results", [])))
    print(
        "  校验明细 =", json.dumps(r5.get("validation", {}).get("scenario_validation", {}).get("invalid_details", []), ensure_ascii=False)
    )
