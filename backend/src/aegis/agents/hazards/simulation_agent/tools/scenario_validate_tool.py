"""
Simulation Agent 情景校验工具
在情景送入仿真执行之前，做全套合法性检查，过滤掉不合格情景；区分两类错误：
【灾种层面全局错误】、【单个情景自身参数错误】，输出结构化校验报告，给 LangGraph 路由判断下一步怎么走。

【本文件角色】
  确定性情景校验编排（不调 LLM）：
  1. 基础 schema 检查：情景列表非空、元素为 dict、scenario_id 存在且唯一；
  2. 灾种支持性检查（归一化失败 → unsupported_reason）；
  3. 逐情景委托 model.validate_input（必需参数 / 范围 / severity 枚举）。

【对外契约】
  提供给：nodes/scenario_validate_node.py（其结果写 validation_result，驱动条件路由）。
  返回键名固定：valid_scenarios / invalid_details / total / valid_count /
                all_invalid / unsupported_reason，便于节点稳定读取。
"""

from aegis.agents.hazards.simulation_agent.tools.hazard_model_router import (
    UnsupportedHazardError,
    get_model,
    normalize_hazard_type,
    supported_hazards_text,
)


def validate_scenarios_tool(scenario_configs: list, hazard_type: str) -> dict:
    """校验情景配置列表。

    参数:
        scenario_configs: 情景 dict 列表（扁平结构：公共字段 + 灾种参数）
        hazard_type: 灾种原始输入（内部先归一化）

    返回:
        {
          "valid_scenarios": [校验通过的 normalized 情景 dict],
          "invalid_details": [{"scenario_id": str, "issues": [str]}],
          "total": int, "valid_count": int, "all_invalid": bool,
          "unsupported_reason": str,   # 灾种不支持时的原因，支持时为 ""
        }
    """
    # ---------- 1) 列表基础检查 ----------
    if not isinstance(scenario_configs, list) or not scenario_configs:
        return {
            "valid_scenarios": [],
            "invalid_details": [{"scenario_id": "-", "issues": ["情景列表为空或不是列表"]}],
            "total": 0,
            "valid_count": 0,
            "all_invalid": True,
            "unsupported_reason": "",
        }

    # ---------- 2) 灾种支持性 ----------
    key = normalize_hazard_type(hazard_type)
    if not key:
        return {
            "valid_scenarios": [],
            "invalid_details": [{"scenario_id": "-", "issues": [f"灾种'{hazard_type}'无法识别，当前支持：{supported_hazards_text()}"]}],
            "total": len(scenario_configs),
            "valid_count": 0,
            "all_invalid": True,
            "unsupported_reason": f"灾种'{hazard_type}'无法识别，当前支持：{supported_hazards_text()}",
        }
    try:
        model = get_model(key)
    except UnsupportedHazardError as e:
        return {
            "valid_scenarios": [],
            "invalid_details": [{"scenario_id": "-", "issues": [str(e)]}],
            "total": len(scenario_configs),
            "valid_count": 0,
            "all_invalid": True,
            "unsupported_reason": str(e),
        }

    # ---------- 3) 逐情景校验 ----------
    valid_scenarios = []
    invalid_details = []
    seen_ids = set()
    for sc in scenario_configs:
        if not isinstance(sc, dict):
            invalid_details.append({"scenario_id": "(non-dict)", "issues": ["情景必须是 dict"]})
            continue

        sid = str(sc.get("scenario_id", "") or "(missing)")
        issues = []

        # 3.1 schema：scenario_id 存在且唯一
        if not sc.get("scenario_id"):
            issues.append("缺少 scenario_id")
        elif sc["scenario_id"] in seen_ids:
            issues.append(f"scenario_id 重复: {sc['scenario_id']}")
        else:
            seen_ids.add(sc["scenario_id"])

        # 3.2 委托模型校验（必需参数 / 范围 / severity 枚举 / 结构归一化）
        vr = model.validate_input(sc)
        if not vr["is_valid"]:
            for f in vr["missing_fields"]:
                issues.append(f"缺少必需参数: {f}")
            for bad in vr["invalid_fields"]:
                issues.append(f"参数 {bad['field']} 非法（值={bad['value']}）：{bad['reason']}")

        if issues:
            invalid_details.append({"scenario_id": sid, "issues": issues})
        else:
            valid_scenarios.append(vr["normalized"])

    return {
        "valid_scenarios": valid_scenarios,
        "invalid_details": invalid_details,
        "total": len(scenario_configs),
        "valid_count": len(valid_scenarios),
        "all_invalid": not valid_scenarios,
        "unsupported_reason": "",
    }
