"""
全局工作流路由 —— 薄再导出

确定性条件路由的唯一实现在 agents/manager_agent/router.py。
"""

from aegis.agents.hazards.manager_agent.router import (  # noqa: F401
    route_after_analyze,
    route_after_check,
    route_after_feedback_review,
    route_after_plan,
    route_after_retry,
)
