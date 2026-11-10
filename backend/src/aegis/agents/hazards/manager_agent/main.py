"""
Manager Agent 入口 / 独立测试（多智能体全局调度中心）

【对外契约】run_manager_agent(...) 返回最终任务报告 dict（第十二节结构）：
    task_status        completed / partially_completed / insufficient_data / failed /
                       need_clarification / unsupported_hazard / needs_human_review
    agent_status       五个 Agent 的交付物状态原语（含 provided / skipped / not_run）
    data_result 等     各 Agent 交付物（只读透传，Manager 未修改业务内容）
    final_report       结构化汇总（执行摘要 / 错误历史 / Mock 声明 / 能力限制）

【运行方式】
  cd C:\\Users\\lixin\\Desktop\\地政预警项目
  .venv\\Scripts\\python.exe -m agents.manager_agent.main            # 离线 Mock 演示
  .venv\\Scripts\\python.exe -m agents.manager_agent.main --real     # 真实五 Agent 串联（需 LLM/网络）
"""

import sys

from aegis.agents.hazards.manager_agent.graph import create_manager_graph
from aegis.agents.hazards.manager_agent.state import (
    AGENT_ORDER,
    AGENT_RESULT_KEY,
    ManagerState,
)


def run_manager_agent(
    hazard_type: str,
    task_type: str = "full_analysis",
    user_request: str = "",
    region: str = "",
    analysis_time: str = "",
    event_id: str = "",
    data_result: dict | None = None,
    risk_result: dict | None = None,
    simulation_result: dict | None = None,
    warning_result: dict | None = None,
    require_simulation: bool = False,
    preset_scenario_configs: list | None = None,
    llm_polish: bool = False,
    test_hooks: dict | None = None,
    checkpointer=None,
    thread_id: str | None = None,
) -> dict:
    """运行 Manager 全局调度，返回最终任务报告（final_report 同级的结构化结果）。

    参数:
        hazard_type        ★ 显式必填 ★（标准键或明确中文名；缺失→need_clarification）
        task_type          六类任务之一（默认 full_analysis）
        data_result 等     外部直供的上游交付物（simulation_only / warning_only /
                           feedback_only 依赖它们；Manager 只读不改）
        require_simulation 用户明确要求模拟为强制环节（覆盖 SIMULATION_OPTIONAL）
        preset_scenario_configs 直传 Simulation 的情景（确定性集成测试用）
        llm_polish           最终报告是否用 LLM 润色叙述文字（默认 False；
                             结构化事实与调度决策不受 LLM 影响，失败自动兜底）
        checkpointer         Phase 5：SQLite Checkpointer；thread_id 缺省取 event_id
    """
    agent = create_manager_graph(checkpointer=checkpointer)

    initial_state: ManagerState = {
        "event_id": event_id,
        "user_request": user_request,
        "task_type": task_type,
        "hazard_type": hazard_type,
        "region": region,
        "analysis_time": analysis_time,
        "require_simulation": bool(require_simulation),
        "llm_polish": bool(llm_polish),
        "execution_plan": [],
        "pending_agents": [],
        "current_agent": "",
        "completed_agents": [],
        "skipped_agents": [],
        "failed_agents": [],
        "data_result": data_result or {},
        "risk_result": risk_result or {},
        "simulation_result": simulation_result or {},
        "warning_result": warning_result or {},
        "feedback_result": {},
        "preset_scenario_configs": list(preset_scenario_configs or []),
        "agent_status": {},
        "agent_call_log": [],
        "check_result": {},
        "error_message": "",
        "error_history": [],
        "retry_counts": {},
        "rerun_plan": [],
        "feedback_loop_count": 0,
        "human_review_required": False,
        "next": "",
        "task_status": "",
        "final_report": {},
        "test_hooks": test_hooks or {},
    }

    config = {}
    tid = thread_id or (event_id or None)
    if checkpointer is not None:
        config["configurable"] = {"thread_id": tid or "mgr-thread"}

    final_state = agent.invoke(initial_state, config or None)
    return _build_payload(final_state)


def _build_payload(final_state: dict) -> dict:
    """第十二节输出结构（交付物 + 报告并列，供调用方/接口层直接使用）。"""
    payload = {
        "event_id": final_state.get("event_id", ""),
        "hazard_type": final_state.get("hazard_type", ""),
        "region": final_state.get("region", ""),
        "task_status": final_state.get("task_status", ""),
        "agent_status": final_state.get("agent_status", {}),
        "human_review_required": final_state.get("human_review_required", False),
        "final_report": final_state.get("final_report", {}),
        "error_message": final_state.get("error_message", ""),
    }
    for a in AGENT_ORDER:
        payload[AGENT_RESULT_KEY[a]] = final_state.get(AGENT_RESULT_KEY[a], {})
    return payload


