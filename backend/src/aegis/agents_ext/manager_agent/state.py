"""
Manager Agent 全局状态（多智能体调度中心）

【定位】这是全项目唯一的全局调度 State（workflow/state.py 对本模块薄再导出）。
  只保存任务上下文、执行进度、五个 Agent 的交付物引用与控制流字段；
  不把各 Agent 内部 State 字段复制进来 —— 专业 Agent 保留自己的局部 State，
  Manager 只通过交付物 dict（data_result 等）只读传递与核验。

【红线】Manager 不修改任何 Agent 交付物的业务内容（风险分数、预警等级、
  权重阈值、模拟结果、Mock 标识）。result_check 只读核验并记录状态。
"""

from typing import TypedDict

# ============================================================
# 被调度的五个专业 Agent（标准名，与目录名一致）
# ============================================================

AGENT_DATA = "data_agent"
AGENT_RISK = "risk_agent"
AGENT_SIMULATION = "simulation_agent"
AGENT_WARNING = "warning_agent"
AGENT_FEEDBACK = "feedback_agent"

AGENT_ORDER = [AGENT_DATA, AGENT_RISK, AGENT_SIMULATION, AGENT_WARNING, AGENT_FEEDBACK]

AGENT_CN_NAME = {
    AGENT_DATA: "数据智能体",
    AGENT_RISK: "风险智能体",
    AGENT_SIMULATION: "模拟推演智能体",
    AGENT_WARNING: "预警智能体",
    AGENT_FEEDBACK: "反馈智能体",
}

# 各 Agent 交付物在 ManagerState 中的存放键
AGENT_RESULT_KEY = {
    AGENT_DATA: "data_result",  # Data 返回最终平铺 State（全量保留）
    AGENT_RISK: "risk_result",
    AGENT_SIMULATION: "simulation_result",
    AGENT_WARNING: "warning_result",
    AGENT_FEEDBACK: "feedback_result",
}

# 各 Agent 交付物 status 的"执行成功"取值（以各 Agent 实际代码为准）：
#   Data: success/partial（partial 时再看 data_sufficiency，见 result_check）
#   Risk / Simulation: success / uncertain（uncertain=完成但带不确定性声明）
#   Warning: draft（第一版只出草稿即视为成功）
#   Feedback: no_issues / issues_found / needs_review / insufficient_evidence
#             （均为"正常跑完"，问题与否由 feedback_review 解读）
SUCCESS_STATUSES = {
    AGENT_DATA: {"success"},
    AGENT_RISK: {"success", "uncertain"},
    AGENT_SIMULATION: {"success", "uncertain"},
    AGENT_WARNING: {"draft"},
    AGENT_FEEDBACK: {"no_issues", "issues_found", "needs_review", "insufficient_evidence"},
}
PARTIAL_STATUSES = {AGENT_DATA: {"partial"}}

# ============================================================
# 任务类型（六类）
# ============================================================

TASK_TYPES = ["data_only", "risk_only", "simulation_only", "warning_only", "full_analysis", "feedback_only"]

# ============================================================
# 任务终态（第十二节；result_check / finalize 只能从这里取值）
# ============================================================

TASK_COMPLETED = "completed"
TASK_PARTIALLY_COMPLETED = "partially_completed"
TASK_INSUFFICIENT_DATA = "insufficient_data"
TASK_FAILED = "failed"
TASK_NEED_CLARIFICATION = "need_clarification"
TASK_UNSUPPORTED_HAZARD = "unsupported_hazard"
TASK_NEEDS_HUMAN_REVIEW = "needs_human_review"

# ============================================================
# 错误类型（第九节；error_history[].error_type 的受控词表）
# ============================================================

