"""
ResultsAnalysis 节点 —— 结果比较与分析

【本文件角色】
  确定性对比（compare_scenarios_tool，数字不做任何二次推算）
  → LLM 叙述分析（结构化输出；不可用时用工具结论兜底）
  → 完整性检查（check_results_completeness_tool）→ 决定是否消耗预算做修复重试。

【对外契约】
  读取：simulation_results / hazard_type / simulation_task / uncertainties /
        comparison_result（重试时读上次缺失项）/ retry_count / max_retries
  写入：comparison_result / uncertainties / retry_count / next
  路由：route_after_analysis 读 comparison_result.completeness 的
        is_complete / fixable 与 retry_count（不完整且可修复且预算未耗尽 → 本节点重试）。

【诚实红线】
  - 叙述中的数字全部来自确定性对比工具，LLM 不得改写
  - llm_used=False 时分析结论明确标注为"确定性工具输出"
  - 所有结论必须携带 MOCK 局限，不得称为真实预测
"""

import json

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from aegis.agents_ext.prompts.simulation_prompt import (
    RESULTS_ANALYSIS_SYSTEM_PROMPT,
    build_results_analysis_user_prompt,
)
from aegis.agents_ext.simulation_agent.llm_setup import get_simulation_llm
from aegis.agents_ext.simulation_agent.tools.completeness_check_tool import check_results_completeness_tool
from aegis.agents_ext.simulation_agent.tools.hazard_model_router import get_model_requirements
from aegis.agents_ext.simulation_agent.tools.scenario_comparison_tool import compare_scenarios_tool

# 公共 MOCK 局限（追加进 uncertainties，去重后写入）
_MOCK_UNCERTAINTY = "所有分析基于 MOCK 玩具模型的归一化合成指标（mock_hazard_index 等），不是真实地质灾害预测，不能用于实际预警决策"


class Finding(BaseModel):
    """单条关键发现。"""

    text: str = Field(description="发现内容（数字必须逐字引自对比工具输出）")
    source_tag: str = Field(default="simulation", description="来源: observed/analysis/simulation/assumption")


class ScenarioAnalysis(BaseModel):
    """LLM 分析输出。"""

    key_findings: list[Finding] = Field(default_factory=list, description="关键发现；涉及模拟数字的必须与对比表一致")
    comparison_summary: str = Field(default="", description="跨情景对比的简短叙述")
    worst_scenario_id: str = Field(default="", description="最不利情景 ID（须来自输入排序结果）")
    implications: list[str] = Field(default_factory=list, description="对预警/处置的启示，每条注明是基于 MOCK 模拟的演示性结论")
    uncertainties: list[str] = Field(default_factory=list, description="分析层面新增的不确定性")
    limitations: list[str] = Field(default_factory=list, description="局限说明（须含 MOCK 模型局限）")


def _append_unique(base: list, extra: list) -> list:
    """去重追加（无 reducer，读旧值拼接后整体返回）。"""
    out = list(base or [])
    for item in extra:
        if item and item not in out:
            out.append(item)
    return out


def _fallback_findings(comp: dict) -> list[dict]:
    """确定性兜底发现：全部来自对比工具输出，source_tag=simulation。"""
    findings = []
    ranking = comp.get("ranking", []) or []
    if ranking:
        top = ranking[0]
        findings.append(
            {
                "text": (
                    f"危险度最高的情景为 {top['scenario_id']}"
                    f"（severity={top['severity_level']}，"
                    f"mock_hazard_index={top['mock_hazard_index']}）"
                ),
                "source_tag": "simulation",
            }
        )
    for metric, ext in (comp.get("metric_extremes", {}) or {}).items():
        findings.append(
            {
                "text": (
                    f"指标 {metric} 最大值出现在 {ext['max']['scenario_id']}"
                    f"（{ext['max']['value']}），最小值出现在 {ext['min']['scenario_id']}"
                    f"（{ext['min']['value']}）"
                ),
                "source_tag": "simulation",
            }
        )
    return findings


