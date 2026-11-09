"""
Manager 工作流 Checkpoint 持久化（SQLite / SqliteSaver —— 第十一节）

【定位与边界】
  - 本模块只负责【工作流执行状态】的持久化与中断恢复（LangGraph Checkpoint）；
  - 与 Feedback Memory（agents/feedback_agent/tools/feedback_memory_tool.py，
    JSON 文件 agents/feedback_agent/memory/）是两套完全不同的机制：
    前者存"任务跑到哪一步/中间状态"，后者存"跨任务的经验反馈记录"。
    两者物理分离（不同目录、不同格式、不同读写代码），互不读写对方数据。

【恢复语义（防重复执行）】
  - thread_id = event_id：一个任务一条线程，不同任务天然隔离；
  - 中断恢复 = graph.invoke(None, {"configurable": {"thread_id": ...}})，
    LangGraph 从最后一个成功 superstep 的检查点继续：已完成且结果仍有效的
    Agent 节点【不会被重新调用】，仅从中断处继续（见 resume_manager_task）；
  - 已跑到 END 的线程再次恢复：直接返回终态，零 Agent 调用（幂等）。
"""

import sqlite3
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver


def get_checkpointer(db_path: str | None = None) -> SqliteSaver:
    """打开（或创建）Manager 专用 SQLite 检查点库。

    db_path 缺省取 config.MANAGER_CHECKPOINT_DB（data/manager_checkpoints.sqlite）。
    返回的 SqliteSaver 与连接同生命周期；进程内复用即可，无需显式关闭。
    """
    from aegis.agents_ext.config import MANAGER_CHECKPOINT_DB

    path = Path(db_path or MANAGER_CHECKPOINT_DB)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    saver = SqliteSaver(conn)
    saver.setup()
    return saver
