"""
WarningOutput 节点（最终结构化预警输出）

【本文件角色】
  组装最终交付物 warning_result，供 Manager / 通知模块（预留）/ Feedback Agent 使用。
  【第一版只生成草稿，不执行真实发布】。

【status 语义】
  - "draft"：预警对象生成并通过校验（Mock 性质已标注）；
  - "validation_failed"：超过重试预算仍未通过校验；
  - "insufficient_data"：上游条件不足（Risk 数据门控未通过 / 灾种无效等）。
  不存在"已发布"语义。
"""

from typing import Any

from aegis.agents_ext.hazard_registry import HAZARD_CN_NAME, normalize_hazard_type
from aegis.agents_ext.warning_agent.state import WarningAgentState
from aegis.agents_ext.warning_agent.tools.cap_format_tool import (
    CAP_MAPPING_VERSION,
    internal_alert_to_cap,
    serialize_cap_xml,
    validate_cap_dict,
)


def _build_cap(alert: dict, explanation_text: str) -> dict:
    """把已校验的内部预警对象转为 CAP 1.2 消息（转换错误被捕获为明确状态）。"""
    try:
        cap = internal_alert_to_cap(alert, explanation_text)
        if cap.get("status") == "skipped":
            return cap
        issues = validate_cap_dict(cap)
        if issues:
            return {"status": "error", "validation_issues": issues}
        return {
            "status": "generated",
            "mapping_version": CAP_MAPPING_VERSION,
            "validation_issues": [],
            "xml": serialize_cap_xml(cap),
            "dict": cap,
        }
    except Exception as e:
        # ★ CAP 转换/校验异常必须被捕获为明确错误信息，不允许静默丢弃
        return {"status": "error", "issues": [f"CAP 转换异常: {e}"]}


def warning_output_node(state: WarningAgentState) -> dict[str, Any]:
    """预警输出节点：组装最终交付物。"""

    decision = state.get("decision_result", {}) or {}
    rule_result = state.get("warning_rule_result", {}) or {}
    alert = state.get("alert_object", {}) or {}
    explanation = state.get("explanation", {}) or {}
    validation = state.get("validation_result", {}) or {}
    retry_count = state.get("retry_count", 0)
    error_message = state.get("error_message", "")
    hazard_raw = state.get("hazard_type", "")
    hazard_type = normalize_hazard_type(hazard_raw) or hazard_raw

    # ---------- status 判定 ----------
    if not decision.get("can_warn", False):
        status = "insufficient_data"
    elif validation.get("is_valid"):
        status = "draft"
    else:
        status = "validation_failed"

    # ---------- 组装交付物 ----------
    if status == "insufficient_data":
        warning_result = {
            "status": status,
            "hazard_type": hazard_type,
            "hazard_type_cn": HAZARD_CN_NAME.get(hazard_type, hazard_type),
            "alert_object": None,
            "reasons": decision.get("reasons", []),
            "checks": decision.get("checks", {}),
            "cap": {"status": "skipped", "reason": "未生成预警对象，无 CAP 消息"},
            "metadata": {
                "note": "上游条件不足，未生成预警对象（Mock 体系，无真实预警能力）",
                "retry_count": retry_count,
                "error_message": error_message,
            },
        }
    else:
        warning_result = {
            "status": status,  # draft / validation_failed
            "hazard_type": hazard_type,
            "hazard_type_cn": HAZARD_CN_NAME.get(hazard_type, hazard_type),
            "alert": alert,  # ★ 结构化预警对象（生命周期字段齐全）
            "explanation": explanation,  # ★ 可读说明（含 used_llm 标记）
            "validation": validation,
            "rule_result": rule_result,
            "cap": _build_cap(alert, explanation.get("text", "")),  # ★ CAP 1.2 消息（阶段 7）
            "interfaces": {  # ★ 预留接口（第一版不实现）
                "publish": "not_implemented",  # 不执行真实发布
                "websocket": "not_implemented",
                "sms": "not_implemented",
                "email": "not_implemented",
                "update_alert": "not_implemented",
                "cancel_alert": "not_implemented",
                "duplicate_check": "not_implemented",
            },
            "metadata": {
                "retry_count": retry_count,
                "error_message": error_message,
                "note": "预警对象为 Mock 模拟产物（mapping_version=MOCK-v0.1），非真实预警，禁止用于实际发布或应急决策",
            },
        }

    print(f"[WarningOutput] status={status} hazard_type={hazard_type}")

    return {
        "warning_result": warning_result,  # >>> 最终交付物
        "status": status,  # >>> 给 Manager 判断流程
        "next": "end",
    }
