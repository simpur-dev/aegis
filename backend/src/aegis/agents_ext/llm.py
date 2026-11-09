"""
项目统一 LLM 工厂（收口点）

【本文件角色】
  全项目所有 Agent（Data / Risk / Simulation）的 LLM 唯一来源。
  当前指向智谱开放平台的 GLM-4-Flash（OpenAI 兼容接口，
  仍用 langchain_openai.ChatOpenAI 直连，无需换 SDK / 无需新依赖）。

【API Key 配置】
  Key 本身不在本文件：到根目录 config.py 的 ZHIPUAI_API_KEY 里粘贴
  （或设置环境变量 ZHIPUAI_API_KEY，环境变量优先）。
  没填 Key 时本工厂不报错：返回一个占位 Key 的实例，
  真正调用时 401 失败 → 各节点既有 try/except 会打印原因并走确定性兜底，
  流程不崩；错误信息里会带上"去哪里填 Key"的指引。

【★ 智谱 GLM 的结构化输出兼容层（2026-10-17 实测结论）★】
  智谱 OpenAI 兼容接口对 glm-4-flash 的实测行为：
    1. method="json_schema"（langchain 默认，OpenAI 专有 response_format）：
       智谱不遵守 → 模型用自然语言回答 → 解析报 Invalid JSON（用户最初遇到的报错）。
    2. tool_choice 强制调用【无效】：无论传 "auto"/工具名/对象格式，
       模型都可能不调用工具、直接自然语言回答（真实 OpenAI 上 tool_choice=工具名
       是强制的，智谱兼容接口忽略了它）→ with_structured_output 返回 None。
    3. function_calling（工具定义）：支持，模型"看提示词心情"决定是否调用
       （Data Agent 的规划提示词稳定调用；Risk 的评估提示词曾两次不调用）。
    4. response_format={"type":"json_object"}：智谱遵守，服务端强制输出合法 JSON。

  因此本文件提供 ZhipuCompatChatOpenAI，采用【双通道】策略：
    首选 function_calling（对工具调用友好的提示词最精准）；
    若模型没调工具（结果为 None）→ 自动注入 Pydantic 格式指令，
    回退到服务端强制 JSON 模式解析；仍失败则抛出明确异常，
    由各节点既有 try/except 兜底。全部 6 个结构化输出调用点一行不用改。

【历史说明】
  2026-10-17 之前：三个 Agent 各自 new ChatOpenAI(model="gpt-4o-mini")。
  2026-10-17：统一上收到本文件，模型切换为智谱 GLM-4-Flash。
  以后换模型/换供应商只改 config.py（或本文件），所有节点一行不用动。

【对外契约】
  get_llm() -> ChatOpenAI 实例（未绑定结构化输出，由各节点自己 .with_structured_output）
  提供给：data_agent/nodes/data_plan_node.py、risk_agent/llm_setup.py、
          simulation_agent/llm_setup.py
"""

import os

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.output_parsers import PydanticOutputParser
from langchain_core.runnables import RunnableLambda
from langchain_openai import ChatOpenAI
from pydantic import BaseModel

from aegis.agents_ext import config

# Key 未配置时的占位值：调用 API 时必然 401。
# ★ 必须是纯 ASCII（HTTP 头无法编码中文），但内容仍是"去哪里填 Key"的指引
_PLACEHOLDER_KEY = (
    "ZHIPU_KEY_NOT_SET: paste your Zhipu API key into config.py (ZHIPUAI_API_KEY at project root), or run: setx ZHIPUAI_API_KEY your_key"
)


class ZhipuCompatChatOpenAI(ChatOpenAI):
    """智谱 GLM 的结构化输出兼容层（双通道：function_calling → 强制 JSON）。

    详见文件头【智谱 GLM 的结构化输出兼容层】小节的实测结论。
    节点调用方式不变：llm.with_structured_output(Pydantic模型).invoke(messages)。
    """

    def with_structured_output(self, schema, **kwargs):
        """返回与 langchain 兼容的结构化输出 Runnable。

        通道一：function_calling（langchain 原生实现，模型愿意调工具时最精准）。
        通道二（回退）：服务端强制 JSON（response_format=json_object，
        智谱遵守）+ Pydantic 格式指令注入，模型无法输出非 JSON 内容。
        两通道都失败 → 抛 RuntimeError（节点 try/except 兜底并打印明确原因）。
        """
        # ---- 通道一：function_calling（模型不调工具时返回 None，不抛异常）----
        base_fc = super().with_structured_output(schema, method="function_calling", **kwargs)

        # ---- 通道二准备：格式指令 + 服务端强制 JSON ----
        is_pydantic = isinstance(schema, type) and issubclass(schema, BaseModel)
        json_parser = PydanticOutputParser(pydantic_object=schema) if is_pydantic else None
        if json_parser is not None:
            format_instructions = json_parser.get_format_instructions()
            json_chain = self.bind(response_format={"type": "json_object"}) | json_parser

        def _append_directive(messages):
            directive = SystemMessage(
                content=(
                    "刚才你没有调用工具函数。请改用以下方式作答："
                    "【只输出一个 JSON 对象】，不要输出任何解释文字、不要用 markdown 代码块，"
                    "必须严格遵守下面的 JSON 结构（字段名一字不差）：\n" + format_instructions
                )
            )
            if isinstance(messages, str):
                return [HumanMessage(content=messages), directive]
            if isinstance(messages, list):
                return [*messages, directive]
            return messages

        def _invoke(messages):
            # 通道一：模型调用了工具 → 直接返回解析结果
            result = base_fc.invoke(messages)
            if result is not None:
                return result
            # 通道一失败（模型自然语言回答）→ 通道二：服务端强制 JSON
            if json_parser is not None:
                return json_chain.invoke(_append_directive(messages))
            # 非 pydantic schema：无法注入格式指令，直接报错（本项目 schema 全是 pydantic）
            raise RuntimeError("GLM 结构化输出失败：模型未调用工具，且 schema 不是 Pydantic 模型，无法回退")

        return RunnableLambda(_invoke)


def get_llm():
    """返回统一的 LLM 实例（GLM-4-Flash，智谱 OpenAI 兼容接口）。

    返回:
        ZhipuCompatChatOpenAI 实例（未绑定结构化输出，由各节点自己 .with_structured_output）
    """
    # Key 优先级：环境变量 ZHIPUAI_API_KEY > config.py 里的 ZHIPUAI_API_KEY > 占位值
    api_key = os.environ.get("ZHIPUAI_API_KEY") or (getattr(config, "ZHIPUAI_API_KEY", "") or "").strip()
    if not api_key:
        api_key = _PLACEHOLDER_KEY

    return ZhipuCompatChatOpenAI(
        model=getattr(config, "ZHIPU_MODEL", "glm-4-flash"),
        temperature=0,  # 与三个 Agent 既有约定一致：低温度、稳定可复现
        timeout=60,  # 请求级超时（秒），超时走各节点确定性兜底
        api_key=api_key,  # 显式传 Key：不再读 OPENAI_API_KEY，与 OpenAI 官方端点彻底解耦
        base_url=getattr(config, "ZHIPU_BASE_URL", "https://open.bigmodel.cn/api/paas/v4/"),
    )
