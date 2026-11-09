"""
Simulation Agent 的共享状态定义。

【与 Data / Risk Agent State 的对照】
  机制完全相同：节点读 state、return 局部合并（LangGraph 标准玩法），
  TypedDict + 无 reducer，字段按 输入 / 阶段产出 / 控制流 / 输出 分组。

【★ 交接契约 —— 哪些字段给谁 ★】

  ▶ 调用方（将来的 Manager / 现在的 main.py）启动 Simulation 时【必须填】：
      simulation_task      ← 推演任务描述
      data_result          ← Data Agent 的最终完整 State（平铺结构，只读）
      risk_result          ← Risk Agent 的最终 risk_result（risk_output_node 组装的那份，只读）
      hazard_type          ← ★ 必填 ★ 灾种（标准键或明确中文名，如"泥石流"）；
                              hazard_model_router 节点会把它覆写为归一化标准键
                              （全 Agent 唯一允许被覆写的输入字段）
      preset_scenario_configs  ← 可选；外部直传情景，非空时跳过 LLM 情景设计
      max_retries / timeout_seconds  ← 可选，默认 3 / 60.0

  ▶ 内部流转字段（调用方不用管）：
      scenario_design_result, scenario_configs, assumptions, uncertainties,
      validated_scenarios, validation_result, routing_result,
      simulation_results, execution_result, comparison_result,
      retry_count, next, error_message

  ▶ Simulation 产出、【提供给下游 Warning Agent / Manager】的字段：
      simulation_result    ← ★ 最终交付物（simulation_output_node 组装）★
      status               ← ★ 四值：success / uncertain / insufficient_data / failed ★
"""

from typing import TypedDict


class SimulationAgentState(TypedDict):
    """Simulation Agent 的全局状态，贯穿整个推演流程"""

    # === 输入（由调用方填充，除 hazard_type 外只读）===
    simulation_task: str  # 推演任务描述，如"推演强降雨下某沟域泥石流情景"
    data_result: dict  # Data Agent 最终 State 平铺（processed_data / data_quality / missing_data / status...）
    risk_result: dict  # Risk Agent 最终 risk_result（risk_level / evidence / uncertainties...）
    hazard_type: str  # 灾种：调用方填原始值；hazard_model_router 节点写回归一化标准键
    preset_scenario_configs: list  # 可选：外部直传情景配置（非空时 design 节点跳过 LLM）

    # === 情景设计阶段（scenario_design 节点写入）===
    scenario_design_result: dict  # 设计阶段元判定 {needs_more_data, missing_info, unsupported_hazard, reason, used_llm, source}
    # >>> route_after_scenario_design 读它分流（数据缺失/灾种不支持 → 直送输出）
    scenario_configs: list  # 情景配置列表（扁平 dict：公共字段 + 灾种参数平铺）
    assumptions: list  # 情景假设清单（每条带来源标签 observed/analysis/assumption）
    uncertainties: list  # 限制与不确定性说明（含 MOCK 模型局限）

    # === 情景校验阶段（scenario_validate 节点写入）===
    validated_scenarios: list  # 通过确定性校验的情景子集（normalized）
    validation_result: dict  # 情景校验报告 {is_valid, recommendation: proceed/retry/fail,
    #                invalid_details, issues, deterministic_check, llm_review}
    # >>> 这是路由核心字段之一，对齐 Risk Agent 的用法

    # === 模型路由阶段（hazard_model_router 节点写入）===
    routing_result: dict  # 路由判定 {hazard_type_normalized, supported, unsupported_reason,
    #            inputs_ok, missing_inputs, model_version, disclaimer}

    # === 仿真执行阶段（simulation_execute 节点写入）===
    simulation_results: list  # 逐情景统一 Schema 结果（steps 时间步 + indicators + disclaimer）
    execution_result: dict  # 执行层元结果 {ok, attempt, error, failures, scenario_ok_count,
    #               scenario_fail_count, duration_seconds}
    # >>> 仿真异常重试的判定依据

    # === 结果分析阶段（results_analysis 节点写入）===
    comparison_result: dict  # 分析交付物 {comparison_table, ranking, metric_extremes,
    #             scenario_differences, key_findings, worst_scenario_id,
    #             narrative_summary, implications, limitations,
    #             llm_used, completeness:{is_complete, missing_items, fixable}}

    # === 控制流（循环、异常、路由控制）===
    retry_count: int  # 全局共享重试预算：产生 retry 判定的节点在同一 return 里自增，
    # 路由读它判断 retry_count < max_retries（修正 Risk Agent 恒不自增的缺陷模式）
    max_retries: int  # 重试上限，默认 3（对齐 Data / Risk Agent）
    timeout_seconds: float  # 单情景仿真超时（秒），默认 60（Mock 毫秒级完成）
    next: str  # 记录性路由标识（实际路由以各 validation/routing 字段为准，镜像现有写法）
    error_message: str  # 最近一次错误文本（输出节点透传进 metadata）

    # === 输出（simulation_output 节点写入，交给下游 Warning Agent / Manager）===
    status: str  # 四值：success / uncertain / insufficient_data / failed（对齐 Risk Agent）
    simulation_result: dict  # ★ 最终交付物（final_output 整体），run_simulation_agent 只返回它
