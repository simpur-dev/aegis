"""
ScenarioDesign 节点 —— 情景设计

【本文件角色】
  推演情景设计：前置检查（灾种支持性 / 上游可用性，fail-fast）
  → LLM 设计情景（结构化输出）→ LLM 不可用时确定性默认情景兜底。

【对外契约】
  读取：simulation_task / data_result / risk_result / hazard_type / preset_scenario_configs
  写入：scenario_design_result / scenario_configs / assumptions / uncertainties / next
  路由：route_after_scenario_design 读 scenario_design_result 的
        needs_more_data / unsupported_hazard 决定"进校验"还是"直送输出"。

【诚实红线】
  - 不编造上游没有的观测数据；情景参数一律视为假设并打标签
  - 参数菜单来自 get_model_requirements，LLM 只能从中选键取值
  - LLM 失败走确定性兜底，兜底情景明确标注"非专家设计"
"""

import json
import logging

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from aegis.agents.hazards.prompts.simulation_prompt import (
    SCENARIO_DESIGN_SYSTEM_PROMPT,
    build_scenario_design_user_prompt,
)
from aegis.agents.hazards.simulation_agent.llm_setup import get_simulation_llm
from aegis.agents.hazards.simulation_agent.tools.default_scenario_tool import build_default_scenarios_tool
from aegis.agents.hazards.simulation_agent.tools.hazard_model_router import (
    get_model_requirements,
    list_supported_hazards,
    normalize_hazard_type,
)

log = logging.getLogger("aegis.agents.hazards.simulation_agent.nodes.scenario_design_node")

# 支持灾种的可读清单文本（模块级，一次性生成）
SUPPORTED_LIST_TEXT = "、".join(f"{h['hazard_type_cn']}({h['hazard_type']})" for h in list_supported_hazards())


# ============================================================
# LLM 结构化输出模型
# ============================================================


class ScenarioDraft(BaseModel):
    """单个情景的设计稿（LLM 输出；design 节点会把它展平为情景 dict）。"""

    scenario_id: str = Field(description="情景唯一标识，如 SC-01")
    severity_level: str = Field(description="low/medium/high/extreme")
    trigger: str = Field(description="触发因素描述，如 强降雨/快速升温/冰崩涌浪/冰湖水位上涨")
    trigger_source_tag: str = Field(description="触发依据来源标签: observed(真实观测)/analysis(已有分析)/assumption(情景假设)")
    duration_hours: float = Field(default=6.0, gt=0, description="情景时间窗（小时）")
    parameters: dict[str, float] = Field(description="灾种参数字典；★键名必须且只能取自参数菜单，禁止发明新参数或阈值公式参数")
    rationale: str = Field(description="设计依据：改变了哪些参数、为什么（引用数据须注明来源标签）")
    assumptions: list[str] = Field(default_factory=list, description="本情景的假设清单")


class ScenarioPlan(BaseModel):
    """情景设计方案（LLM 输出）。"""

    needs_more_data: bool = Field(default=False, description="上游信息不足以设计任何情景时为 true")
    missing_info: list[str] = Field(default_factory=list, description="缺失信息清单，每条格式『字段: 缺失原因』")
    scenario_configs: list[ScenarioDraft] = Field(default_factory=list, description="2~4 个情景，须覆盖至少两个 severity_level")
    assumptions: list[str] = Field(default_factory=list, description="全部情景假设的显式清单")
    uncertainties: list[str] = Field(default_factory=list, description="设计层面的限制与不确定性")


# ============================================================
# 摘要构造（给 LLM 的上游上下文，如实反映数据局限）
# ============================================================


