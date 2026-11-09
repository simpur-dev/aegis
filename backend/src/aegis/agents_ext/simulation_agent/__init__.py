"""
西藏山地灾害多智能体系统 - Simulation Agent 模块
（西藏山地灾害多灾种风险推演 Agent）

【包声明，无逻辑】
  被全局 workflow/Manager 通过子模块 import（对齐 risk_agent 新约定）：
      from aegis.agents_ext.simulation_agent.graph import build_simulation_agent_graph, create_simulation_agent
      from aegis.agents_ext.simulation_agent.main import run_simulation_agent
      from aegis.agents_ext.simulation_agent.state import SimulationAgentState

【对外契约】
  run_simulation_agent(task, data_result, risk_result, hazard_type, ...) -> dict
      返回 simulation_result（最终交付物），模式对齐 run_risk_agent 只返回 risk_result。
  必填输入：task / data_result（Data Agent 最终 State 平铺）/
            risk_result（Risk Agent 最终 risk_result）/ hazard_type（标准键或中文别名）。

【诚实声明】
  当前五个灾种模型全部是 MOCK 玩具模型（MOCK-v0.1），
  输出为 mock_ 前缀归一化合成指标，不是真实地质灾害预测。
"""
