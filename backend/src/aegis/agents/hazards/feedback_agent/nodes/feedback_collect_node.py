"""
FeedbackCollect 节点（上游结果收集）

【本文件角色】
  收集 event_id / hazard_type / 四个上游 Agent 交付物与各 Agent 的
  模型版本、规则版本、时间戳、运行状态，形成只读快照 collected_context。
  【只读取，绝不修改原始结果】——本节点对所有上游 dict 仅做取值与摘要。

【对外契约】
  读取：state 输入字段（data/risk/simulation/warning_result 等）
  写入：collected_context / next
"""

import logging
import uuid
from typing import Any

from aegis.agents.hazards.feedback_agent.state import FeedbackAgentState
from aegis.agents.hazards.hazard_registry import normalize_hazard_type

log = logging.getLogger("aegis.agents.hazards.feedback_agent.nodes.feedback_collect_node")


def _digest_data(data_result: dict) -> dict:
    processed = (data_result or {}).get("processed_data", {}) or {}
    events = processed.get("events", []) or []
    times = [e.get("time") for e in events if e.get("time")]
    return {
        "present": bool(data_result),
        "status": (data_result or {}).get("status", ""),
        "hazard_type": data_result.get("hazard_type", "") if data_result else "",
        "is_mock": processed.get("is_mock"),
        "data_source": processed.get("data_source", "unknown"),
        "event_count": len(events),
        "latest_observation_time": max(times) if times else None,
        "data_quality_valid": ((data_result or {}).get("data_quality", {}) or {}).get("is_valid"),
        "sufficient": ((data_result or {}).get("data_sufficiency", {}) or {}).get("sufficient"),
        "missing_data_count": len((data_result or {}).get("missing_data", []) or []),
    }


def _digest_risk(risk_result: dict) -> dict:
    risk_result = risk_result or {}
    model_info = risk_result.get("model_info", {}) or {}
    assessment = risk_result.get("assessment", {}) or {}
    return {
        "present": bool(risk_result),
        "status": risk_result.get("status", ""),
        "hazard_type": risk_result.get("hazard_type", ""),
        "risk_level": risk_result.get("risk_level", ""),
        "R": assessment.get("R"),
        "engine_version": model_info.get("engine_version", "") or assessment.get("engine_version", ""),
        "rule_version": model_info.get("rule_version", ""),
        "is_mock": model_info.get("is_mock"),
        "evidence_count": len(risk_result.get("evidence", []) or []),
    }


def _digest_simulation(simulation_result: dict) -> dict:
    simulation_result = simulation_result or {}
    model_info = simulation_result.get("model_info", {}) or {}
    return {
        "present": bool(simulation_result),
        "status": simulation_result.get("status", ""),
        "hazard_type": simulation_result.get("hazard_type", ""),
        "model_version": model_info.get("model_version", ""),
        "is_real_prediction": model_info.get("is_real_prediction"),
        "scenario_count": len(simulation_result.get("simulation_results", []) or []),
    }


def _digest_warning(warning_result: dict) -> dict:
    warning_result = warning_result or {}
    alert = warning_result.get("alert", {}) or {}
    return {
        "present": bool(warning_result),
        "status": warning_result.get("status", ""),
        "hazard_type": warning_result.get("hazard_type", ""),
        "alert_id": alert.get("alert_id", ""),
        "warning_level": alert.get("warning_level", ""),
        "alert_status": alert.get("alert_status", ""),
        "mock_only": alert.get("mock_only"),
        "mapping_version": alert.get("mapping_version", ""),
        "generated_at": alert.get("generated_at", ""),
        "cap_status": (warning_result.get("cap", {}) or {}).get("status", ""),
    }


def feedback_collect_node(state: FeedbackAgentState) -> dict[str, Any]:
    """反馈收集节点：组装上游交付物快照（只读）。"""
    hazard_raw = state.get("hazard_type", "")
    hazard_key = normalize_hazard_type(hazard_raw)
    event_id = state.get("event_id", "") or f"EVT-{uuid.uuid4().hex[:8]}"

    data_result = state.get("data_result", {}) or {}
    risk_result = state.get("risk_result", {}) or {}
    simulation_result = state.get("simulation_result", {}) or {}
    warning_result = state.get("warning_result", {}) or {}

    collected_context = {
        "event_id": event_id,
        "hazard_type_input_raw": hazard_raw,
        "hazard_type_normalized": hazard_key,
        "task": state.get("task", ""),
        "agents": {
            "data": _digest_data(data_result),
            "risk": _digest_risk(risk_result),
            "simulation": _digest_simulation(simulation_result),
            "warning": _digest_warning(warning_result),
        },
        "external_feedback_provided": bool(state.get("external_feedback")),
        "collected_at_note": "本快照为只读摘要，未对任何上游结果做修改",
    }

    log.info(
        f"[FeedbackCollect] event_id={event_id} hazard={hazard_key or hazard_raw!r} "
        f"data={collected_context['agents']['data']['present']} "
        f"risk={collected_context['agents']['risk']['present']} "
        f"sim={collected_context['agents']['simulation']['present']} "
        f"warning={collected_context['agents']['warning']['present']}"
    )

    return {
        "event_id": event_id,  # 补全缺省事件号（仅写 Feedback 自己的 State）
        "collected_context": collected_context,
        "next": "feedback_analyze",
    }
