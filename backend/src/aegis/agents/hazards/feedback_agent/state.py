"""
Feedback Agent 的共享状态定义（面向西藏五类山地地质灾害）。

【定位】质量检查 / 问题追溯 / 经验积累模块：
  只读取 Data / Risk / Simulation / Warning 的最终交付物，
  输出结构化反馈 feedback_result 给 Manager，绝不修改上游结果。

【★ 交接契约 —— 哪些字段给谁 ★】
  ▶ Manager 启动 Feedback 时【必须填】的输入字段：
      event_id            ← 本次预警事件的业务标识（用于经验记录与外部反馈关联）
      hazard_type         ← ★ 必填 ★ 灾种（标准键或明确中文名）；标准键见根目录 hazard_registry.py
      data_result         ← Data Agent 最终平铺 State（processed_data / data_quality / ...）
      risk_result         ← Risk Agent 最终 risk_result（risk_output_node 的 final_output）
      simulation_result   ← 可选；Simulation Agent 最终 simulation_result
      warning_result      ← 可选；Warning Agent 最终 warning_result
      external_feedback   ← 可选；外部实际反馈记录（接口见 tools/feedback_memory_tool）

  ▶ Feedback 产出、【提供给 Manager】的字段：
      feedback_result     ← ★ 最终交付物（结构化反馈 + manager_actions + 安全声明）★
      status              ← no_issues / issues_found / needs_review /
                            insufficient_evidence / failed
"""

from typing import TypedDict


class FeedbackAgentState(TypedDict):
    """Feedback Agent 的全局状态，贯穿整个反馈流程"""

    # === 输入（由 Manager 填充，只读，绝不修改）===
    task: str  # 任务描述
    event_id: str  # 事件业务标识（经验记录与外部反馈的关联键）
    hazard_type: str  # ★ 必填 ★ 灾种（标准键或明确中文名）
    data_result: dict  # Data Agent 最终平铺 State
    risk_result: dict  # Risk Agent 最终 risk_result
    simulation_result: dict  # Simulation Agent 最终 simulation_result（可选）
    warning_result: dict  # Warning Agent 最终 warning_result（可选）
    external_feedback: dict  # 外部实际反馈（观测时间/来源/区域/内容/可靠度/审核状态/事件号）

    # === 收集阶段（feedback_collect 节点写入）===
    collected_context: dict  # 上游快照：各 Agent 的存在性/状态/灾种/模型版本/规则版本/时间戳

    # === 分析 + 评估阶段（analyze / evaluate 节点写入）===
    detected_issues: list  # 确定性检查发现的问题清单（结构化 issue 记录）
    analysis_notes: dict  # LLM 对检查结果的解释性说明（仅解释，不判定问题存在性）
    feedback_items: list  # evaluate 分类标注后的反馈条目（含 severity/confidence/needs_review）
    evaluation_result: dict  # 评估汇总 {has_issues, recommendation: pass/reflect/insufficient/output, ...}

    # === 反思 / 建议 / 记忆阶段（借鉴 Reflexion）===
    reflection_result: dict  # 问题归因分析（含 evidence_sufficient 标记，不足时不得编造原因）
    improvement_suggestions: list  # 具体到 Agent/组件 的改进建议
    suggestion_used_llm: bool  # 建议环节是否使用了 LLM（来源可追溯）
    historical_feedback: list  # 从经验库检索的同灾种历史反馈（只读参考）
    needs_another_round: bool  # 反思回环请求（受 max_iterations 限界）
    memory_result: dict  # 经验存储结果 {saved_count, record_ids, store_path}

    # === 控制流 ===
    iteration_count: int  # 反思已执行轮数（每进入 reflection 自增一次）
    max_iterations: int  # 反思最大轮数，默认 2（防无限循环）
    next: str  # 记录性路由标识（实际路由以 evaluation_result 等为准）
    error_message: str  # 错误信息

    # === 输出（feedback_output 节点写入，交给 Manager）===
    status: str  # no_issues / issues_found / needs_review / insufficient_evidence / failed
    feedback_result: dict  # ★ 最终交付物（不含对上游结果的任何修改）★
    test_hooks: dict  # 测试钩子（项目既有契约风格，见 ScenarioConfig）：
    #   {"force_needs_another_round": True} 强制请求额外反思轮，
    #   用于验证反思回环的有界性；正常流程恒为空。
