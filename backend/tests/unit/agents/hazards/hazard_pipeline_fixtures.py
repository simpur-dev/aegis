"""
Feedback Agent 测试数据构造器（从既有注册表构造"契约一致"的正常链路交付物）

【定位】为独立测试与演示提供五灾种的 data/risk/simulation/warning 结果：
  - Risk 走真实因子引擎（hazard_risk_registry.assessment_tool，确定性）；
  - Simulation 走真实 Mock 模型（hazard_model_router.get_model，确定性）；
  - Warning 按注册表映射规则组装结构化对象（确定性，不经过 LLM）。
  构造口径与各 Agent 输出节点的 Schema 对齐，用于验证 Feedback 的接口兼容性。

【诚实声明】这里构造的是"符合系统约定的正常输入"，用于测试 Feedback
  不误报（正常输入不产生虚假问题）；异常场景由测试在用例中定向注入。
"""

from datetime import UTC, datetime, timedelta

from aegis.agents.hazards.hazard_registry import HAZARD_CN_NAME, normalize_hazard_type


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def build_data_result(hazard_type: str, event_id: str = "EVT-T-001") -> dict:
    """构造 Data Agent 最终平铺 State（与 data_output_node 输出契约一致）。"""
    from aegis.agents.hazards.risk_agent.tools.hazard_risk_registry import get_hazard_risk_profile

    hazard_key = normalize_hazard_type(hazard_type)
    profile = get_hazard_risk_profile(hazard_key)
    region = "西藏波密"

    events = [
        dict(
            profile.get("sample_values", {}),
            event_id=f"{event_id}-OBS-{i:03d}",
            time=_now_iso(),
            latitude=29.6 + i * 0.01,
            longitude=95.3 + i * 0.01,
            region=region,
        )
        for i in range(1, 3)
    ]

    return {
        "hazard_type": hazard_key,
        "processed_data": {
            "events": events,
            "data_source": "mock",
            "is_mock": True,
            "hazard_type": hazard_key,
            "fetch_timestamp": _now_iso(),
        },
        "data_quality": {"is_valid": True, "validation_reason": "测试构造：必需字段齐备"},
        "missing_data": [],
        "data_sufficiency": {"sufficient": True, "reason": "测试构造：数据足以支撑评估"},
        "status": "success",
        "error_message": "",
    }


def build_risk_result(hazard_type: str, data_result: dict) -> dict:
    """构造 Risk Agent 最终 risk_result（走真实因子引擎，与 risk_output_node 契约一致）。"""
    from aegis.agents.hazards.risk_agent.tools.hazard_risk_registry import get_hazard_risk_profile, sample_observations

    hazard_key = normalize_hazard_type(hazard_type)
    profile = get_hazard_risk_profile(hazard_key)

    assessment = profile["assessment_tool"](sample_observations(hazard_key, observed_at=_now_iso()), "西藏波密")
    status = "success" if assessment.get("assessment_status") == "scored" else "insufficient_data"

    return {
        "status": status,
        "hazard_type": hazard_key,
        "risk_level": assessment.get("provisional_level", "unknown"),
        "provisional_level_status": assessment.get("provisional_level_status", "mock_thresholds"),
        "risk_factors": list(profile.get("risk_factors", [])),
        "trigger_conditions": [f"{h['rule_id']}: {h['description']}" for h in assessment.get("rule_hits", [])],
        "rule_hits": assessment.get("rule_hits", []),
        "reasoning": "（测试构造）因子引擎评估结果，详见 assessment",
        "assessment": assessment,
        "evidence": [{"source": "factor_engine", "note": "测试构造的最小证据条目"}],
        "missing_info": [],
        "uncertainties": ["（测试构造）评估结果"],
        "validation": {"is_valid": status == "success", "issues": [], "recommendation": "proceed" if status == "success" else "fail"},
        "model_info": {
            "rule_version": profile.get("rule_version", "MOCK-v0.1"),
            "engine_version": assessment.get("engine_version", ""),
            "implementation_status": "mock",
            "is_mock": True,
        },
        "metadata": {"note": "测试构造的 Risk 交付物（Mock）"},
    }


def build_simulation_result(hazard_type: str, risk_result: dict) -> dict:
    """构造 Simulation Agent 最终 simulation_result（走真实 Mock 模型，契约对齐输出节点）。"""
    from aegis.agents.hazards.simulation_agent.tools.hazard_model_router import get_model, get_model_requirements

    hazard_key = normalize_hazard_type(hazard_type)
    mreq = get_model_requirements(hazard_key)

    results = []
    for i, severity in enumerate(("medium", "high"), start=1):
        scenario = {
            "scenario_id": f"SC-{i:02d}",
            "hazard_type": hazard_key,
            "severity_level": severity,
            "trigger": "测试构造触发（Mock）",
            "trigger_source_tag": "assumption",
            "duration_hours": 6.0,
            "notes": "feedback 测试情景",
        }
        # 模型必需/可选输入用 Risk 样例观测值填充（同名字段口径）
        from aegis.agents.hazards.risk_agent.tools.hazard_risk_registry import get_hazard_risk_profile

        sample = get_hazard_risk_profile(hazard_key).get("sample_values", {})
        for k in list(mreq["required_inputs"]) + list(mreq["optional_inputs"]):
            if k in sample:
                scenario[k] = sample[k]
        model = get_model(hazard_key)
        v = model.validate_input(scenario)
        if not v["is_valid"]:
            raise AssertionError(f"测试情景不合法: {v}")
        run_out = model.run_simulation(v["normalized"], {"attempt": 1, "risk_result": risk_result, "data_summary": "test"})
        results.append(model.collect_results(run_out, v["normalized"]))

    return {
        "status": "success",
        "hazard_type": hazard_key,
        "hazard_type_raw": hazard_type,
        "simulation_task": "（测试构造）情景推演",
        "scenarios_summary": [
            {
                "scenario_id": r["scenario_id"],
                "severity_level": r.get("severity_level", ""),
                "trigger": r.get("trigger", ""),
                "execution_status": "ok",
                "key_indicators": {},
            }
            for r in results
        ],
        "simulation_results": results,
        "comparison": {},
        "key_findings": [],
        "assumptions": [],
        "uncertainties": ["（测试构造）"],
        "validation": {
            "scenario_validation": {"is_valid": True, "recommendation": "proceed", "issues": [], "invalid_details": []},
            "routing": {"supported": True, "inputs_ok": True, "missing_inputs": [], "unsupported_reason": ""},
            "analysis_completeness": {},
        },
        "missing_inputs": [],
        "unsupported_reason": "",
        "model_info": {
            "hazard_type": hazard_key,
            "model_version": mreq["model_version"],
            "simulation_mode": mreq["simulation_mode"],
            "is_real_prediction": False,
            "disclaimer": mreq["disclaimer"],
        },
        "metadata": {"note": "仿真结果由 MOCK 玩具模型生成（测试构造）"},
    }


