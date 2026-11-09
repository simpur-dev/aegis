"""
Warning Agent 的 LLM 工厂（集中收口）

【2026-10-17 起】项目 LLM 统一走根目录 llm.py（智谱 GLM-4-Flash，兼容层已内置）。
本文件只是 Warning Agent 侧的转接层：节点只认 get_warning_llm() 这个函数名。
"""

from aegis.agents_ext.llm import get_llm


def get_warning_llm():
    """返回 Warning Agent 使用的 LLM 实例（仅用于预警文本解释，不做任何决策）。"""
    return get_llm()
