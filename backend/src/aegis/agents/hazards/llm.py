"""
项目统一 LLM 工厂（收口点）

【本文件角色】
  全项目所有 Agent（Data / Risk / Simulation）的 LLM 唯一来源。
  当前指向智谱开放平台的 GLM-4-Flash（OpenAI 兼容接口，
  仍用 langchain_openai.ChatOpenAI 直连，无需换 SDK / 无需新依赖）。

【API Key 配置】
  Key 本身不在本文件：走环境变量 AEGIS_EXT_LLM_API_KEY
  （旧名 ZHIPUAI_API_KEY 仍兼容，但文档与配置面只登记平台口径那一个）。
  【并入主线时的改动】原实现让运维"把 Key 粘进 config.py"——那是把凭据写回源码，
  主线口径是**只从环境变量读**（见 agents_ext/config.py）。
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

【★ 性能与韧性：客户端复用 + 熔断（2026-11-10 实测驱动）★】
  剖析 ext 研判链路量到：单次研判中位 **2952 ms**，其中 **26.3 s / 32.5 s（81%）**
  是 openai SDK 的指数退避 `time.sleep`（`openai/_base_client.py::_sleep_for_retry`），
  其余 6.2 s 是 SSL 读；纯 CPU 计算不到 0.5%。根因：
    SDK 默认 `max_retries=2`，而上游持续返回 500 —— **盲重试把一次逻辑调用变成
    三次 HTTP 往返**，每次都白等 0.5 s / 1.0 s 的退避。
  对策：
    - **熔断**（`agents_ext/llm_breaker.py`）：连续失败达阈值 → 冷却窗口内直接跳过 LLM，
      节点既有 try/except 立刻走确定性兜底；窗口到期放行一次试探，成功即恢复。
      实测：上游 5xx 时研判中位 **2952 ms → 4.14 ms**。
    - **客户端进程级复用**（按模型/端点/凭据指纹/重试次数缓存）：每次新建 client 要
      走一遍 pydantic 校验 + `openai.OpenAI` 包装层构造，实测 **0.26 ms/次**；
      复用后同一份配置共用一个实例。
      ★ 更重要的理由不是这 0.26 ms：**熔断器挂在 client 实例上**，
      若每次新建 client，失败计数就永远归零，熔断形同虚设。
      （注：httpx 连接池本来就由 langchain 按 (base_url, timeout) 缓存共享，
      所以这里省的不是 TLS 握手——那是误判，实测后已改正。）
  熔断的检查点选在 `_generate`（本类的薄适配层）——**不是** `get_llm()`：
  `risk_assess_node` / `risk_validate_node` 里 `get_risk_llm()` 在 try 块**之外**调用，
  若在工厂里抛错会冲掉节点既有的"模板兜底"契约；放在 `_generate` 则异常落在
  节点的 try 内，兜底行为逐字不变。

【对外契约】
  get_llm() -> ChatOpenAI 实例（未绑定结构化输出，由各节点自己 .with_structured_output）
  提供给：data_agent/nodes/data_plan_node.py、risk_agent/llm_setup.py、
          simulation_agent/llm_setup.py
"""

import hashlib
import os
import threading
import time

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.output_parsers import PydanticOutputParser
from langchain_core.runnables import RunnableLambda
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, PrivateAttr

from aegis.agents.core.breaker import LlmBreaker, LlmUnavailableError, is_transport_failure
from aegis.agents.core.ledger import LlmCallRecord, record_call
from aegis.agents.core.meta import CallMeta, current_call_meta
from aegis.agents.hazards import config
from aegis.domain.messages import now_iso

# Key 未配置时的占位值：调用 API 时必然 401。
# ★ 必须是纯 ASCII（HTTP 头无法编码中文），但内容仍是"去哪里填 Key"的指引
# 【并入主线时的改动】原提示串让运维"把 Key 粘进 config.py"——主线口径是**只从环境变量读**
# （源码里不该有凭据），照着旧提示做等于把凭据重新写回源码。
_PLACEHOLDER_KEY = (
    "ZHIPU_KEY_NOT_SET: set the AEGIS_EXT_LLM_API_KEY environment variable "
    "(or ZHIPUAI_API_KEY for compatibility); keys must not be written into source files"
)