def _warning_level_from_R(rules: dict, R) -> str:
    """按注册表映射参数得到等级（等级映射属 Warning 职责，此处仅按规则复现正常值）。"""
    p = rules["params"]
    if R is None or R <= 0:
        return "none"
    if p["r_warning"] <= R:
        return "warning"
    if p["r_watch"] <= R:
        return "watch"
    return "advisory"


def build_warning_result(hazard_type: str, data_result: dict, risk_result: dict, simulation_result: dict | None = None) -> dict:
    """构造 Warning Agent 最终 warning_result（与 warning_output_node/alert 契约一致）。"""
    from aegis.agents.hazards.warning_agent.tools.hazard_warning_rule_registry import get_hazard_warning_rules, get_suggested_actions

    hazard_key = normalize_hazard_type(hazard_type)
    rules = get_hazard_warning_rules(hazard_key)
    assessment = risk_result.get("assessment", {}) or {}
    warning_level = _warning_level_from_R(rules, assessment.get("R"))

    events = (data_result.get("processed_data", {}) or {}).get("events", [])
    region = events[0].get("region", "未标注区域") if events else "未标注区域"
    now = _now_iso()

    alert = {
        "alert_id": f"ALT-{hazard_key.upper()}-TEST0001",
        "related_event_refs": [e.get("event_id") for e in events if e.get("event_id")],
        "hazard_type": hazard_key,
        "hazard_type_cn": HAZARD_CN_NAME.get(hazard_key, hazard_key),
        "warning_level": warning_level,
        "level_basis": "mock_mapping_v0.1",
        "official_grade": None,
        "official_grade_status": "pending_review",
        "alert_status": "draft",
        "mock_only": True,
        "generated_at": now,
        "valid_from": now,
        "valid_until": (datetime.now(UTC) + timedelta(hours=rules["params"]["valid_duration_hours"])).isoformat(),
        "published_at": None,
        "acknowledged_at": None,
        "resolved_at": None,
        "cancelled_at": None,
        "updated_at": now,
        "target_region": region,
        "sender": "测试构造（非官方发布机构）",
        "suggested_actions": get_suggested_actions(hazard_key, warning_level),
        "based_on": {
            "risk_status": risk_result.get("status", ""),
            "risk_level": risk_result.get("risk_level", "unknown"),
            "P": assessment.get("P"),
            "T": assessment.get("T"),
            "R": assessment.get("R"),
            "rule_hits": assessment.get("rule_hits", []),
            "data_is_mock": True,
            "simulation": {
                "status": (simulation_result or {}).get("status", ""),
                "scenario_count": len((simulation_result or {}).get("simulation_results", []) or []),
            },
        },
        "trigger_conditions": risk_result.get("trigger_conditions", []),
        "rule_version": rules.get("mapping_version", "MOCK-v0.1"),
        "mapping_version": rules.get("mapping_version", "MOCK-v0.1"),
    }

    return {
        "status": "draft",
        "hazard_type": hazard_key,
        "hazard_type_cn": HAZARD_CN_NAME.get(hazard_key, hazard_key),
        "alert": alert,
        "explanation": {
            "text": f"（Mock 测试构造）{alert['hazard_type_cn']}模拟预警草稿，等级 {warning_level}，非真实预警，禁止用于实际发布。",
            "summary": "测试构造说明",
            "bullet_points": [],
            "used_llm": False,
        },
        "validation": {"is_valid": True, "issues": [], "recommendation": "proceed"},
        "rule_result": {
            "warning_level": warning_level,
            "level_basis": "mock_mapping_v0.1",
            "mapping_version": rules["mapping_version"],
            "hits": [],
            "params_used": rules["params"],
            "escalation": None,
        },
        "cap": {"status": "skipped", "reason": "测试构造不执行 CAP 转换"},
        "metadata": {"note": "预警对象为 Mock 模拟产物（测试构造）"},
    }


def build_consistent_pipeline_results(
    hazard_type: str, event_id: str = "EVT-T-001", include_simulation: bool = True, include_warning: bool = True
) -> dict:
    """构造五字段一致的完整链路输入（data/risk/simulation/warning_result）。"""
    data_result = build_data_result(hazard_type, event_id=event_id)
    risk_result = build_risk_result(hazard_type, data_result)
    sim_result = build_simulation_result(hazard_type, risk_result) if include_simulation else {}
    warn_result = build_warning_result(hazard_type, data_result, risk_result, sim_result) if include_warning else {}
    return {
        "data_result": data_result,
        "risk_result": risk_result,
        "simulation_result": sim_result,
        "warning_result": warn_result,
    }
