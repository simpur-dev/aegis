"""
result_check_node：对刚完成的专业 Agent 交付物做结构化核验（全确定性）

【核验项（第六节调度规则）】
  1. 交付物存在且为 dict、有 status；
  2. 灾种一致性：输出灾种 ≠ 任务灾种 → HAZARD_TYPE_MISMATCH（拒绝下游）；
  3. 结构完整性：成功交付物必须含下游消费的关键键 → INVALID_AGENT_OUTPUT；
  4. 数据侧：partial+不足够 → MISSING_REQUIRED_DATA；非 Mock 过期 → STALE_DATA；
  5. Mock 红线：data.is_mock=True 但下游声称非 Mock / simulation
     is_real_prediction=True → 按致命结构错误拦截（不重试、转人工）；
  6. 业务失败状态 → 按 Agent 归类到受控错误词表（交给 retry_decision）。

【红线】只读核验：不评价业务内容对错，绝不修改风险分数/预警等级/权重阈值；
  预警等级的唯一决定来源是 Warning Agent 规则引擎。
"""

import logging
from datetime import UTC, datetime

from aegis.agents.hazards.manager_agent.state import (
    AGENT_DATA,
    AGENT_FEEDBACK,
    AGENT_RESULT_KEY,
    AGENT_RISK,
    AGENT_SIMULATION,
    AGENT_WARNING,
    ERROR_AGENT_EXECUTION,
    ERROR_FEEDBACK_REVIEW,
    ERROR_HAZARD_MISMATCH,
    ERROR_INVALID_OUTPUT,
    ERROR_MISSING_DATA,
    ERROR_SIMULATION_FAILURE,
    ERROR_STALE_DATA,
    ERROR_WARNING_VALIDATION,
    PARTIAL_STATUSES,
    SUCCESS_STATUSES,
)

log = logging.getLogger("aegis.agents.hazards.manager_agent.nodes.result_check_node")

# 各 Agent "成功交付物"必须包含的关键键（下游实际消费字段，以真实代码为准）
REQUIRED_KEYS = {
    AGENT_DATA: ["processed_data", "data_quality"],
    AGENT_RISK: ["assessment"],
    AGENT_SIMULATION: ["simulation_results", "model_info"],
    AGENT_WARNING: ["alert"],
    AGENT_FEEDBACK: ["feedback_items", "status"],
}

# 业务失败状态 → 错误类型（受控词表映射）
BIZ_FAILURE_ERROR_TYPE = {
    AGENT_DATA: {"failed": ERROR_AGENT_EXECUTION},
    AGENT_RISK: {"failed": ERROR_AGENT_EXECUTION, "insufficient_data": ERROR_MISSING_DATA},
    AGENT_SIMULATION: {"failed": ERROR_SIMULATION_FAILURE, "insufficient_data": ERROR_SIMULATION_FAILURE},
    AGENT_WARNING: {
        "validation_failed": ERROR_WARNING_VALIDATION,
        "insufficient_data": ERROR_MISSING_DATA,
        "failed": ERROR_AGENT_EXECUTION,
    },
    AGENT_FEEDBACK: {"failed": ERROR_FEEDBACK_REVIEW},
}


def _output_hazard(agent: str, output: dict) -> str:
    """按各 Agent 实际交付物结构提取灾种标识（顶层优先；Warning 回退 alert 内嵌）。"""
    top = str(output.get("hazard_type", "") or "")
    if top:
        return top
    if agent == AGENT_WARNING:
        return str(((output.get("alert") or {}).get("hazard_type")) or "")
    return ""


def _parse_iso(ts: str):
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except (ValueError, TypeError):
        return None


