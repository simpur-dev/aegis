"""
Manager Agent 的 LLM 工厂（集中转接）

【转接说明】项目 LLM 统一走根目录 llm.py（智谱 GLM-4-Flash，兼容层已内置）。
本文件只是 Manager Agent 侧的转接层：节点只认 get_manager_llm() 这个函数名。

【职责边界】LLM 仅用于：自然语言任务的理解辅助、异常解释文本、最终报告润色。
灾种合法性 / Agent 依赖 / 重试上限 / 停止条件 / 预警等级等控制动作全部由
确定性 Python 完成；LLM 不可用时全流程仍可运行（各节点确定性兜底）。
"""

from aegis.agents_ext.llm import get_llm


def get_manager_llm():
    """返回 Manager 使用的 LLM 实例（仅做理解/解释/润色，不做任何调度决策）。"""
    return get_llm()
