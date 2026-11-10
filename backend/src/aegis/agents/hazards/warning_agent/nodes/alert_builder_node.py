"""
AlertBuilder 节点（结构化预警对象构建）

【本文件角色】
  在调用 LLM 之前，把已确定的信息组装成结构化预警对象：
  唯一预警 ID、灾种、等级（Mock 映射）、生命周期状态、依据链、时效、
  目标区域、建议行动、去重/发送/更新等【预留接口】。
  纯确定性组装，不调用 LLM。

【生命周期（第一版）】
  生成的预警 alert_status 恒为 "draft"；
  issued / acknowledged / resolved / cancelled 仅为预留枚举，本版不会出现。
  第一版不接入任何真实通知渠道。
"""

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from aegis.agents.hazards.hazard_registry import HAZARD_CN_NAME, normalize_hazard_type
from aegis.agents.hazards.warning_agent.state import WarningAgentState
from aegis.agents.hazards.warning_agent.tools.hazard_warning_rule_registry import (
    get_hazard_warning_rules,
    get_suggested_actions,
)

log = logging.getLogger("aegis.agents.hazards.warning_agent.nodes.alert_builder_node")


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def alert_builder_node(state: WarningAgentState) -> dict[str, Any]:
    """结构化预警对象构建节点（确定性）。"""

    hazard_key = normalize_hazard_type(state.get("hazard_type", ""))
    rule_result = state.get("warning_rule_result", {}) or {}
    risk_result = state.get("risk_result", {}) or {}
    simulation_result = state.get("simulation_result", {}) or {}
    data_result = state.get("data_result", {}) or {}

    rules = get_hazard_warning_rules(hazard_key)
    params = rules["params"]
    warning_level = rule_result.get("warning_level", "none")

    now = _now_iso()
    duration = params.get("valid_duration_hours", 24)

    # ---------- 事件引用与目标区域（来自 Data 观测记录；Mock 数据亦如实标注） ----------
    processed = data_result.get("processed_data", {}) or {}
    events = processed.get("events", []) or []
    event_refs = [e.get("event_id") for e in events if e.get("event_id")]
    region = (events[0].get("region") if events else None) or "未标注区域"

    # ---------- 依据链 ----------
    assessment = risk_result.get("assessment", {}) or {}
    based_on = {
        "risk_status": risk_result.get("status", ""),
        "risk_level": risk_result.get("risk_level", "unknown"),
        "provisional_level_status": assessment.get("provisional_level_status", ""),
        "P": assessment.get("P"),
        "T": assessment.get("T"),
        "R": assessment.get("R"),
        "formula_expression": (assessment.get("formula", {}) or {}).get("expression", ""),
        "formula_status": (assessment.get("formula", {}) or {}).get("status", ""),
        "rule_hits": assessment.get("rule_hits", []),
        "completeness": assessment.get("completeness", {}),
        "data_sufficiency": data_result.get("data_sufficiency", {}),
        "data_is_mock": processed.get("is_mock", None),
        "simulation": (
            {"status": simulation_result.get("status", ""), "scenario_count": len(simulation_result.get("simulation_results", []) or [])}
            if simulation_result
            else None
        ),
    }

    alert_object = {
        # ---- 标识 ----
        "alert_id": f"ALT-{hazard_key.upper()}-{uuid.uuid4().hex[:8]}",
        "related_event_refs": event_refs,
        # ---- 灾种与等级 ----
        "hazard_type": hazard_key,
        "hazard_type_cn": HAZARD_CN_NAME.get(hazard_key, hazard_key),
        "warning_level": warning_level,
        "level_basis": rule_result.get("level_basis", "mock_mapping_v0.1"),
        "official_grade": None,  # ★ 不输出红/橙/黄/蓝官方等级
        "official_grade_status": "pending_review",
        # ---- 生命周期 ----
        "alert_status": "draft",  # ★ 第一版只生成草稿
        "mock_only": True,  # ★ 系统尚无真实预警能力
        "generated_at": now,
        "valid_from": now,
        "valid_until": (datetime.now(UTC) + timedelta(hours=duration)).isoformat(),
        "published_at": None,
        "acknowledged_at": None,
        "resolved_at": None,
        "cancelled_at": None,
        "updated_at": now,
        # ---- 空间与内容素材 ----
        "target_region": region,
        "sender": "西藏山地灾害多智能体原型系统（非官方发布机构）",
        "suggested_actions": get_suggested_actions(hazard_key, warning_level),
        # ---- 依据链 ----
        "based_on": based_on,
        "trigger_conditions": risk_result.get("trigger_conditions", []),
        # ---- 版本 ----
        "rule_version": rule_result.get("rule_version", "MOCK-v0.1"),
        "mapping_version": rule_result.get("mapping_version", "MOCK-v0.1"),
        # ---- 预留接口（第一版不实现，字段占位）----
        "dedup": {"check_status": "not_implemented"},  # 重复告警判断接口
        "cooldown": {"enabled": False, "seconds": None},  # 可配置冷却机制（预留）
        "delivery": {"channels": [], "send_status": "not_implemented"},  # WebSocket/短信/邮件预留
        "update": {"supported": False},  # 告警更新接口（预留）
        "cancel": {"supported": False},  # 告警取消接口（预留）
        # ---- CAP 预留（阶段 7 接入 cap_format_tool）----
        "cap": {"status": "not_implemented"},
    }

    log.info(f"[AlertBuilder] alert_id={alert_object['alert_id']} level={warning_level} region={alert_object['target_region']}")

    return {
        "alert_object": alert_object,
        "next": "llm_explanation",
    }
