"""
Risk Agent 的 LLM 工厂（集中收口）

【2026-10-17 更新】本文件预留的"上收到根 llm.py"TODO 已完成：
  模型统一切换为智谱 GLM-4-Flash（OpenAI 兼容接口），
  Key 与模型名在根目录 config.py 里配置（ZHIPUAI_API_KEY / ZHIPU_MODEL）。
  本文件现在只是 Risk Agent 侧的转接层：节点只认 get_risk_llm() 这个函数名，
  将来再换模型/供应商时本文件依旧一行不用改。

【对外契约】
  提供给：risk_assess_node.py / risk_validate_node.py
  不提供给：Data Agent / Simulation Agent（它们各自也走根 llm.py）
"""


# 把项目根目录加入 sys.path，保证 "import llm" 在任何运行方式下都能找到根目录的 llm.py

from aegis.agents_ext.llm import get_llm


def get_risk_llm():
    """返回 Risk Agent 使用的 LLM 实例（未绑定结构化输出，节点自己 .with_structured_output）。"""
    return get_llm()
