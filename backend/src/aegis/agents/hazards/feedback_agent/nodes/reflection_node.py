"""
Reflection 节点（借鉴 Reflexion：问题定位 → 候选原因 → 证据充分性判定）

【本文件角色】
  对 evaluate 后的高置信可行动问题做归因分析：
    - LLM 通道：按 FEEDBACK_REFLECTION_PROMPT 输出结构化反思
      （环节定位 / 候选原因 / 证据是否充分 / 尚需数据 / 可留存经验）；
    - 确定性兜底：LLM 不可用时，按问题类别给出【待验证的候选原因假设】，
      并如实标注 evidence_sufficient —— 证据不足绝不编造成因结论。

【红线】
  - 反思文本不构成专业地质灾害验证；
  - 不给出新的风险分数/等级；
  - 每次进入本节点 iteration_count 自增（反思轮数受 max_iterations 限界）。

【对外契约】
  读取：feedback_items / evaluation_result / historical_feedback / iteration_count
  写入：reflection_result / iteration_count / next
"""

import json
from typing import Any

from aegis.agents.hazards.feedback_agent.state import FeedbackAgentState

# 问题类别 → 环节定位 与 候选原因假设（确定性兜底用；措辞均为"候选/待验证"，
# 不构成结论；类别口径与 feedback_check_tool 一致）
_STAGE_BY_CATEGORY = {
    "data_quality_issue": ("data", ["数据源覆盖不全或观测缺测", "清洗/标准化环节字段丢失", "数据获取超时或来源不可用"]),
    "risk_calculation_issue": (
        "risk",
        ["因子注册表与观测字段口径不一致", "评估工具入参未覆盖必需因子", "证据链组装环节遗漏（不构成计算错误判定）"],
    ),
    "simulation_issue": ("simulation", ["情景参数与模型要求不匹配", "模型路由输入不齐备", "仿真执行部分失败"]),
    "warning_consistency_issue": (
        "warning",
        ["预警对象组装时引用了过期上游结果", "解释文本生成环节声明与结构化字段不一致", "规则映射版本漂移"],
    ),
    "hazard_type_mismatch": ("manager", ["多 Agent 串联时 hazard_type 传参不一致", "某 Agent 归一化环节失败后未阻断流程"]),
    "model_version_issue": ("manager", ["上游 Agent 与注册表版本更新不同步", "缓存了旧版本 Agent 结果"]),
    "insufficient_evidence": ("manager", ["流程跳过了必要上游 Agent", "Manager 组装反馈输入时字段遗漏"]),
    "workflow_error": ("manager", ["状态级联约定被违反（上游失败下游仍继续）", "某 Agent 输出 Schema 偏离系统约定"]),
}


def _deterministic_reflection(items: list) -> list:
    """确定性兜底反思：只定位环节 + 给候选原因假设 + 标注证据充分性。"""
    reflections = []
    for item in items:
        stage, cause_pool = _STAGE_BY_CATEGORY.get(item.get("category", ""), ("manager", ["原因待人工分析"]))
        evidence_ok = float(item.get("confidence", 0)) >= 0.7
        reflections.append(
            {
                "issue_id": item.get("issue_id", ""),
                "feedback_id": item.get("feedback_id", ""),
                "occurred_stage": stage,
                "possible_causes": [f"候选原因（待验证，非结论）：{c}" for c in cause_pool[:2]],
                "evidence_sufficient": evidence_ok,
                "additional_data_needed": [] if evidence_ok else ["需补充与该问题相关的原始字段值/日志/新版本结果后才能判定原因"],
                "lessons": [f"{item.get('category')}：{item.get('description', '')[:60]}"] if evidence_ok else [],
            }
        )
    return reflections


def reflection_node(state: FeedbackAgentState) -> dict[str, Any]:
    """反思节点（LLM 优先，确定性兜底；轮次限界由 iteration_count 控制）。"""
    iteration = state.get("iteration_count", 0) + 1
    items = [i for i in (state.get("feedback_items", []) or []) if i.get("severity") in ("minor", "major", "critical")]

    reflections, used_llm, overall = [], False, ""
    try:
        from langchain_core.messages import HumanMessage, SystemMessage
        from pydantic import BaseModel, Field

        from aegis.agents.hazards.feedback_agent.llm_setup import get_feedback_llm
        from aegis.agents.hazards.prompts.feedback_prompt import FEEDBACK_REFLECTION_PROMPT, build_reflection_user_prompt

        class ReflectionOut(BaseModel):
            overall_analysis: str = Field(description="整体问题分析概述")
            reflections: list = Field(
                description="逐条反思，每项为对象："
                "{issue_id, occurred_stage, possible_causes(候选原因列表，措辞为假设), "
                "evidence_sufficient(证据是否足以支持原因判断), "
                "additional_data_needed(证据不足时还需哪些数据), "
                "lessons(有证据支撑的可留存经验)}，必须覆盖每个 issue_id"
            )

        llm = get_feedback_llm()
        structured = llm.with_structured_output(ReflectionOut)
        r = structured.invoke(
            [
                SystemMessage(content=FEEDBACK_REFLECTION_PROMPT),
                HumanMessage(
                    content=build_reflection_user_prompt(
                        items_json=json.dumps(items, ensure_ascii=False),
                        historical_json=json.dumps(state.get("historical_feedback", [])[:3], ensure_ascii=False),
                    )
                ),
            ]
        )
        # LLM 结果与确定性结果并轨：以 issue_id 对齐，缺条目用兜底补齐；
        # 低置信条目强制 evidence_sufficient=False（不允许 LLM 拔高证据等级）
        by_id = {x.get("issue_id"): x for x in (r.reflections or []) if isinstance(x, dict)}
        fallback_by_id = {x["issue_id"]: x for x in _deterministic_reflection(items)}
        for item in items:
            base = by_id.get(item["issue_id"]) or fallback_by_id.get(item["issue_id"], {})
            merged = dict(fallback_by_id.get(item["issue_id"], {}))
            merged.update({k: v for k, v in base.items() if v})
            if float(item.get("confidence", 0)) < 0.7:
                merged["evidence_sufficient"] = False
                merged["possible_causes"] = list(merged.get("possible_causes", []))
            merged["issue_id"] = item["issue_id"]
            merged["feedback_id"] = item.get("feedback_id", "")
            merged["source"] = "llm+guardrail"
            reflections.append(merged)
        overall, used_llm = r.overall_analysis, True
    except Exception as e:
        print(f"[Reflection] LLM 不可用，走确定性兜底反思: {e}")
        reflections = _deterministic_reflection(items)
        overall = "确定性兜底反思：仅完成环节定位与候选原因假设，证据不足的条目已标注待补充数据，未给出成因结论。"
        used_llm = False

    # 历史经验对照（只读参考：同问题类型是否曾经出现过）
    historical = state.get("historical_feedback", []) or []
    repeat_types = sorted({h.get("problem_type") for h in historical if h.get("problem_type")})

    reflection_result = {
        "iteration": iteration,
        "overall_analysis": overall,
        "reflections": reflections,
        "used_llm": used_llm,
        "repeat_problem_types": repeat_types,
        "note": "反思是系统内部的归因参考，不构成专业地质灾害验证；evidence_sufficient=false 的条目不得作为重跑上游的充分依据",
    }

    print(f"[Reflection] 第 {iteration} 轮，条目数={len(reflections)} used_llm={used_llm} 重复问题类型={repeat_types}")

    return {
        "reflection_result": reflection_result,
        "iteration_count": iteration,
        "next": "suggestion",
    }
