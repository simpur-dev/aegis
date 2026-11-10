"""
Feedback Agent 的 LLM 工厂（集中转接）

【转接说明】项目 LLM 统一走根目录 llm.py（智谱 GLM-4-Flash，兼容层已内置）。
本文件只是 Feedback Agent 侧的转接层：节点只认 get_feedback_llm() 这个函数名。

【职责边界】LLM 仅用于问题解释 / 反思归因 / 建议生成（软性文本），
所有确定性检查（字段/版本/时间/状态/一致性）均由 Python 工具完成，
LLM 不可用时全流程仍可运行（各节点走确定性兜底）。
"""

from aegis.agents.hazards.llm import get_llm


def get_feedback_llm():
    """返回 Feedback Agent 使用的 LLM 实例（仅做解释/反思/建议，不做任何决策与校验）。"""
    return get_llm()
