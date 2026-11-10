"""
SimulationOutput 节点 —— 最终结构化输出

【本文件角色】
  status 级联判定 → 组装 final_output → 写入 simulation_result，
  供下游 Warning Agent / Manager 读取。不写自然语言报告，只输出结构化 dict
  （对齐 risk_output_node 风格）。

【对外契约】
  读取：全部阶段产出字段
  写入：simulation_result（★ 最终交付物）/ status / next
  图：simulation_output → END

【status 级联判定】（自上而下首个命中即止）
  1. 灾种不支持                                    → failed
  2. 数据缺失 / 模型上下文不齐备                    → insufficient_data
  3. 情景参数校验失败（recommendation == fail）     → failed
  4. 仿真执行失败（含重试耗尽）                     → failed
  5. 未进入分析环节（防御）                         → uncertain
  6. 分析结果不完整（不可修复或预算耗尽）           → uncertain
  7. 分析走了确定性兜底（llm_used == False）        → uncertain
  8. 其余                                          → success
"""

from aegis.agents.hazards.simulation_agent.tools.hazard_model_router import get_model_requirements


def simulation_output_node(state: dict) -> dict:
    """仿真输出节点：组装最终交付物。"""
    design = state.get("scenario_design_result", {}) or {}
    validation = state.get("validation_result", {}) or {}
    routing = state.get("routing_result", {}) or {}
    execution = state.get("execution_result", {}) or {}
    comparison = state.get("comparison_result", {}) or {}
    results = state.get("simulation_results", []) or []
    hazard = state.get("hazard_type", "") or ""
    hazard_raw = state.get("hazard_type", "") or ""
    data_result = state.get("data_result", {}) or {}
    risk_result = state.get("risk_result", {}) or {}

    # ---------- 1) status 级联判定 ----------
    if design.get("unsupported_hazard") or routing.get("supported") is False:
        status = "failed"  # 灾种不支持 / 无法识别
    elif design.get("needs_more_data") or routing.get("inputs_ok") is False:
        status = "insufficient_data"  # 数据缺失 / 上下文不齐备
    elif validation.get("recommendation") == "fail":
        status = "failed"  # 参数不合法，拒绝执行
    elif execution and execution.get("ok") is False:
        status = "failed"  # 仿真异常（含重试耗尽）
    elif not comparison:
        status = "uncertain"  # 防御：未进入分析环节
    elif comparison.get("completeness", {}).get("is_complete") is False:
        status = "uncertain"  # 结果不完整（不可修复 / 预算耗尽）
    elif comparison.get("llm_used") is False:
        status = "uncertain"  # 分析走了确定性兜底
    else:
        status = "success"

    # ---------- 2) 缺失字段与不支持原因（case 2 / case 4 透传）----------
    if routing.get("missing_inputs"):
        missing_inputs = list(routing["missing_inputs"])
    elif design.get("missing_info"):
        # design 的 missing_info 是 "字段: 原因" 字符串，转为结构化条目
        missing_inputs = [{"field": str(s).split(":")[0].strip(), "source": "上游 Agent", "reason": str(s)} for s in design["missing_info"]]
    else:
        missing_inputs = []
    unsupported_reason = routing.get("unsupported_reason") or (design.get("reason") if design.get("unsupported_hazard") else "")

    # ---------- 3) 模型元信息 ----------
    model_info = {
        "hazard_type": hazard,
        "hazard_type_cn": "",
        "model_version": routing.get("model_version", ""),
        "simulation_mode": "mock",
        "is_real_prediction": False,
        "disclaimer": routing.get("disclaimer", ""),
        "real_model_note": "",
    }
    try:
        req = get_model_requirements(hazard)
        model_info.update(
            {
                "hazard_type_cn": req["hazard_type_cn"],
                "model_version": req["model_version"],
                "simulation_mode": req["simulation_mode"],
                "is_real_prediction": req["is_real_prediction"],
                "disclaimer": req["disclaimer"],
                "real_model_note": req["real_model_note"],
            }
        )
    except Exception:
        pass  # 灾种不支持路径：保持 routing_result 里的空值与失败原因

    # ---------- 4) 逐情景摘要（成功 ok / 执行失败 failed / 校验跳过 skipped）----------
    scenarios_summary = []
    for r in results:
        indicators = r.get("indicators", {}) or {}
        scenarios_summary.append(
            {
                "scenario_id": r.get("scenario_id", ""),
                "severity_level": r.get("severity_level", ""),
                "trigger": r.get("trigger", ""),
                "execution_status": "ok",
                "key_indicators": dict(list(indicators.items())[:3]),
            }
        )
    for f in execution.get("failures", []) or []:
        scenarios_summary.append(
            {
                "scenario_id": f.get("scenario_id", "?"),
                "severity_level": "",
                "trigger": "",
                "execution_status": "failed",
                "key_indicators": {},
                "error": f.get("error", ""),
            }
        )
    for d in validation.get("invalid_details", []) or []:
        scenarios_summary.append(
            {
                "scenario_id": d.get("scenario_id", "?"),
                "severity_level": "",
                "trigger": "",
                "execution_status": "skipped",
                "key_indicators": {},
                "issues": d.get("issues", []),
            }
        )

    # ---------- 5) 组装 final_output ----------
    final_output = {
        "status": status,
        "hazard_type": hazard,
        "hazard_type_raw": hazard_raw,
        "simulation_task": state.get("simulation_task", ""),
        "scenarios_summary": scenarios_summary,
        "simulation_results": results,  # 完整逐情景结果（每条含 model_version 与 disclaimer）
        "comparison": comparison,
        "key_findings": comparison.get("key_findings", []),
        "assumptions": state.get("assumptions", []),
        "uncertainties": state.get("uncertainties", []),
        "validation": {
            "scenario_validation": {
                "is_valid": validation.get("is_valid", False),
                "recommendation": validation.get("recommendation", ""),
                "issues": validation.get("issues", []),
                "invalid_details": validation.get("invalid_details", []),
            },
            "routing": {
                "supported": routing.get("supported", False),
                "inputs_ok": routing.get("inputs_ok", False),
                "missing_inputs": routing.get("missing_inputs", []),
                "unsupported_reason": unsupported_reason,
            },
            "analysis_completeness": comparison.get("completeness", {}),
        },
        "missing_inputs": missing_inputs,  # case 2：所需字段及缺失原因
        "unsupported_reason": unsupported_reason,  # case 4：灾种不支持原因
        "model_info": model_info,
        "metadata": {
            "retry_count": state.get("retry_count", 0),
            "max_retries": state.get("max_retries", 3),
            "error_message": state.get("error_message", "") or (execution.get("error", "") if execution else ""),
            "data_source": (data_result.get("processed_data", {}) or {}).get("data_source")
            or (data_result.get("processed_data", {}) or {}).get("source")
            or "unknown",
            "risk_level_from_risk_agent": risk_result.get("risk_level", "unknown"),
            "scenario_source": design.get("source", ""),
            "llm_used_in_analysis": comparison.get("llm_used", False),
            # ★ 全局免责声明：Mock 结果不是真实地质灾害预测
            "note": "仿真结果由 MOCK 玩具模型生成，仅用于流程演示与测试，不是真实预测，禁止用于实际预警决策",
        },
    }

    print(f"[SimulationOutput] status={status} 灾种={hazard} 情景摘要数={len(scenarios_summary)}")

    return {
        "simulation_result": final_output,  # >>> 覆盖为最终交付物，给 Warning / Manager
        "status": status,  # >>> 给 Manager 判断流程
        "next": "end",
    }
