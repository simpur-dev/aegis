"""
Warning Agent 的共享状态定义（面向西藏五类山地灾害）。

【★ 交接契约 ★】
  ▶ 调用方（Manager / 独立测试）启动 Warning 时【必须填】：
      task          ← 任务描述
      hazard_type   ← 灾种（标准键或明确中文名；归一化后写回）
      risk_result   ← ★ Risk Agent 最终 risk_result（risk_output_node 的 final_output）
      simulation_result ← 可选；Simulation Agent 最终 simulation_result（无则跳过）
      data_result   ← 可选；Data Agent 最终 State（用于数据充分性核验）

  ▶ 产出、【提供给 Manager / 通知模块 / Feedback Agent】：
      warning_result ← ★ 最终交付物（结构化预警对象 + 解释 + 校验结果）
      status         ← draft / validation_failed / insufficient_data
"""

from typing import TypedDict


class WarningAgentState(TypedDict):
    """Warning Agent 的全局状态，贯穿整个预警生成流程"""

    # === 输入（只读）===
    task: str  # 任务描述
    hazard_type: str  # 灾种（调用方填原始值；决策节点归一化后写回标准键）
    risk_result: dict  # Risk Agent 最终交付物（含 assessment/R/rule_hits/model_info）
    simulation_result: dict  # Simulation Agent 最终交付物（可选，空 dict 表示未提供）
    data_result: dict  # Data Agent 最终 State（可选，用于数据充分性核验）

    # === 决策阶段（warning_decision 节点写入）===
    decision_result: dict  # 是否具备生成预警条件的判定 {can_warn, reasons, checks}

    # === 规则映射阶段（hazard_warning_rule 节点写入）===
    warning_rule_result: dict  # 灾种规则映射结果 {warning_level, level_basis, mapping_version,
    #                   hits, params_used, escalation}
    # === 告警对象构建（alert_builder 节点写入）===
    alert_object: dict  # 结构化预警对象（生命周期/Mock 标记/预留字段齐全）

    # === 解释阶段（llm_explanation 节点写入）===
    explanation: dict  # 可读预警说明 {text, bullet_points, used_llm}

    # === 校验阶段（warning_validator 节点写入）===
    validation_result: dict  # 消息校验 {is_valid, issues, recommendation: proceed/retry/fail}

    # === 控制流 ===
    retry_count: int  # 解释重试计数（bounded）
    max_retries: int  # 默认 3
    next: str  # 记录性路由标识
    error_message: str  # 错误信息

    # === 输出 ===
    status: str  # draft / validation_failed / insufficient_data
    warning_result: dict  # ★ 最终交付物（结构化预警对象 + 解释 + cap 预留）
