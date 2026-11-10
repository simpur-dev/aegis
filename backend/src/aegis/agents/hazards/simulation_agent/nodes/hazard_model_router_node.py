"""
HazardModelRouter 节点 —— 灾种模型路由

【本文件角色】
  权威灾种归一化（写回 state["hazard_type"] 为标准键）→ 支持性判定
  → 模型上下文齐备性检查（risk_result 是否存在）→ 写 routing_result。

【对外契约】
  读取：hazard_type（原始值）/ risk_result
  写入：hazard_type（★ 覆写为归一化标准键，全 Agent 唯一被覆写的输入字段）/
        routing_result / next
  路由：route_after_router 读 routing_result 的 supported / inputs_ok
        （支持且齐备 → 执行节点；否则 → 直送输出并给出明确原因）。

【红线】
  灾种无法识别时不得到相近灾种，必须给出含支持列表的明确原因。
  实际的模型选择发生在 simulation_execute_node（get_model），
  本节点只做"选哪个模型"的判定与记录（对齐任务 §四"模型路由"职责）。
"""

import logging

from aegis.agents.hazards.simulation_agent.tools.hazard_model_router import (
    get_model_requirements,
    normalize_hazard_type,
    supported_hazards_text,
)

log = logging.getLogger("aegis.agents.hazards.simulation_agent.nodes.hazard_model_router_node")


def hazard_model_router_node(state: dict) -> dict:
    """灾种模型路由节点。"""
    hazard_raw = state.get("hazard_type", "") or ""
    risk_result = state.get("risk_result", {}) or {}

    # ---------- 1) 权威归一化 ----------
    normalized = normalize_hazard_type(hazard_raw)
    if not normalized:
        reason = f"灾种'{hazard_raw}'无法识别。当前支持：{supported_hazards_text()}"
        log.info(f"[HazardModelRouter] {reason}")
        return {
            "hazard_type": hazard_raw,  # 识别失败保持原值（输出节点会透传原因）
            "routing_result": {
                "hazard_type_normalized": "",
                "supported": False,
                "unsupported_reason": reason,
                "inputs_ok": False,
                "missing_inputs": [],
                "model_version": "",
                "disclaimer": "",
            },
            "next": "simulation_output",
        }

    # ---------- 2) 模型上下文齐备性 ----------
    missing_inputs = []
    if not risk_result:
        missing_inputs.append(
            {
                "field": "risk_result",
                "source": "Risk Agent",
                "reason": "缺少风险研判结果，仿真缺少风险上下文",
            }
        )
    inputs_ok = not missing_inputs

    # ---------- 3) 模型元信息（supported → 不会抛异常）----------
    req = get_model_requirements(normalized)

    log.info(f"[HazardModelRouter] 灾种={normalized}（{req['hazard_type_cn']}） 模型={req['model_version']} inputs_ok={inputs_ok}")

    return {
        "hazard_type": normalized,  # >>> ★ 权威写回标准键
        "routing_result": {
            "hazard_type_normalized": normalized,
            "supported": True,
            "unsupported_reason": "",
            "inputs_ok": inputs_ok,
            "missing_inputs": missing_inputs,
            "model_version": req["model_version"],
            "disclaimer": req["disclaimer"],
        },
        "next": "simulation_execute" if inputs_ok else "simulation_output",
    }