def _data_special_checks(state: dict, output: dict) -> tuple:
    """Data 交付物专属核验：充分性与时效。返回 (error_type, message) 或 ('', '')。"""
    status = str(output.get("status", ""))
    # partial：以 Data Agent 自报的充分性为准（不替它猜）
    if status == "partial":
        suff = output.get("data_sufficiency") or {}
        if not suff.get("sufficient", False):
            return (ERROR_MISSING_DATA, f"数据部分可用但自报不足以支撑评估：{suff.get('reason', '')}")
    # 时效检查：仅对非 Mock 数据（Mock 时间戳固定，检查会系统性误报——
    # 与 Data/Feedback Agent 既有约定一致）
    proc = output.get("processed_data") or {}
    if proc.get("is_mock") is False:
        from aegis.agents.hazards.config import MANAGER_DATA_MAX_AGE_HOURS

        raw_ts = proc.get("fetch_timestamp") or ((proc.get("events") or [{}])[0].get("time", ""))
        ts = _parse_iso(raw_ts)
        if ts is None:
            return (ERROR_INVALID_OUTPUT, "非 Mock 数据缺少可解析的 fetch_timestamp/time")
        age_h = (datetime.now(UTC) - ts).total_seconds() / 3600.0
        if age_h > MANAGER_DATA_MAX_AGE_HOURS:
            return (ERROR_STALE_DATA, f"数据已过期：获取于 {ts.date()}（{age_h:.0f}h 前，阈值 {MANAGER_DATA_MAX_AGE_HOURS}h）")
    return ("", "")


def _mock_redline(agent: str, state: dict, output: dict) -> tuple:
    """Mock/Real 一致性核验：冒充真实的交付物按致命错误拦截（不重试）。"""
    data_mock = ((state.get("data_result") or {}).get("processed_data") or {}).get("is_mock")
    if agent == AGENT_SIMULATION and (output.get("model_info") or {}).get("is_real_prediction") is True:
        return (True, "红线违规：Mock 环境的模拟结果声称 is_real_prediction=True")
    if agent == AGENT_RISK and data_mock is True and (output.get("model_info") or {}).get("is_mock") is False:
        return (True, "红线违规：上游为 Mock 数据，风险结果却声称非 Mock 计算")
    if agent == AGENT_WARNING and data_mock is True and (output.get("alert") or {}).get("mock_only") is False:
        return (True, "红线违规：上游为 Mock 数据，预警对象却声称非 Mock（mock_only=False）")
    return (False, "")