def results_analysis_node(state: dict) -> dict:
    """结果分析节点。"""
    results = state.get("simulation_results", []) or []
    hazard = state.get("hazard_type", "") or ""
    task = state.get("simulation_task", "") or ""
    retry_count = state.get("retry_count", 0)
    max_retries = state.get("max_retries", 3)
    # 修复重试时读取上次的缺失项，喂给 LLM 针对性补全
    previous_issues = "；".join((state.get("comparison_result", {}) or {}).get("completeness", {}).get("missing_items", []) or [])

    # ---------- 1) 确定性对比（数字唯一来源）----------
    comp = compare_scenarios_tool(results)

    # ---------- 2) LLM 叙述分析（不可用时兜底）----------
    findings = _fallback_findings(comp)
    narrative = (
        f"共对比 {len(comp.get('comparison_table', []) or [])} 个情景；基线情景为 {comp.get('baseline_scenario_id', '') or '（无）'}。"
    )
    worst = (comp.get("ranking", []) or [{}])[0].get("scenario_id", "")
    implications = ["（确定性兜底）以上为 MOCK 模拟的相对比较结论，不构成预警建议"]
    limitations = ["分析基于 MOCK 玩具模型的归一化合成指标，无真实物理含义"]
    analysis_uncertainties = []
    llm_used = False
    try:
        llm = get_simulation_llm()
        structured = llm.with_structured_output(ScenarioAnalysis)
        # 逐情景摘要（含 model_version 与 disclaimer，压缩 token）
        digest = json.dumps(
            [
                {
                    "scenario_id": r.get("scenario_id"),
                    "severity_level": r.get("severity_level"),
                    "indicators": r.get("indicators"),
                    "model_version": r.get("model_version"),
                    "disclaimer": r.get("disclaimer"),
                }
                for r in results
            ],
            ensure_ascii=False,
        )
        analysis: ScenarioAnalysis = structured.invoke(
            [
                SystemMessage(content=RESULTS_ANALYSIS_SYSTEM_PROMPT),
                HumanMessage(
                    content=build_results_analysis_user_prompt(
                        simulation_task=task,
                        hazard_type=hazard,
                        comparison_tool_result=json.dumps(comp, ensure_ascii=False),
                        results_digest=digest,
                        retry_count=retry_count,
                        previous_issues=previous_issues,
                    )
                ),
            ]
        )
        findings = [{"text": f.text, "source_tag": f.source_tag} for f in analysis.key_findings] or findings
        narrative = analysis.comparison_summary or narrative
        worst = analysis.worst_scenario_id or worst
        implications = list(analysis.implications or []) or implications
        limitations = list(analysis.limitations or []) or limitations
        analysis_uncertainties = list(analysis.uncertainties or [])
        llm_used = True
    except Exception as e:
        print(f"[ResultsAnalysis] LLM 分析不可用，使用确定性工具兜底: {e}")

    # ---------- 3) 组装 comparison_result（completeness 先占位）----------
    comparison_result = {
        "comparison_table": comp.get("comparison_table", []),
        "ranking": comp.get("ranking", []),
        "metric_extremes": comp.get("metric_extremes", {}),
        "scenario_differences": comp.get("scenario_differences", []),
        "baseline_scenario_id": comp.get("baseline_scenario_id", ""),
        "key_findings": findings,
        "worst_scenario_id": worst,
        "narrative_summary": narrative,
        "implications": implications,
        "limitations": limitations,
        "llm_used": llm_used,
        "completeness": {},
    }

    # ---------- 4) 完整性检查（确定性判据）----------
    try:
        required_metrics = get_model_requirements(hazard)["required_metrics"]
    except Exception:
        required_metrics = ["mock_hazard_index", "mock_impact_index"]
    completeness = check_results_completeness_tool(results, comparison_result, required_metrics)
    comparison_result["completeness"] = completeness

    # ---------- 5) 修复重试判定（与 route_after_analysis 同条件）----------
    new_retry_count = retry_count
    if (not completeness["is_complete"]) and completeness["fixable"] and retry_count < max_retries:
        new_retry_count = retry_count + 1  # >>> ★ 与 retry 判定原子绑定

    # ---------- 6) 不确定性（去重追加）----------
    extra_uncertainties = [_MOCK_UNCERTAINTY, *analysis_uncertainties]
    if not completeness["is_complete"]:
        extra_uncertainties.append(
            f"输出不完整（缺失: {'；'.join(completeness['missing_items'])}），"
            f"分析结论可信度受限（{'可' if completeness['fixable'] else '不可'}修复）"
        )
    uncertainties = _append_unique(state.get("uncertainties", []), extra_uncertainties)

    print(
        f"[ResultsAnalysis] llm_used={llm_used} "
        f"complete={completeness['is_complete']} fixable={completeness['fixable']} "
        f"retry_count={new_retry_count}/{max_retries}"
    )

    return {
        "comparison_result": comparison_result,
        "uncertainties": uncertainties,
        "retry_count": new_retry_count,
        "next": "simulation_output",
    }
