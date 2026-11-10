"""
DataOutput 节点（面向西藏五类山地地质灾害）

职责：
  - 组装最终结构化输出（携带 hazard_type / 数据来源 / Mock 标记 / 数据充分性）
  - 标记成功/失败状态
  - 将结果写入共享 State，供下游 Risk Agent 使用

设计思想：
  Data Agent 不生成自然语言报告，只输出结构化数据。
  下游 Agent 自行决定如何使用这些数据。
"""

from typing import Any

from aegis.agents.hazards.data_agent.state import DataAgentState


def data_output_node(state: DataAgentState) -> dict[str, Any]:
    """数据输出节点：组装结构化结果（含灾种与数据充分性标记）。"""

    data_quality = state.get("data_quality", {})
    processed_data = state.get("processed_data", {})
    missing_data = state.get("missing_data", [])
    error_message = state.get("error_message", "")
    hazard_type = state.get("hazard_type", "")

    # ---------- 判断最终状态 ----------
    is_valid = data_quality.get("is_valid", False)
    if is_valid:
        status = "success"
    elif error_message:
        status = "failed"
    else:
        status = "partial"  # 数据部分可用

    # ---------- 数据充分性评估（是否足以支撑后续风险评估）----------
    if is_valid:
        data_sufficiency = {
            "sufficient": True,
            "reason": data_quality.get("validation_reason", "数据质量合格"),
        }
    elif error_message:
        data_sufficiency = {"sufficient": False, "reason": f"数据获取失败: {error_message}"}
    else:
        data_sufficiency = {
            "sufficient": False,
            "reason": (
                f"数据部分可用: {data_quality.get('validation_reason', '质量未达标')}；缺失: {missing_data if missing_data else '无明细'}"
            ),
        }

    # ---------- processed_data 自描述化（下游可独立读取灾种与 Mock 标记）----------
    processed_data_out = dict(processed_data) if isinstance(processed_data, dict) else {}
    processed_data_out["hazard_type"] = hazard_type
    processed_data_out["is_mock"] = processed_data_out.get("data_source") == "mock"

    print(f"[DataOutput] status={status} hazard_type={hazard_type} sufficient={data_sufficiency['sufficient']}")

    return {
        "hazard_type": hazard_type,  # >>> 归一化标准键透传给下游 Risk Agent
        "processed_data": processed_data_out,
        "data_quality": data_quality,
        "missing_data": missing_data,
        "data_sufficiency": data_sufficiency,  # >>> 数据是否足以支撑风险评估
        "status": status,
        "error_message": error_message,
        "next": "end",
    }
