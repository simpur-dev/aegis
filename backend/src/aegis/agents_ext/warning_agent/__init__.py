"""
西藏山地灾害多智能体系统 - Warning Agent 模块
（面向五类山地灾害的预警规则执行、结构化预警对象生成与校验）

【本文件角色】包声明，无逻辑。

【被谁 import】全局 workflow / Manager 通过子模块 import：
    from aegis.agents_ext.warning_agent.graph import build_warning_agent_graph, create_warning_agent
    from aegis.agents_ext.warning_agent.main import run_warning_agent

【诚实声明 / 重要红线】
  1. 第一版只负责【生成和校验】预警对象，不执行真实发布；
     alert_status 恒为 draft / validation_failed / insufficient_data，
     不存在"已发布"语义（issued 仅作生命周期预留枚举）。
  2. 预警等级由灾种 Mock 规则映射产生（mapping_version=MOCK-v0.1），
     不输出红/橙/黄/蓝等官方等级（official_grade=pending_review）。
  3. LLM 仅将已确定的结构化结果转为可读说明，不得改变等级或编造事实。
"""