def result_check_node(state: dict) -> dict:
    """核验 current_agent 刚返回的交付物，产出 check_result（供确定性路由）。"""
    agent = state.get("current_agent", "")
    if not agent:
        return {"check_result": state.get("check_result") or {"verdict": "ok", "agent": ""}}

    # dispatch 阶段调用异常 → 结论已写好（带 from_dispatch_exception 标记），透传给路由
    prev = state.get("check_result") or {}
    if prev.get("agent") == agent and prev.get("verdict") == "failed" and prev.get("from_dispatch_exception"):
        return {}

    output = state.get(AGENT_RESULT_KEY[agent], {}) or {}
    updates: dict = {
        "agent_status": dict(state.get("agent_status") or {}),
        "completed_agents": list(state.get("completed_agents") or []),
        "failed_agents": list(state.get("failed_agents") or []),
        "pending_agents": list(state.get("pending_agents") or []),
        "error_history": list(state.get("error_history") or []),
    }

    status = str(output.get("status", ""))
    verdict, error_type, message, fatal = "ok", "", "", False

    # ---- 1. 空交付物 / 无 status ----
    if not output or not status:
        verdict, error_type = "failed", ERROR_INVALID_OUTPUT
        message = f"{agent} 返回缺少 status 字段或交付物为空"
    else:
        # ---- 2. 灾种一致性 ----
        out_h = _output_hazard(agent, output)
        if status in SUCCESS_STATUSES[agent] | PARTIAL_STATUSES.get(agent, set()) and out_h and out_h != state.get("hazard_type", ""):
            verdict, error_type, fatal = "failed", ERROR_HAZARD_MISMATCH, True
            message = f"{agent} 输出灾种'{out_h}'与任务灾种'{state.get('hazard_type')}'不一致"
        # ---- 3. 业务失败状态 ----
        elif status not in SUCCESS_STATUSES[agent] | PARTIAL_STATUSES.get(agent, set()):
            verdict = "failed"
            error_type = BIZ_FAILURE_ERROR_TYPE.get(agent, {}).get(status, ERROR_AGENT_EXECUTION)
            detail = (
                output.get("error_message") or output.get("unsupported_reason") or output.get("missing_info") or output.get("reasons") or ""
            )
            message = f"{agent} 状态={status}：{str(detail)[:200]}"
        else:
            # ---- 4. 成功态交付物：结构完整性（键存在性核验；
            #      如 feedback_items=[] 这类"合法空结果"属语义问题，归 Feedback Agent 判）----
            missing_keys = [k for k in REQUIRED_KEYS.get(agent, []) if k not in output or output[k] is None or output[k] == ""]
            if missing_keys:
                verdict, error_type = "failed", ERROR_INVALID_OUTPUT
                message = f"{agent} 交付物缺少关键键: {missing_keys}"
            else:
                # ---- 5. Mock 红线（致命，不重试）----
                fatal, redline_msg = _mock_redline(agent, state, output)
                if fatal:
                    verdict, error_type, message = "failed", ERROR_INVALID_OUTPUT, redline_msg
                # ---- 6. Data 专属：充分性 / 时效 ----
                elif agent == AGENT_DATA:
                    error_type, message = _data_special_checks(state, output)
                    verdict = "failed" if error_type else "ok"

    if verdict == "ok":
        if updates["pending_agents"] and updates["pending_agents"][0] == agent:
            updates["pending_agents"] = updates["pending_agents"][1:]
        if agent not in updates["completed_agents"]:
            updates["completed_agents"].append(agent)
        # 状态原值登记（不覆盖为"success"——保留各 Agent 语义）
        updates["agent_status"][agent] = status
        # 区域一致性提示（非致命）：上下文 region 与数据实际区域冲突仅记录
        _region_note(state, agent, output, updates)
    else:
        if fatal:
            # 灾种串线/红线冒充：重试无意义，直接移出队列记为失败（转人工）
            if updates["pending_agents"] and updates["pending_agents"][0] == agent:
                updates["pending_agents"] = updates["pending_agents"][1:]
            if agent not in updates["failed_agents"]:
                updates["failed_agents"].append(agent)
            updates["agent_status"][agent] = "failed"
        updates["error_history"].append(
            {
                "agent": agent,
                "error_type": error_type,
                "message": message,
                "fatal": fatal,
                "attempt": sum(1 for c in (state.get("agent_call_log") or []) if c.get("agent") == agent),
            }
        )

    updates["check_result"] = {
        "verdict": verdict,
        "agent": agent,
        "error_type": error_type,
        "message": message,
        "fatal": fatal,
        "status": status,
    }
    log.info(
        f"[Manager/Check] {agent}: status={status} → verdict={verdict}" + (f"（{error_type}: {message[:80]}）" if verdict != "ok" else "")
    )
    return updates


def _region_note(state: dict, agent: str, output: dict, updates: dict) -> None:
    """region 上下文一致性：仅记录观察，不阻断（Data 过滤能力未实现）。"""
    if agent != AGENT_DATA or not state.get("region"):
        return
    events = ((output.get("processed_data") or {}).get("events")) or []
    regions = {e.get("region") for e in events if isinstance(e, dict) and e.get("region")}
    if regions and state["region"] not in regions:
        updates["error_history"].append(
            {
                "agent": agent,
                "error_type": "REGION_CONTEXT_NOTE",
                "message": f"任务上下文区域'{state['region']}'与数据实际区域"
                f"{sorted(regions)}不一致（Data Agent 当前不支持按区域过滤，"
                f"仅作记录不阻断）",
                "fatal": False,
                "attempt": 0,
            }
        )