class ZhipuCompatChatOpenAI(ChatOpenAI):
    """智谱 GLM 的结构化输出兼容层（双通道：function_calling → 强制 JSON）。

    详见文件头【智谱 GLM 的结构化输出兼容层】小节的实测结论。
    节点调用方式不变：llm.with_structured_output(Pydantic模型).invoke(messages)。
    """

    #: 本实例专属的熔断器（进程级共享的那一个由 `get_llm()` 注入）。
    #: 放在模型实例上而不是全局单例：不同端点/凭据各有一条熔断账，互不污染。
    _breaker: LlmBreaker = PrivateAttr(default_factory=LlmBreaker)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        """在真正发请求之前过熔断；请求结果回来后记账。

        为什么检查点在这里（而不是 `get_llm()` 工厂里）：
        `risk_assess_node` / `risk_validate_node` 里 `get_risk_llm()` 是在 try 块
        **之外**调用的——若在工厂里抛"LLM 不可用"，异常会冲掉节点既有的
        "模板兜底"契约（节点会整个失败，而不是降级出模板说明）。
        在 `_generate` 里抛，异常落在节点的 try 内，
        **兜底行为逐字不变**：LLM 不可用 → 模板说明 / 规则兜底。

        记账只认"传输层失败"（连接错误 / 超时 / 5xx / 429）：
        语义错误（模型返回了非法 JSON）是模型质量问题，不是"服务不可用"，
        计入熔断会把一个可修的提示词问题变成一段冷却期内的服务不可用。
        """
        breaker = self._breaker
        if not breaker.allow():
            raise LlmUnavailableError("LLM 熔断中（连续失败达阈值，冷却窗口内不再发起调用）；本次直接走确定性兜底。")
        started = time.perf_counter()
        meta = current_call_meta()
        try:
            result = super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
        except Exception as exc:
            if is_transport_failure(exc):
                breaker.note_failure()
            self._record_call(
                meta,
                latency_ms=(time.perf_counter() - started) * 1000,
                validation="transport_error",
                error=f"{type(exc).__name__}: {str(exc)[:180]}",
            )
            raise
        breaker.note_success()
        self._record_call(meta, latency_ms=(time.perf_counter() - started) * 1000, validation="ok")
        return result

    def _record_call(
        self,
        meta: CallMeta,
        *,
        latency_ms: float,
        validation: str,
        error: str | None = None,
    ) -> None:
        """落一条调用台账（与平台网关同一份形状）。

        灾种侧此前**零记账、零埋点**：用量、首过率、时延按 agent×model 归因这些口径
        全都统计不到灾种的调用。现在两边的台账行是同一种形状，
        `scripts/quality_gate.py` 的首过率口径对两侧一致。
        """
        record = LlmCallRecord(
            task=meta.task or "hazard",
            model=self.model_name,
            prompt_version=meta.prompt_version,
            temperature=float(self.temperature or 0.0),
            max_tokens=int(self.max_tokens or 0),
            latency_ms=latency_ms,
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=0,
            validation=validation,
            trace_id=meta.trace_id,
            agent_id=meta.agent_id,
            error=error,
            at=now_iso(),
            structured=meta.structured,
            attempt=0,
        )
        record_call(record)

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


# ---------- 客户端进程级复用 ----------
#
# 为什么需要（两条，第二条是主因）：
#   ① 每次新建 client 要走一遍 pydantic 校验 + `openai.OpenAI` 包装层构造，
#      实测 0.26 ms/次——小，但白花；
#   ② **熔断器挂在 client 实例上**：若每次新建 client，连续失败计数就永远归零，
#      熔断永远打不开，熔断本身就成了摆设。
#      （连接池不是问题：httpx 客户端由 langchain 按 (base_url, timeout) 缓存共享，
#      新建 client 并不会导致重新 TLS 握手——这一点是实测后纠正的，别再写错。）
#
# 缓存键为什么含 Key 的**指纹**而不是 Key 本身：
#   - 指纹够用（Key 换了 → 指纹变 → 建新 client），且不会把凭据留在缓存键里被日志/异常带出去；
#   - 指纹是单向摘要，读不出原 Key。
_CLIENT_CACHE: dict[tuple[str, str, str, int], ZhipuCompatChatOpenAI] = {}
_CLIENT_CACHE_LOCK = threading.Lock()


