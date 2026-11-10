"""
西藏山地灾害多智能体系统 - Risk Agent 模块
（面向五类山地灾害的触发条件识别与风险评估，本项目不研究地震）

【本文件角色】包声明，无逻辑。

【被谁 import】全局 workflow / Manager 通过子模块 import：
    from aegis.agents.hazards.risk_agent.graph import build_risk_agent_graph, create_risk_agent

【解耦原则】本包【不直接 import Data Agent 的任何代码】。
    两个 Agent 只通过 State 里的 data_result 字段交接数据，
    不互相 import。将来改 Data 内部逻辑不影响 Risk，反之亦然。
    灾种标准键与别名统一定义在根目录 hazard_registry.py。
"""
