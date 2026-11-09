"""
RiskOutput 节点（面向西藏五类山地地质灾害，因子化口径）

【本文件角色】组装最终结构化风险结果，写回 State，供 Simulation / Warning / Manager 读取。
【不写自然语言报告】只输出结构化 dict。
【对外契约 —— 重点】
  本节点 return 的 risk_result 字段 = Risk Agent 的最终交付物：
    >>> 提供给 Warning Agent：读 risk_result["risk_level"] / "R" / "rule_hits" /
        "trigger_conditions" / "model_info" / "metadata"（判断是否具备预警条件）
    >>> 提供给 Simulation Agent：读 risk_result["hazard_type"] / "risk_level" / "uncertainties"
    >>> 提供给 Manager 全局 State：读 status 判断流程是否继续
"""

import logging
from typing import Any

from aegis.agents_ext.hazard_registry import normalize_hazard_type
from aegis.agents_ext.risk_agent.confidence import assess_confidence
from aegis.agents_ext.risk_agent.state import RiskAgentState
from aegis.agents_ext.risk_agent.tools.factor_engine import FACTOR_ENGINE_VERSION, R_FORMULA_NOTE
from aegis.domain.provenance import DataProvenance

log = logging.getLogger(__name__)


def risk_output_node(state: RiskAgentState) -> dict[str, Any]:
    """风险输出节点（组装最终交付物，携带灾种 / 因子框架 / 置信度 / 数据出处 / Mock 标记）。"""

    risk_result = state.get("risk_result", {})
    evidence = state.get("evidence", [])
    validation = state.get("validation_result", {})
    calculation_result = state.get("calculation_result", {})  # ★ 因子评估信封
    retry_count = state.get("retry_count", 0)
    error_message = state.get("error_message", "")
    hazard_type = normalize_hazard_type(state.get("hazard_type", "")) or state.get("hazard_type", "")

    # ---------- 判定最终状态 ----------
    if risk_result.get("needs_more_data") and not evidence:
        status = "insufficient_data"  # 没数据，让 Manager 决定是否回调 Data
    elif validation.get("is_valid"):
        status = "success"
    elif validation.get("recommendation") == "fail":
        status = "failed"
    else:
        status = "uncertain"  # 验证未完全通过但已尽力

    # ---------- 尚未具备的能力（诚实声明）----------
    unimplemented_capabilities = []
    try:
        from aegis.agents_ext.risk_agent.tools.hazard_risk_registry import get_hazard_risk_profile

        profile = get_hazard_risk_profile(hazard_type)
        unimplemented_capabilities = list(profile.get("current_limitations", []))
    except Exception:
        unimplemented_capabilities = []

    # ---------- 因子框架字段（来自 assess 节点的信封）----------
    assessment = risk_result.get("assessment", {}) or calculation_result or {}
    factor_scores = assessment.get("factor_scores", {})
    impact_factors = {k: v for k, v in factor_scores.items() if v.get("group") == "I"}

    # ---------- 置信度（生产方自产：覆盖率 + 验证结果，mock 方法封顶 0.60）----------
    confidence, confidence_basis = assess_confidence(assessment.get("completeness", {}), validation)

    # ---------- 数据出处（一等字段）：输入证据的 kind/sources + 本次研判的方法 ----------
    request_provenance = DataProvenance.from_payload(state.get("data_provenance"))
    provenance = DataProvenance(
        data_kind=request_provenance.data_kind,
        sources=list(request_provenance.sources)[:16],
        method="factor_engine_mock",
        method_version=assessment.get("engine_version") or FACTOR_ENGINE_VERSION,
        # MOCK 权重 + 原型公式：无论输入数据多真实，这条结论的方法面就是 mock，宁可读者高估。
        is_mock=True,
        disclaimer=(R_FORMULA_NOTE + " " + str(assessment.get("disclaimer", "")))[:500],
    )

    # ---------- 组装最终交付物 ----------
    final_output = {
        "status": status,
        "hazard_type": hazard_type,  # ★ 灾种标准键
        "risk_level": risk_result.get("risk_level", "unknown"),  # 引擎 MOCK 阈值的临时等级
        "provisional_level_status": assessment.get("provisional_level_status", "mock_thresholds"),  # ★ 等级阈值来源标记
        "risk_factors": state.get("risk_factors", []),
        "trigger_conditions": risk_result.get("trigger_conditions", []),  # ★ 保护规则命中（触发条件）
        "rule_hits": assessment.get("rule_hits", []),  # ★ 触发规则结果
        "reasoning": risk_result.get("reasoning", ""),
        "confidence": confidence,  # ★ 生产方自产：0.15..0.60（mock 方法封顶）
        "confidence_basis": confidence_basis,  # ★ 置信度的依据（可核）
        "data_provenance": provenance.model_dump(),  # ★ 数据出处（一等字段）
        "assessment": {  # ★ 因子框架核心输出
            "engine_version": assessment.get("engine_version", ""),
            "assessment_status": assessment.get("assessment_status", ""),
            "formula": assessment.get("formula", {}),  # ★ R 公式身份声明
            "P": assessment.get("P"),
            "T": assessment.get("T"),
            "R": assessment.get("R"),
            "factor_scores": factor_scores,  # ★ 因子得分（含权重与来源）
            "completeness": assessment.get("completeness", {}),  # ★ 数据覆盖程度
            "coverage_basis": assessment.get("coverage_basis", ""),
            "min_coverage": assessment.get("min_coverage"),
            "missing_factors": assessment.get("missing_factors", []),
            "insufficient_reasons": assessment.get("insufficient_reasons", []),
            "route_value": assessment.get("route_value", ""),
            "sample_count": assessment.get("sample_count", 0),
        },
        "impact_factors": impact_factors,  # ★ 影响条件（如冰湖下游距离）
        "evidence": evidence,
        "missing_info": risk_result.get("missing_info", []),
        "uncertainties": state.get("uncertainties", []),
        "validation": {
            "is_valid": validation.get("is_valid", False),
            "issues": validation.get("issues", []),
            "recommendation": validation.get("recommendation", ""),
        },
        "model_info": {  # ★ 规则/引擎版本与 Mock 标记
            "rule_version": risk_result.get("rule_version", "unknown"),
            "engine_version": assessment.get("engine_version", ""),
            "implementation_status": risk_result.get("implementation_status", "mock"),
            "is_mock": risk_result.get("implementation_status", "mock") == "mock",
        },
        "unimplemented_capabilities": unimplemented_capabilities,  # ★ 尚未实现的能力
        "metadata": {
            "retry_count": retry_count,
            "error_message": error_message,
            "data_source": request_provenance.data_kind,
            "is_mock_data": request_provenance.is_mock,
            "note": "风险等级与组合指数基于 MOCK 因子权重与原型候选公式，非真实地质灾害风险预测，禁止用于实际预警决策",
        },
    }

    log.info(
        "[RiskOutput] status=%s hazard_type=%s 风险等级=%s R=%s",
        status,
        hazard_type,
        final_output["risk_level"],
        assessment.get("R"),
    )

    return {
        "risk_result": final_output,  # >>> 覆盖为最终交付物，给 Warning / Simulation / Manager
        "status": status,  # >>> 给 Manager 判断流程
        "next": "end",
    }
