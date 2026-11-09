"""
项目统一配置文件

【本文件角色】
  存放大模型 API Key 等项目级配置。目前使用智谱开放平台（bigmodel.cn）的
  GLM-4-Flash 模型，通过其 OpenAI 兼容接口调用。

【★ API Key 在这里填 ★】
  1. 到 https://open.bigmodel.cn 注册并创建 API Key（glm-4-flash 免费额度即可用）
  2. 把 Key 粘贴到下面 ZHIPUAI_API_KEY = ""  # 原文件此处是明文密钥，已抹除（密钥须轮换） 的引号之间，保存即可，其他都不用改
  3. 也可以不改文件，改用环境变量：setx ZHIPUAI_API_KEY "你的Key"
     （环境变量优先于本文件）

【诚实声明】
  密钥属于敏感信息：不要把填好 Key 的 config.py 提交到公开仓库或发给别人。
"""

# ============================================================
# 智谱开放平台（GLM）配置
# ============================================================

ZHIPUAI_API_KEY = ""  # 原文件此处是明文密钥，已抹除（密钥须轮换）   # ←★ 在等号引号之间粘贴你的智谱 APIKey，例如 "xxxxxxxx.xxxxxxxx"

ZHIPU_MODEL = "glm-4-flash"   # 模型名（glm-4-flash 免费；可换 glm-4-plus 等）

# 智谱 OpenAI 兼容接口地址（ChatOpenAI 用 base_url 直连，无需换 SDK）
ZHIPU_BASE_URL = "https://open.bigmodel.cn/api/paas/v4/"

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

# Checkpoint（工作流执行状态持久化，与 Feedback Memory 是两套机制）
MANAGER_CHECKPOINT_DB = "data/manager_checkpoints.sqlite"