def _build_data_summary(data_result: dict) -> str:
    """Data Agent 数据摘要。如实说明：上游为灾种观测数据（当前为 Mock 构造）。"""
    processed = data_result.get("processed_data", {}) or {}
    events = processed.get("events", []) or []
    source = processed.get("data_source") or processed.get("source") or "unknown"
    hazard_key = processed.get("hazard_type") or data_result.get("hazard_type") or ""
    is_mock = processed.get("is_mock", source == "mock")
    return (
        f"数据状态={data_result.get('status', 'unknown')}；数据源={source}（Mock 构造={bool(is_mock)}）；"
        f"观测记录数={len(events)}；"
        f"数据质量={json.dumps(data_result.get('data_quality', {}), ensure_ascii=False)[:200]}。"
        f"注意：上游观测记录为 {hazard_key or '未标注灾种'} 的 Mock 合成数据，"
        f"引用其中的数值时必须标注来源为 Mock，不得当作真实观测。"
    )


def _build_risk_summary(risk_result: dict) -> str:
    """Risk Agent 研判摘要。"""
    return (
        f"风险状态={risk_result.get('status', 'unknown')}；"
        f"风险等级={risk_result.get('risk_level', 'unknown')}；"
        f"结论摘要={str(risk_result.get('reasoning', ''))[:200]}；"
        f"证据条数={len(risk_result.get('evidence', []) or [])}；"
        f"不确定性条数={len(risk_result.get('uncertainties', []) or [])}。"
    )


# ============================================================
# 早退分支（统一结构，路由函数据此分流）
# ============================================================


def _early_exit_unsupported(reason: str) -> dict:
    """灾种缺失/无法识别：直送输出（status 将判为 failed）。"""
    return {
        "scenario_design_result": {
            "needs_more_data": False,
            "missing_info": [],
            "unsupported_hazard": True,
            "reason": reason,
            "used_llm": False,
            "source": "precheck",
        },
        "scenario_configs": [],
        "assumptions": [],
        "uncertainties": [f"灾种不支持：{reason}"],
        "next": "simulation_output",
    }


def _early_exit_needs_more_data(missing_info: list[str]) -> dict:
    """上游数据缺失：直送输出（status 将判为 insufficient_data）。"""
    return {
        "scenario_design_result": {
            "needs_more_data": True,
            "missing_info": missing_info,
            "unsupported_hazard": False,
            "reason": "；".join(missing_info),
            "used_llm": False,
            "source": "precheck",
        },
        "scenario_configs": [],
        "assumptions": [],
        "uncertainties": ["上游输入不足，未进入情景设计"],
        "next": "simulation_output",
    }


# ============================================================
# LLM 设计稿 → 情景 dict（扁平结构：公共字段 + 参数平铺）
# ============================================================


def _flatten_drafts(plan: ScenarioPlan, hazard_key: str) -> list:
    configs = []
    for i, d in enumerate(plan.scenario_configs, start=1):
        sc = {
            "scenario_id": (d.scenario_id or f"SC-{i:02d}").strip(),
            "hazard_type": hazard_key,
            "severity_level": d.severity_level,
            "trigger": d.trigger,
            "trigger_source_tag": d.trigger_source_tag or "assumption",
            "duration_hours": d.duration_hours,
            "notes": d.rationale,
            "test_hooks": {},
            "assumptions": list(d.assumptions or []),
        }
        # 参数平铺进情景 dict（模型按 param_constraints 校验范围）
        for k, v in (d.parameters or {}).items():
            sc[str(k)] = v
        configs.append(sc)
    # LLM 一个情景都没给 → 交给确定性默认模板
    return configs or build_default_scenarios_tool(hazard_key)


# ============================================================
# 节点主体
# ============================================================