def resume_manager_task(event_id: str, checkpointer=None, db_path: str | None = None, thread_id: str | None = None) -> dict:
    """从 SQLite Checkpoint 恢复中断任务（第十一节）。

    - thread_id 缺省取 event_id（一任务一线程，不同任务互不干扰）；
    - 恢复 = 从最后一个成功 superstep 继续：已完成且结果仍有效的 Agent
      不会被重复调用；已跑到 END 的线程直接返回终态（幂等，零调用）；
    - 恢复与 Feedback Memory 无关（两套机制物理分离，见 checkpoint.py）。
    """
    from aegis.agents.hazards.manager_agent.checkpoint import get_checkpointer

    saver = checkpointer if checkpointer is not None else get_checkpointer(db_path)
    agent = create_manager_graph(checkpointer=saver)
    tid = thread_id or event_id
    config = {"configurable": {"thread_id": tid}}

    snap = agent.get_state(config)
    if not snap.values:
        return {
            "event_id": event_id,
            "hazard_type": "",
            "region": "",
            "task_status": "failed",
            "agent_status": {},
            "human_review_required": False,
            "final_report": {},
            "error_message": f"未找到线程 {tid} 的检查点：任务不存在或未曾以 Checkpoint 模式运行（db={db_path or '默认'}）",
        }
    # invoke(None)：只补执行中断点之后的环节，不重放已完成节点
    final_state = agent.invoke(None, config)
    print(f"[Manager/Resume] 线程 {tid} 恢复执行完成 → {final_state.get('task_status', '')}")
    return _build_payload(final_state)


# ============================================================
# 演示
# ============================================================


def _mock_demo() -> None:
    """离线演示：用 Mock 子图替换五个真实 Agent（不依赖 LLM/网络）。"""
    from aegis.agents.hazards.manager_agent import dispatcher
    from aegis.agents.hazards.manager_agent.state import (
        AGENT_DATA,
        AGENT_FEEDBACK,
        AGENT_RISK,
        AGENT_SIMULATION,
        AGENT_WARNING,
    )

    def _mk(agent, status, extra=None):
        def fn(state):
            out = {"status": status, "hazard_type": state["hazard_type"]}
            out.update(extra or {})
            return out

        return fn

    dispatcher.set_runner(
        AGENT_DATA,
        _mk(
            "data",
            "success",
            {
                "processed_data": {"events": [], "is_mock": True, "data_source": "mock"},
                "data_quality": {"is_valid": True},
                "data_sufficiency": {"sufficient": True},
            },
        ),
    )
    dispatcher.set_runner(
        AGENT_RISK,
        _mk(
            "risk",
            "success",
            {"risk_level": "high", "assessment": {"R": 68.0}, "model_info": {"is_mock": True, "rule_version": "MOCK-v0.1"}},
        ),
    )
    dispatcher.set_runner(AGENT_SIMULATION, _mk("sim", "success", {"simulation_results": [], "model_info": {"is_real_prediction": False}}))

    def _warn_fn(state):
        return {
            "status": "draft",
            "hazard_type": state["hazard_type"],
            "alert": {"warning_level": "warning", "mock_only": True, "hazard_type": state["hazard_type"]},
            "cap": {"status": "generated"},
        }

    dispatcher.set_runner(AGENT_WARNING, _warn_fn)
    dispatcher.set_runner(AGENT_FEEDBACK, _mk("fb", "no_issues", {"summary": {}}))

    print("\n" + "#" * 60)
    print("# 演示 1：full_analysis（Mock 子图，正常串联）")
    print("#" * 60)
    r1 = run_manager_agent("泥石流", task_type="full_analysis", region="西藏波密", event_id="EVT-DEMO-M1")
    print("task_status =", r1["task_status"], "| agent_status =", r1["agent_status"])

    print("\n" + "#" * 60)
    print("# 演示 2：未知灾种 → unsupported_hazard")
    print("#" * 60)
    r2 = run_manager_agent("火山泥流")
    print("task_status =", r2["task_status"], "|", r2["error_message"][:60])

    print("\n" + "#" * 60)
    print("# 演示 3：warning_only 缺 Risk 依赖 → insufficient_data")
    print("#" * 60)
    r3 = run_manager_agent("滑坡", task_type="warning_only")
    print("task_status =", r3["task_status"], "|", r3["error_message"][:60])

    dispatcher.restore_runners()


def _real_demo() -> None:
    """真实五 Agent 串联演示（需 LLM 可用；LLM 失败时各 Agent 走自身兜底）。"""
    r = run_manager_agent("泥石流", task_type="full_analysis", region="西藏波密", event_id="EVT-DEMO-REAL-1")
    print("\n=== 真实串联结果 ===")
    print("task_status =", r["task_status"])
    print("agent_status =", r["agent_status"])
    print("mock_disclosure =", r["final_report"].get("mock_disclosure"))


if __name__ == "__main__":
    # 【并入主线时的改动】`sys.stdout` 的静态类型是 TextIO，而 `reconfigure` 是 TextIOWrapper 的方法；
    # 直接调用过不了 mypy。用 getattr 取、取不到就跳过——这正是"没有这个方法时也不该炸"的语义。
    _reconfigure = getattr(sys.stdout, "reconfigure", None)
    if _reconfigure is not None:
        try:
            _reconfigure(encoding="utf-8")
        except Exception:
            pass
    if "--real" in sys.argv:
        _real_demo()
    else:
        _mock_demo()