ERROR_TASK_VALIDATION = "TASK_VALIDATION_ERROR"
ERROR_UNSUPPORTED_HAZARD = "UNSUPPORTED_HAZARD"
ERROR_AGENT_EXECUTION = "AGENT_EXECUTION_ERROR"
ERROR_AGENT_TIMEOUT = "AGENT_TIMEOUT"
ERROR_INVALID_OUTPUT = "INVALID_AGENT_OUTPUT"
ERROR_HAZARD_MISMATCH = "HAZARD_TYPE_MISMATCH"
ERROR_MISSING_DATA = "MISSING_REQUIRED_DATA"
ERROR_STALE_DATA = "STALE_DATA"
ERROR_SIMULATION_FAILURE = "SIMULATION_FAILURE"
ERROR_WARNING_VALIDATION = "WARNING_VALIDATION_FAILURE"
ERROR_FEEDBACK_REVIEW = "FEEDBACK_REVIEW_FAILURE"
ERROR_MAX_RETRIES = "MAX_RETRIES_EXCEEDED"
ERROR_MISSING_DEPENDENCY = "MISSING_DEPENDENCY"

# retry_decision 的受控动作集合
RETRY_ACTIONS = [
    "retry_current_agent",
    "call_next_agent",
    "skip_optional_agent",
    "stop_with_insufficient_data",
    "stop_with_error",
    "require_human_review",
]


class ManagerState(TypedDict, total=False):
    """Manager 全局调度状态（节点返回值按键局部合并）。

    所有节点顺序执行（hub-and-spoke 单线程调度），不存在并发写同一键的情况，
    因此不需要 reducer；累积型列表（error_history / agent_call_log 等）由
    唯一责任节点写入。
    """

    # === 任务信息 ===
    event_id: str  # 任务业务标识（同时用作 Checkpoint thread_id）
    user_request: str  # 用户原始输入文本
    task_type: str  # 六类之一（空则按 full_analysis）
    hazard_type: str  # 标准灾种键（task_analyze 归一化后写入）
    region: str  # 仅记入上下文（Data Agent 不按此过滤，报告中说明）
    analysis_time: str  # 仅记入上下文（同上）
    require_simulation: bool  # 用户明确要求模拟为强制环节（覆盖 SIMULATION_OPTIONAL）
    llm_polish: bool  # 最终报告是否启用 LLM 润色（默认关；失败自动兜底）

    # === 执行计划与进度 ===
    execution_plan: list  # 本任务计划 ["data_agent", ...]
    pending_agents: list  # 调度队列（dispatch 弹出队首执行）
    current_agent: str  # 本轮 dispatch 正在执行 / 刚执行的 Agent
    completed_agents: list  # 已成功完成（含重试后成功）
    skipped_agents: list  # 按配置跳过的可选环节（含模拟失败跳过）
    failed_agents: list  # 最终失败的 Agent

    # === 五个 Agent 交付物（只读透传，Manager 不改业务内容）===
    data_result: dict
    risk_result: dict
    simulation_result: dict
    warning_result: dict
    feedback_result: dict

    # === Simulation 直传情景（集成测试确定性用；正常任务为空）===
    preset_scenario_configs: list

    # === 执行状态与日志 ===
    agent_status: dict  # agent -> 交付物 status 原值 / "provided" / "skipped"
    agent_call_log: list  # 每次调用一条 {agent, attempt, status, error}
    check_result: dict  # result_check 结论 {verdict, agent, error_type, message}

    # === 异常与重试 ===
    error_message: str
    error_history: list  # [{agent, error_type, message, attempt}]
    retry_counts: dict  # agent -> 已重试次数

    # === Feedback 返工 ===
    rerun_plan: list  # feedback_review 制定的最小返工计划
    feedback_loop_count: int  # 已进入返工的轮数
    feedback_decisions: list  # 每轮反馈处置决策记录（pass/rerun/human_review/loop_limit_stop）
    human_review_required: bool

    # === 控制流 ===
    next: str

    # === 最终输出 ===
    task_status: str  # 只能取上方 TASK_* 之一
    final_report: dict

    # === 测试钩子（仅测试注入，生产不启用；沿用项目既有约定）===
    test_hooks: dict