def scenario_design_node(state: dict) -> dict:
    """情景设计节点。"""
    task = state.get("simulation_task", "") or ""
    data_result = state.get("data_result", {}) or {}
    risk_result = state.get("risk_result", {}) or {}
    hazard_raw = state.get("hazard_type", "") or ""
    presets = state.get("preset_scenario_configs", []) or []

    log.info(f"[ScenarioDesign] 任务={task[:40]}... 灾种输入={hazard_raw!r} 直传情景数={len(presets)}")

    # ---------- 前置检查 1：灾种（缺失 / 无法识别 → fail-fast）----------
    if not str(hazard_raw).strip():
        return _early_exit_unsupported(f"未指定灾种（hazard_type 为空）。当前支持：{SUPPORTED_LIST_TEXT}")
    normalized = normalize_hazard_type(hazard_raw)
    if not normalized:
        return _early_exit_unsupported(f"灾种'{hazard_raw}'无法识别。当前支持：{SUPPORTED_LIST_TEXT}")

    # ---------- 前置检查 2：上游可用性（数据缺失 → fail-fast）----------
    missing_info = []
    if (not data_result) or data_result.get("status") == "failed":
        status_text = data_result.get("status", "(缺失)") if data_result else "(缺失)"
        missing_info.append(f"data_result: 上游 Data Agent 结果缺失或失败（status={status_text}）")
    if not risk_result:
        missing_info.append("risk_result: 上游 Risk Agent 结果缺失，缺少风险研判上下文")
    if missing_info:
        return _early_exit_needs_more_data(missing_info)

    # ---------- 情景生成 ----------
    used_llm = False
    source = "preset"
    if presets:
        # 外部直传情景：跳过 LLM（测试注入 / 将来 Manager 指定情景的正式入口）
        # 灾种以调用方显式传入的 hazard_type 为准：统一覆写进每条情景，
        # 避免"调用方声明灾种 A、情景里写着灾种 B（或漏写）"造成校验混乱。
        configs = []
        for p in presets:
            sc = dict(p)
            sc["hazard_type"] = normalized
            configs.append(sc)
        assumptions = ["情景由调用方通过 preset_scenario_configs 直传（未经 LLM 设计），参数合理性由调用方保证"]
        uncertainties = ["外部直传情景：假设与参数来源为调用方"]
    else:
        try:
            req = get_model_requirements(normalized)
            llm = get_simulation_llm()
            structured = llm.with_structured_output(ScenarioPlan)
            plan: ScenarioPlan = structured.invoke(
                [
                    SystemMessage(content=SCENARIO_DESIGN_SYSTEM_PROMPT),
                    HumanMessage(
                        content=build_scenario_design_user_prompt(
                            simulation_task=task,
                            hazard_type=normalized,
                            hazard_type_cn=req["hazard_type_cn"],
                            data_summary=_build_data_summary(data_result),
                            risk_summary=_build_risk_summary(risk_result),
                            requirements_str=json.dumps(req["param_menu"], ensure_ascii=False, indent=1),
                        )
                    ),
                ]
            )
            # LLM 判断上游信息不足 → 早退（缺什么由 LLM 给出）
            if plan.needs_more_data:
                info = plan.missing_info or ["上游信息不足（LLM 未给出具体缺失项）"]
                return _early_exit_needs_more_data(info)
            configs = _flatten_drafts(plan, normalized)
            assumptions = list(plan.assumptions or [])
            uncertainties = list(plan.uncertainties or [])
            used_llm = True
            source = "llm"
            log.info(f"[ScenarioDesign] LLM 设计了 {len(configs)} 个情景")
        except Exception as e:
            # LLM 不可用 → 确定性默认情景兜底，流程不崩
            log.info(f"[ScenarioDesign] LLM 设计失败，使用确定性默认情景兜底: {e}")
            configs = build_default_scenarios_tool(normalized)
            assumptions = ["LLM 不可用，情景为确定性默认模板（MOCK 兜底），非专家设计"]
            uncertainties = ["情景设计走了确定性兜底，未经 LLM 审查"]
            source = "default"

    # 公共 MOCK 不确定性说明（无论来源，都必须写明）
    uncertainties = [
        *list(uncertainties),
        f"当前 {normalized} 仿真模型为 MOCK 玩具模型（MOCK-v0.1），输出为 mock_ 前缀归一化合成指标，非真实地质灾害预测",
    ]

    return {
        "scenario_design_result": {
            "needs_more_data": False,
            "missing_info": [],
            "unsupported_hazard": False,
            "reason": "",
            "used_llm": used_llm,
            "source": source,
        },
        "scenario_configs": configs,
        "assumptions": assumptions,
        "uncertainties": uncertainties,
        "next": "scenario_validate",
    }
