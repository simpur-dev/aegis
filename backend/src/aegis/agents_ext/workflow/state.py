"""
全局工作流 State —— 薄再导出

【说明】全局调度 State 的唯一实现位于 agents/manager_agent/state.py
（Manager 即全局 workflow 的唯一控制中心）。本文件只做再导出，
不再实现第二套调度逻辑，避免双全局状态漂移。
"""

from aegis.agents_ext.manager_agent.state import (  # noqa: F401
    AGENT_CN_NAME,
    AGENT_ORDER,
    AGENT_RESULT_KEY,
    TASK_TYPES,
    ManagerState,
)