def _client_cache_key(model: str, base_url: str, api_key: str, max_retries: int) -> tuple[str, str, str, int]:
    fingerprint = hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16]
    return (model, base_url, fingerprint, max_retries)


def get_llm():
    """返回统一的 LLM 实例（GLM-4-Flash，智谱 OpenAI 兼容接口）。

    同一份配置下返回**同一个实例**（进程级复用：省掉每次的构造开销，
    更要紧的是让熔断账能跨调用累积——熔断器挂在 client 上）。
    配置变了（换 Key / 换模型 / 换端点）则返回新实例，旧实例仍在缓存里
    （凭据指纹入键，不会串号）。

    返回:
        ZhipuCompatChatOpenAI 实例（未绑定结构化输出，由各节点自己 .with_structured_output）
    """
    # Key 优先级：平台口径 AEGIS_EXT_LLM_API_KEY > 旧名 ZHIPUAI_API_KEY（兼容她原来的用法）> 占位值。
    # 平台口径的名字写在配置面（.env.example / config.py）上，旧名只作兼容，不再是文档里的主口径。
    api_key = (
        os.environ.get("AEGIS_EXT_LLM_API_KEY")
        or os.environ.get("ZHIPUAI_API_KEY")
        or (getattr(config, "ZHIPUAI_API_KEY", "") or "").strip()
    )
    if not api_key:
        api_key = _PLACEHOLDER_KEY

    model = getattr(config, "ZHIPU_MODEL", "glm-4-flash")
    base_url = getattr(config, "ZHIPU_BASE_URL", "https://open.bigmodel.cn/api/paas/v4/")
    # SDK 盲重试次数：0 = 不重试。
    # 为什么默认 0：SDK 默认 2 次重试带指数退避（0.5s/1.0s），上游持续 5xx 时
    # 一次逻辑调用变成三次 HTTP 往返（实测占研判时延的 81%）。
    # 上游已经不可用时，重试的是"同样的请求"，只会把降级推迟 1.5 s。
    # 韧性改由本模块的熔断承担：它记得"上游坏了"，坏了就直接跳过并走确定性兜底。
    # 偶发抖动仍会被下面的节点兜底接住（模板/规则），不会丢结论。
    max_retries = int(getattr(config, "ZHIPU_MAX_RETRIES", 0))

    cache_key = _client_cache_key(model, base_url, api_key, max_retries)
    with _CLIENT_CACHE_LOCK:
        client = _CLIENT_CACHE.get(cache_key)
        if client is None:
            client = ZhipuCompatChatOpenAI(
                model=model,
                temperature=0,  # 与三个 Agent 既有约定一致：低温度、稳定可复现
                timeout=60,  # 请求级超时（秒），超时走各节点确定性兜底
                api_key=api_key,  # 显式传 Key：不再读 OPENAI_API_KEY，与 OpenAI 官方端点彻底解耦
                base_url=base_url,
                max_retries=max_retries,
            )
            client._breaker = LlmBreaker(
                threshold=int(getattr(config, "LLM_BREAKER_THRESHOLD", 3)),
                cooldown_seconds=float(getattr(config, "LLM_BREAKER_COOLDOWN_SECONDS", 60)),
            )
            _CLIENT_CACHE[cache_key] = client
    return client


def current_hazard_model_name() -> str:
    """当前配置的模型名；**没配 Key 就返回空串**（不臆造模型名）。

    D10 凭证里"模型名"这一格的含义是"这条结论由哪个模型产出"。没配凭据时灾种这一腿
    走的是模板兜底，没有模型参与——那时写一个模型名就是伪造审计证据。
    """
    api_key = (
        os.environ.get("AEGIS_EXT_LLM_API_KEY")
        or os.environ.get("ZHIPUAI_API_KEY")
        or (getattr(config, "ZHIPUAI_API_KEY", "") or "").strip()
    )
    if not api_key:
        return ""
    return str(getattr(config, "ZHIPU_MODEL", "glm-4-flash"))


def reset_llm_clients() -> None:
    """清空客户端缓存与熔断状态（测试用；运维改配置后也可调用）。"""
    with _CLIENT_CACHE_LOCK:
        _CLIENT_CACHE.clear()
