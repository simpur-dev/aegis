"""
WarningValidator 节点（预警消息校验）

【本文件角色】
  确定性校验预警对象与解释文本：
  - 必要字段是否齐全（alert_id/等级/时间/区域/依据/建议行动）；
  - 解释文本与结构化结果是否一致（不得出现官方红/橙/黄/蓝等级；等级关键词需一致）；
  - Mock 声明是否披露（mock_only=True 时文本必须含 Mock/模拟/演练字样）；
  - 建议行动是否有依据（等级非 none 时建议行动非空）。
  校验失败 → recommendation=retry（回解释节点针对性修正，限次）或直接失败出图。

【对外契约】
  写入：validation_result / retry_count（retry 时自增）/ next
"""

import logging
from typing import Any

from aegis.agents.hazards.prompts.warning_prompt import LEVEL_KEYWORDS
from aegis.agents.hazards.warning_agent.state import WarningAgentState

log = logging.getLogger("aegis.agents.hazards.warning_agent.nodes.warning_validator_node")

# 官方分级措辞（正式等级未确认前，解释文本中禁止出现）
_FORBIDDEN_OFFICIAL_GRADES = ["红色预警", "橙色预警", "黄色预警", "蓝色预警", "一级预警", "二级预警", "三级预警", "四级预警"]

_MOCK_DISCLOSURE_KEYWORDS = ["Mock", "mock", "模拟", "演练", "原型"]


def warning_validator_node(state: WarningAgentState) -> dict[str, Any]:
    """预警消息校验节点（确定性）。"""

    alert = state.get("alert_object", {}) or {}
    explanation = state.get("explanation", {}) or {}
    retry_count = state.get("retry_count", 0)
    max_retries = state.get("max_retries", 3)
    text = explanation.get("text", "") or ""

    issues = []

    # ---------- 1) 必要字段 ----------
    for field in ("alert_id", "hazard_type", "warning_level", "generated_at", "target_region", "sender"):
        if not alert.get(field):
            issues.append(f"缺少必要字段: {field}")

    # ---------- 2) Mock 标记（第一版恒为 Mock） ----------
    if not alert.get("mock_only"):
        issues.append("mock_only 必须为 True（系统尚无真实预警能力）")

    # ---------- 3) 官方等级禁用 ----------
    for word in _FORBIDDEN_OFFICIAL_GRADES:
        if word in text:
            issues.append(f"解释文本出现官方等级措辞'{word}'（正式分级未确认，禁止使用）")

    # ---------- 4) 等级关键词一致性 ----------
    level = alert.get("warning_level", "none")
    keyword = LEVEL_KEYWORDS.get(level, "")
    if keyword and keyword not in text:
        issues.append(f"解释文本未体现等级关键词'{keyword}'（level={level}）")

    # ---------- 5) Mock 披露 ----------
    if alert.get("mock_only") and not any(k in text for k in _MOCK_DISCLOSURE_KEYWORDS):
        issues.append("解释文本未披露 Mock/模拟性质")

    # ---------- 6) 建议行动 ----------
    if level != "none" and not alert.get("suggested_actions"):
        issues.append(f"等级 {level} 但建议行动为空")

    # ---------- 7) 不确定性披露（cautions 非空） ----------
    if not explanation.get("cautions"):
        issues.append("缺少不确定性/Mock 声明（cautions 为空）")

    is_valid = not issues
    if is_valid:
        recommendation = "proceed"
    elif retry_count < max_retries:
        recommendation = "retry"
    else:
        recommendation = "fail"  # 超过重试预算仍不合规 → validation_failed 出图

    new_retry_count = retry_count + 1 if recommendation == "retry" else retry_count

    validation_result = {
        "is_valid": is_valid,
        "issues": issues,
        "recommendation": recommendation,
        "checks": {
            "required_fields": True,
            "official_grades_forbidden": True,
            "level_keywords": True,
            "mock_disclosure": True,
            "suggested_actions": True,
        },
    }

    log.info(f"[WarningValidator] is_valid={is_valid} recommendation={recommendation} 问题={issues}")

    return {
        "validation_result": validation_result,
        "retry_count": new_retry_count,  # >>> ★ retry 判定与自增原子绑定
        "next": ("llm_explanation" if recommendation == "retry" else "warning_output"),
    }
