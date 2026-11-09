"""
HazardWarningRule 节点（灾种预警等级映射）

【本文件角色】
  按【显式 hazard_type】从 hazard_warning_rule_registry 取该灾种独立的映射规则，
  把 Risk Agent 的结构化结果（risk_level / 组合指数 R / 保护规则命中）映射为内部
  warning_level。纯规则执行，【LLM 不参与】。

【映射语义（MOCK-v0.1，全部为测试假设）】
  1. 保护规则命中 level_suggest=high → 直接 "warning"（升级通道，不被指数稀释）；
  2. risk_level=high 或 R ≥ r_warning → "warning"；
  3. risk_level=medium 或 R ≥ r_watch → "watch"；
  4. risk_level=low 或 0 < R < r_watch → "advisory"；
  5. 其余 → "none"。
  不输出红/橙/黄/蓝官方等级（official_grade 恒为 pending_review）。
"""

from typing import Any

from aegis.agents_ext.hazard_registry import UnsupportedHazardError, normalize_hazard_type
from aegis.agents_ext.warning_agent.state import WarningAgentState
from aegis.agents_ext.warning_agent.tools.hazard_warning_rule_registry import get_hazard_warning_rules


def hazard_warning_rule_node(state: WarningAgentState) -> dict[str, Any]:
    """灾种预警等级映射节点（确定性规则执行）。"""

    hazard_key = normalize_hazard_type(state.get("hazard_type", ""))
    risk_result = state.get("risk_result", {}) or {}

    try:
        rules = get_hazard_warning_rules(hazard_key)
    except UnsupportedHazardError as e:
        # 防御分支：decision 节点已保证灾种合法
        return {
            "warning_rule_result": {
                "warning_level": "none",
                "level_basis": "mock_mapping_v0.1",
                "mapping_version": "",
                "hits": [],
                "escalation_applied": False,
                "params_used": {},
                "error": str(e),
            },
            "error_message": str(e),
            "next": "warning_output",
        }
    #     提取
    #     Risk
    #     里面的核心字段-
    #     `params`：当前灾种阈值 `{"r_warning":65, "r_watch":40, "escalate_rule_hits":["high"],...}`
    # - `risk_level`：Risk Agent 输出的粗分级：`high / medium / low / unknown`
    # - `R`：综合风险分数（0~100）
    # - `rule_hits`：**保护规则命中列表**，列表里面每一条是字典，包含 `rule_id`、`level_suggest`
    params = rules["params"]
    # `assessment` 是 **Risk Agent 输出里的一个子字典**，是风险评估结果包，所有算出来的风险核心指标都放在它里面。
    assessment = risk_result.get("assessment", {}) or {}
    risk_level = risk_result.get("risk_level", "unknown")
    R = assessment.get("R")
    rule_hits = assessment.get("rule_hits", []) or []

    escalation_applied = False
    warning_level = "none"

    # ---------- 1) 保护规则升级通道（命中即升级，不被综合指数稀释） ----------
    # `escalate_rule_hits`
    # 在注册表里面固定是 `["high"]`
    # 含义：筛选所有命中规则里，`level_suggest = "high"
    # ` 的条目。
    # 只要有任意一条命中： 直接赋值
    # `warning_level = "warning"`，并且打上标记
    # `escalation_applied = True
    #  ** 后面R分数、risk_level统统不再判断！ **
    high_hits = [h for h in rule_hits if h.get("level_suggest") in params.get("escalate_rule_hits", [])]
    if high_hits:
        warning_level = "warning"
        escalation_applied = True

    # ---------- 2) 指数/等级阈值映射（首个命中生效） ----------
    if warning_level != "warning":
        if risk_level == "high" or (R is not None and params["r_warning"] <= R):
            warning_level = "warning"
        elif risk_level == "medium" or (R is not None and params["r_watch"] <= R):
            warning_level = "watch"
        elif risk_level == "low" or (R is not None and R > 0):
            warning_level = "advisory"
        else:
            warning_level = "none"

    warning_rule_result = {
        "hazard_type": hazard_key,
        "warning_level": warning_level,
        "level_basis": "mock_mapping_v0.1",
        "mapping_version": rules["mapping_version"],
        "escalation_applied": escalation_applied,
        "high_rule_hits": [h["rule_id"] for h in high_hits],
        "hits": rule_hits,
        "params_used": dict(params),
        "risk_level": risk_level,
        "R": R,
        "disclaimer": rules["disclaimer"],
    }

    print(f"[HazardWarningRule] {hazard_key} risk_level={risk_level} R={R} → warning_level={warning_level}（升级={escalation_applied}）")

    return {
        "warning_rule_result": warning_rule_result,
        "next": "alert_builder",
    }
