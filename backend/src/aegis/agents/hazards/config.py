"""外部智能体模块（`agents_ext`）的运行配置面。

【本文件在并入时改了什么——只改配置来源，其余逻辑逐字保留】
原仓库的同名文件把**明文 API Key 写死在源码里**并提交进了公开仓库。
并入主线时必须改掉，否则主仓会带着一个已泄露的凭据发布出去。三条改动：

1. **Key 只从环境变量读**（`AEGIS_EXT_LLM_API_KEY`），不再有"把 Key 粘进源码"这条路；
2. **原 Key 必须轮换**：它随原仓库公开过，已经算泄露（见 `archive/external_agents_source_20261109/README.md`）；
3. **运行时文件路径配置化**（checkpoint 库、反馈记忆 JSON）：原实现把路径写死成相对 CWD 的
   `data/...` 与包内固定位置，从不同目录启动会写到不同地方、还会写进代码仓。

【为什么保留这个文件而不是并进 `aegis.config.Settings`】
本模块是**外部交付的可选实现**：它带着自己的依赖（langgraph/langchain）与自己的运行参数，
与平台主配置面不是一回事。平台侧只通过 `aegis.config.Settings` 的
`agent_impl` 等键把"要不要装、装不装得起来"交过来；模型凭据与模型名这类
"外部实现自己的事"留在本文件里，但**只允许从环境变量取**。

【诚实声明】
没配 Key 时本模块不报错：LLM 工厂返回一个占位 Key 的实例，真正调用时 401 失败 →
各节点既有 try/except 打印原因并走确定性兜底，流程不崩。
"""

from __future__ import annotations

import os
from pathlib import Path

# ============================================================
# 智谱开放平台（GLM）配置
# ============================================================

#: API Key **只从环境变量读**。没有"粘进源码"这个选项——原仓库那样做过，且因此泄露。
#: 未配置时留空，LLM 工厂会用占位 Key（调用必 401，节点兜底降级，流程不崩）。
ZHIPUAI_API_KEY = os.environ.get("AEGIS_EXT_LLM_API_KEY", "").strip()

#: 模型名与接口地址：可用环境变量覆盖（不同部署可能用别的 OpenAI 兼容端点）。
ZHIPU_MODEL = os.environ.get("AEGIS_EXT_LLM_MODEL", "glm-4-flash").strip()

#: 智谱 OpenAI 兼容接口地址（ChatOpenAI 用 base_url 直连，无需换 SDK）
ZHIPU_BASE_URL = os.environ.get("AEGIS_EXT_LLM_BASE_URL", "https://open.bigmodel.cn/api/paas/v4/").strip()

#: SDK 盲重试次数（openai SDK 默认 2）。
#: 这里默认 **0**：SDK 的重试带指数退避（0.5s/1.0s），上游持续 5xx 时
#: 一次逻辑调用变成三次 HTTP 往返——2026-11-10 实测它占 ext 研判时延的 81%（26.3s / 32.5s）。
#: 上游已坏时重试同样的请求只会推迟降级；韧性由 LLM 熔断承担（见 `llm_breaker.py`）。
ZHIPU_MAX_RETRIES = int(os.environ.get("AEGIS_EXT_LLM_MAX_RETRIES", "0"))

#: LLM 熔断阈值：连续多少次"服务不可用"后打开熔断。
#: 缺省 3 与平台网关同口径（`aegis.config.Settings.llm_breaker_fail_threshold`）。
LLM_BREAKER_THRESHOLD = int(os.environ.get("AEGIS_EXT_LLM_BREAKER_THRESHOLD", "3"))

#: LLM 熔断冷却窗口（秒）：窗口内直接跳过 LLM 调用，让节点走确定性兜底。
#: 平台网关的熔断**没有**恢复窗口（靠 fallback 模型接力）；外部实现只有一个模型，
#: 必须自带冷却，否则上游恢复了也永远用不上。
LLM_BREAKER_COOLDOWN_SECONDS = float(os.environ.get("AEGIS_EXT_LLM_BREAKER_COOLDOWN_SECONDS", "60"))

# ============================================================
# Manager Agent 配置（全局调度中心）
# ============================================================

# 各 Agent 的 Manager 外层重试上限（"重新完整调用一次该 Agent"计为一次重试，
# 与各 Agent 内部自带的小重试相互独立）
MANAGER_RETRY_CONFIG = {
    "data_agent": 2,
    "risk_agent": 1,
    "simulation_agent": 1,
    "warning_agent": 1,
    "feedback_agent": 1,
}

# Feedback 返工闭环最大轮数（防 Manager ⇄ Feedback 无限反复）
MANAGER_MAX_FEEDBACK_LOOPS = 2

# 模拟是否可选：True 时 Simulation 失败并达重试上限 → 跳过并继续 Warning，
# 任务标记 partially_completed；用户显式 require_simulation=True 时本项失效。
MANAGER_SIMULATION_OPTIONAL = True

# 非 Mock 数据的时效阈值（小时），与 Feedback Agent 检查口径一致
MANAGER_DATA_MAX_AGE_HOURS = 72.0


def _runtime_path(env_name: str, default_name: str) -> Path:
    """运行时文件路径：环境变量优先，否则落在**进程工作目录**下的 `data/`。

    原实现写死相对路径 `data/manager_checkpoints.sqlite`：从不同目录启动会写到不同地方，
    容器里还会写到镜像层（重启即失）。这里改成绝对路径，并允许显式指定。
    """
    raw = os.environ.get(env_name, "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return (Path.cwd() / "data" / default_name).resolve()


#: Checkpoint（工作流执行状态持久化，与 Feedback Memory 是两套机制）。
#: 显式配置了 AEGIS_EXT_CHECKPOINT_DB 才启用断点恢复——默认关闭，不为没要求的能力
#: 引入每请求一次的 SQLite 开销。
MANAGER_CHECKPOINT_DB = str(_runtime_path("AEGIS_EXT_CHECKPOINT_DB", "manager_checkpoints.sqlite"))
MANAGER_CHECKPOINT_ENABLED = bool(os.environ.get("AEGIS_EXT_CHECKPOINT_DB", "").strip())

#: Feedback 记忆的 JSON 存储：原实现固定写在包内（会进代码仓、且被测试写）。
#: 这里默认落到工作目录的 `data/`，可用环境变量改。
FEEDBACK_MEMORY_FILE = str(_runtime_path("AEGIS_EXT_FEEDBACK_MEMORY", "feedback_memory.json"))
