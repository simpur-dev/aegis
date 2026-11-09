"""
MemoryStore 节点（反馈经验存档）

【本文件角色】
  把本次反馈条目（含反思结论与改进建议）写入经验库（第一版 JSON 文件），
  每条记录保留来源、版本与审核状态；外部反馈记录一并校验后存档。

【审核状态语义】（详见 feedback_memory_tool 红线说明）
  auto_validated：确定性高置信且无需人工复核（仍不等于专业验证）；
  pending_review：证据不足或涉专业判断，须人工审核后才能用于模型改进。

【对外契约】
  读取：feedback_items / reflection_result / improvement_suggestions /
        collected_context / external_feedback
  写入：memory_result / next
"""

from typing import Any

from aegis.agents_ext.feedback_agent.state import FeedbackAgentState
from aegis.agents_ext.feedback_agent.tools.feedback_memory_tool import (
    make_feedback_record,
    save_external_feedback,
    save_feedback_records,
    validate_external_feedback,
)


def memory_store_node(state: FeedbackAgentState) -> dict[str, Any]:
    """记忆存储节点（确定性；只写 Feedback 自己的经验库，不碰上游结果）。"""
    context = state.get("collected_context", {}) or {}
    agents_meta = context.get("agents", {}) or {}
    model_version = (agents_meta.get("simulation", {}) or {}).get("model_version", "")
    rule_version = (agents_meta.get("risk", {}) or {}).get("rule_version", "")

    reflections = (state.get("reflection_result", {}) or {}).get("reflections", []) or []
    refl_by_id = {r.get("feedback_id", ""): r for r in reflections if isinstance(r, dict)}
    sugg_by_id = {s.get("feedback_id", ""): s for s in (state.get("improvement_suggestions", []) or [])}

    # ---------- 1) 反馈条目 → 经验记录 ----------
    records = []
    for item in state.get("feedback_items", []) or []:
        if item.get("severity") == "info":  # 纯提示类不入库，避免经验库噪音
            continue
        refl = refl_by_id.get(item.get("feedback_id", ""), {})
        sugg = sugg_by_id.get(item.get("feedback_id", ""), {})
        records.append(
            make_feedback_record(
                event_id=state.get("event_id", ""),
                hazard_type=context.get("hazard_type_normalized", "") or state.get("hazard_type", ""),
                item=item,
                reflection=refl,
                suggested_action=sugg.get("suggested_action", ""),
                model_version=model_version,
                rule_version=rule_version,
            )
        )

    memory_result = (
        save_feedback_records(records)
        if records
        else {"saved_count": 0, "record_ids": [], "total_records": 0, "note": "本次无可入库的问题经验"}
    )

    # ---------- 2) 外部反馈存档（格式合格的入库；不合格的如实记录） ----------
    external_saved = []
    ext = state.get("external_feedback")
    if ext:
        ext_records = ext if isinstance(ext, list) else [ext]
        for rec in ext_records:
            if isinstance(rec, dict) and not validate_external_feedback(rec):
                external_saved.append(save_external_feedback(rec))
            else:
                # 不合格记录也存档（含 format_issues），供人工补录
                external_saved.append(save_external_feedback(rec if isinstance(rec, dict) else {"raw": str(rec)}))
    memory_result["external_feedback_saved"] = external_saved

    print(f"[MemoryStore] 入库经验记录数={memory_result.get('saved_count', 0)} 外部反馈存档数={len(external_saved)}")

    return {
        "memory_result": memory_result,
        "next": "feedback_output",
    }
