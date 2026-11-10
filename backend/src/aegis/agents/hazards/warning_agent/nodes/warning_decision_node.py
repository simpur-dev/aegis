"""
WarningDecision 节点（预警条件判定）

【本文件角色】
  依据 Risk / Simulation / Data 的结构化结果与数据质量，判定是否具备生成预警建议的条件。
  纯确定性检查，【LLM 不参与任何判定】。
  任一硬条件不满足 → can_warn=False，直送输出节点（status=insufficient_data）。

【检查项】
  1. hazard_type 合法且与 risk_result.hazard_type 一致（防跨灾种误报）；
  2. risk_result.status ∈ {success, uncertain}；
  3. Risk 因子引擎 assessment_status == "scored"（数据门控已在 Risk 侧通过）；
  4. data_result.data_sufficiency.sufficient 非 False（提供了 data_result 时才检查）；
  5. simulation_result 若提供且 status=failed → 不具备条件（模拟失败不能支撑预警）。

【对外契约】
  读取：hazard_type / risk_result / simulation_result / data_result
  写入：hazard_type（归一化写回）/ decision_result / error_message / next
"""

import logging
from typing import Any

from aegis.agents.hazards.hazard_registry import normalize_hazard_type
from aegis.agents.hazards.warning_agent.state import WarningAgentState

log = logging.getLogger("aegis.agents.hazards.warning_agent.nodes.warning_decision_node")


def warning_decision_node(state: WarningAgentState) -> dict[str, Any]:
    """预警条件判定节点（确定性）。"""

    hazard_raw = state.get("hazard_type", "") or ""
    risk_result = state.get("risk_result", {}) or {}
    simulation_result = state.get("simulation_result", {}) or {}
    data_result = state.get("data_result", {}) or {}

    checks = {}
    reasons = []
    can_warn = True

    # ---------- 1) 灾种 ----------
    hazard_key = normalize_hazard_type(hazard_raw)
    checks["hazard_type"] = {"value": hazard_key, "ok": bool(hazard_key)}
    if not hazard_key:
        can_warn = False
        reasons.append(f"灾种'{hazard_raw}'未提供或无法识别")

    # ---------- 2) 灾种一致性（Risk 结果必须属于同一灾种，防跨灾种误报） ----------
    risk_hazard = risk_result.get("hazard_type", "")
    if hazard_key and risk_result and risk_hazard and risk_hazard != hazard_key:
        can_warn = False
        reasons.append(f"灾种不一致：请求 {hazard_key}，Risk 结果为 {risk_hazard}")
    checks["hazard_consistency"] = {"value": risk_hazard, "ok": risk_hazard == hazard_key}

    # ---------- 3) Risk 状态 ----------
    risk_status = risk_result.get("status", "")
    checks["risk_status"] = {"value": risk_status, "ok": risk_status in ("success", "uncertain")}
    if risk_status not in ("success", "uncertain"):
        can_warn = False
        reasons.append(f"Risk 结果状态为 {risk_status or '(空)'}，不足以支撑预警")

    # ---------- 4) 因子引擎评分状态 ----------
    assessment = risk_result.get("assessment", {}) or {}
    assess_status = assessment.get("assessment_status", "")
    checks["risk_assessment_status"] = {"value": assess_status, "ok": assess_status == "scored"}
    if assess_status != "scored":
        can_warn = False
        reasons.append(f"Risk 因子引擎状态为 {assess_status or '(空)'}（数据门控未通过或未运行）")

    # ---------- 5) 数据充分性（提供了 data_result 时检查） ----------
    # 分层原则：Risk 因子引擎已做细粒度覆盖率门控（scored 才会到这里），
    # 因此 Data 的"部分可用"（partial）不在此硬性拒绝，转为质量提示写入依据链；
    # 仅当 Data 完全失败（status=failed，即无任何数据）时拒绝。
    if data_result:
        suff = (data_result.get("data_sufficiency", {}) or {}).get("sufficient")
        data_status = data_result.get("status", "")
        risk_scored = assess_status == "scored"
        ok = (suff is not False) or (risk_scored and data_status != "failed")
        checks["data_sufficiency"] = {
            "value": suff,
            "data_status": data_status,
            "ok": ok,
            "note": ("Data 判定数据部分可用；Risk 因子引擎已按覆盖率门控通过" if suff is False and ok else ""),
        }
        if not ok:
            can_warn = False
            reasons.append("Data Agent 判定观测数据不足以支撑风险评估")

    # ---------- 6) Simulation（可选；提供且失败则不具备条件） ----------
    if simulation_result:
        sim_status = simulation_result.get("status", "")
        checks["simulation_status"] = {"value": sim_status, "ok": sim_status in ("success", "uncertain", "partial", "")}
        if sim_status == "failed":
            can_warn = False
            reasons.append("Simulation 结果状态为 failed，情景推演未成功")
    else:
        checks["simulation_status"] = {"value": "not_provided", "ok": True}

    decision_result = {
        "can_warn": can_warn,
        "reasons": reasons,
        "checks": checks,
        "decided_by": "deterministic_rules (no LLM)",
    }

    log.info(f"[WarningDecision] can_warn={can_warn} 原因={reasons if reasons else '全部检查通过'}")

    return {
        "hazard_type": hazard_key if hazard_key else hazard_raw,
        "decision_result": decision_result,
        "error_message": "" if can_warn else "；".join(reasons),
        "next": "hazard_warning_rule" if can_warn else "warning_output",
    }
