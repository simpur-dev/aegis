"""
全局工作流图 —— 薄再导出

唯一实现在 agents/manager_agent/graph.py；本层导出编译入口
create_manager_graph（含 Checkpointer 注入参数）与五 Agent 适配注册表，
供接口层（根 main.py / 未来 API）统一引用。
"""

from aegis.agents_ext.manager_agent import dispatcher  # noqa: F401
from aegis.agents_ext.manager_agent.graph import (  # noqa: F401
    build_manager_graph,
    create_manager_graph,
)
from aegis.agents_ext.manager_agent.main import run_manager_agent  # noqa: F401
