"""
task_analyze_node：任务解析与灾种识别（全确定性，LLM 不参与灾种判定）

【规则（任务书第五节）】
  - hazard_type 必须由调用方显式传入（标准键或明确中文名），
    经 hazard_registry.normalize_hazard_type 确定性归一化；
  - 未提供且无法从上下文确定 → status=need_clarification（绝不猜测）；
  - 不属于五类 → status=unsupported_hazard；
  - task_type 缺失按 full_analysis；非法取值 → need_clarification。
"""

import re
from datetime import UTC, datetime

from aegis.agents.hazards.hazard_registry import normalize_hazard_type, supported_hazards_text
from aegis.agents.hazards.manager_agent.state import (
    ERROR_TASK_VALIDATION,
    ERROR_UNSUPPORTED_HAZARD,
    TASK_NEED_CLARIFICATION,
    TASK_TYPES,
    TASK_UNSUPPORTED_HAZARD,
)

_TERMINAL = (TASK_NEED_CLARIFICATION, TASK_UNSUPPORTED_HAZARD)


def _gen_event_id(hazard_key: str) -> str:
    ts = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    tail = re.sub(r"[^a-z0-9]", "", hazard_key.lower())[:6] or "hazard"
    return f"EVT-{tail}-{ts}"


def task_analyze_node(state: dict) -> dict:
    """解析任务输入，验证灾害类型和必要字段。"""
    updates: dict = {"error_history": list(state.get("error_history") or []), "check_result": {}}

    # ---- 1. 灾种识别（确定性归一化；空/未知都不进入计划阶段）----
    raw_hazard = (state.get("hazard_type") or "").strip()
    hazard_key = normalize_hazard_type(raw_hazard)
    if not raw_hazard:
        updates["task_status"] = TASK_NEED_CLARIFICATION
        updates["error_message"] = "请明确需要分析的灾害类型。"
        updates["error_history"].append({"agent": "manager", "error_type": ERROR_TASK_VALIDATION, "message": "灾种未提供", "attempt": 0})
    elif not hazard_key:
        updates["task_status"] = TASK_UNSUPPORTED_HAZARD
        updates["error_message"] = f"当前系统仅支持五类指定的山地地质灾害；灾种'{raw_hazard}'不在范围内。支持：{supported_hazards_text()}"
        updates["error_history"].append(
            {"agent": "manager", "error_type": ERROR_UNSUPPORTED_HAZARD, "message": f"灾种'{raw_hazard}'不属于五类标准灾种", "attempt": 0}
        )
    else:
        updates["hazard_type"] = hazard_key

    # ---- 2. 任务类型 ----
    task_type = (state.get("task_type") or "").strip() or "full_analysis"
    if task_type not in TASK_TYPES:
        updates["task_status"] = TASK_NEED_CLARIFICATION
        updates["error_message"] = f"task_type='{task_type}' 不在支持范围，可选：{'/'.join(TASK_TYPES)}"
        updates["error_history"].append(
            {"agent": "manager", "error_type": ERROR_TASK_VALIDATION, "message": f"非法 task_type: {task_type}", "attempt": 0}
        )
    elif "task_status" not in updates:
        updates["task_type"] = task_type

    # ---- 3. event_id（同时是 Checkpoint thread_id）----
    event_id = (state.get("event_id") or "").strip()
    if "task_status" not in updates:
        updates["event_id"] = event_id or _gen_event_id(hazard_key)

    print(
        f"[Manager/Analyze] 灾种={updates.get('hazard_type', hazard_key or '(无效)')} "
        f"| 任务类型={updates.get('task_type', task_type)} "
        f"| event_id={updates.get('event_id', event_id)} "
        f"| 结论={updates.get('task_status', '解析通过')}"
    )
    return updates
