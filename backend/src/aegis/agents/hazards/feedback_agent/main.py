"""
Feedback Agent 入口 / 独立测试（面向西藏五类山地地质灾害）

【定位】质量检查、问题追溯与经验积累模块：
  收集 Data / Risk / Simulation / Warning 的最终交付物 → 确定性检查 →
  分类评估 → （有界）反思 → 改进建议 → 经验存档 → 结构化反馈输出。
  【不重新计算风险、不重新决定预警等级、不修改任何上游结果】。

【与 Manager 的交互契约】
  run_feedback_agent(...) 返回的 dict = feedback_result：
    status               no_issues / issues_found / needs_review /
                         insufficient_evidence / failed
    feedback_items       问题明细（分类/所属Agent/证据/置信度/是否需人工复核）
    improvement_suggestions  具体到组件的改进建议（含 requires_human_review）
    manager_actions      给 Manager 的行动建议（rerun_suggested 等，决定权在 Manager）
    safety_declarations  安全声明（自证未修改上游结果/分数/等级/阈值）
    memory               经验存档结果（JSON 路径与记录号）

【运行方式】
  cd C:\\Users\\lixin\\Desktop\\地政预警项目
  .venv\\Scripts\\python.exe -m agents.feedback_agent.main
"""

import sys

from aegis.agents.hazards.feedback_agent.graph import create_feedback_agent
from aegis.agents.hazards.feedback_agent.state import FeedbackAgentState


def run_feedback_agent(
    task: str,
    hazard_type: str,
    data_result: dict,
    risk_result: dict,
    simulation_result: dict | None = None,
    warning_result: dict | None = None,
    event_id: str = "",
    external_feedback=None,
    max_iterations: int = 2,
    test_hooks: dict | None = None,
) -> dict:
    """运行 Feedback Agent，返回结构化反馈结果（feedback_result）。

    参数:
        task              任务描述
        hazard_type       ★ 必填 ★ 灾种（标准键或明确中文名；五类之外一律拒绝）
        data_result       Data Agent 最终平铺 State（只读）
        risk_result       Risk Agent 最终 risk_result（只读）
        simulation_result 可选；Simulation Agent 最终 simulation_result（只读）
        warning_result    可选；Warning Agent 最终 warning_result（只读）
        event_id          事件业务标识（缺省自动生成）
        external_feedback 可选；外部实际反馈记录（接口见 feedback_memory_tool）
        max_iterations    反思最大轮数（防无限循环），默认 2
    """
    agent = create_feedback_agent()

    initial_state: FeedbackAgentState = {
        "task": task,
        "event_id": event_id,
        "hazard_type": hazard_type,
        "data_result": data_result or {},
        "risk_result": risk_result or {},
        "simulation_result": simulation_result or {},
        "warning_result": warning_result or {},
        "external_feedback": external_feedback or {},
        "collected_context": {},
        "detected_issues": [],
        "analysis_notes": {},
        "feedback_items": [],
        "evaluation_result": {},
        "reflection_result": {},
        "improvement_suggestions": [],
        "suggestion_used_llm": False,
        "historical_feedback": [],
        "needs_another_round": False,
        "memory_result": {},
        "iteration_count": 0,
        "max_iterations": max_iterations,
        "next": "",
        "error_message": "",
        "status": "",
        "feedback_result": {},
        "test_hooks": test_hooks or {},
    }

    print("=" * 60)
    print(f"[Feedback Agent] 任务: {task}")
    print(f"[Feedback Agent] 灾种: {hazard_type} | 事件: {event_id or '(自动生成)'}")
    print("=" * 60)

    final_state = agent.invoke(initial_state)
    return final_state.get("feedback_result", {})


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

    from aegis.agents.hazards.feedback_agent.test_data_builder import build_consistent_pipeline_results

    # ---------- 用例1：泥石流正常链路 → 无虚假问题 ----------
    blocks = build_consistent_pipeline_results("debris_flow", event_id="EVT-DEMO-1")
    fb1 = run_feedback_agent("反馈检查：波密泥石流完整链路", hazard_type="debris_flow", event_id="EVT-DEMO-1", **blocks)
    print("\n【用例1 正常链路】status =", fb1.get("status"), "| 问题数 =", fb1.get("summary", {}).get("issue_count"))

    # ---------- 用例2：未知灾种 → 明确拒绝 ----------
    fb2 = run_feedback_agent("反馈检查", hazard_type="火山泥流", data_result={}, risk_result={})
    print(
        "\n【用例2 未知灾种】status =",
        fb2.get("status"),
        "| items =",
        [i.get("description", "")[:30] for i in fb2.get("feedback_items", [])],
    )

    # ---------- 用例3：Warning 与上游 Risk 不一致 → 检出问题 ----------
    blocks3 = build_consistent_pipeline_results("landslide", event_id="EVT-DEMO-3")
    alert = blocks3["warning_result"]["alert"]
    alert["based_on"]["risk_level"] = "high（被人为改动的示例）"
    fb3 = run_feedback_agent("反馈检查：制造不一致", hazard_type="landslide", event_id="EVT-DEMO-3", **blocks3)
    print("\n【用例3 不一致注入】status =", fb3.get("status"), "| 分类 =", fb3.get("summary", {}).get("category_summary"))
    print("  建议示例 =", (fb3.get("improvement_suggestions") or [{}])[0].get("suggested_action", "")[:50], "...")
