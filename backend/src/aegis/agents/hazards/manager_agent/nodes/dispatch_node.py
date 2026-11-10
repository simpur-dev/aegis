"""
dispatch_node：调度枢纽（hub）—— 弹出调度队列队首，执行对应专业 Agent

【职责】
  - 只按 pending_agents 顺序调用 dispatcher 注册表中的适配函数（子图接口），
    五个专业 Agent 之间不直接互调，一切跨 Agent 数据流经 ManagerState；
  - 每次调用有结构化记录（agent_call_log），异常转为错误记录而非静默吞掉；
  - 成功：交付物写入对应 result 键、队首出队、登记 completed；
    失败：Agent 留在队首（供 retry_decision 决定重试/跳过/停止），交付物不落地。

【走哪条接法】
  有总线上下文（`current_bus_context()`）时走**总线**：向承接这一步的兄弟本体发标准
  请求、等标准回执；没有总线上下文（离线自测 / 单机演示）时走本地直调。两条接法
  产出同形状的交付物，闭环其余部分不感知差异。

【红线】对交付物只存不改 —— 风险分数、预警等级、Mock 标识原样透传。
"""

import logging
import time

from aegis.agents.hazards.bus_context import current_bus_context
from aegis.agents.hazards.manager_agent import dispatcher
from aegis.agents.hazards.manager_agent.state import (
    AGENT_RESULT_KEY,
    ERROR_AGENT_EXECUTION,
    ERROR_AGENT_TIMEOUT,
)
from aegis.errors import AegisError, ErrorCode

log = logging.getLogger("aegis.agents.hazards.manager_agent.nodes.dispatch_node")


def _attempt_no(state: dict, agent: str) -> int:
    """该 Agent 迄今的调用次数（首跑=1，之后每次重试+1）。"""
    return sum(1 for c in (state.get("agent_call_log") or []) if c.get("agent") == agent) + 1


def dispatch_node(state: dict) -> dict:
    pending = list(state.get("pending_agents") or [])
    if not pending:
        # 队列空仍被调度到（防御式）：直接交给路由收尾
        return {"current_agent": "", "check_result": {"verdict": "ok", "agent": ""}}

    agent = pending[0]
    result_key = AGENT_RESULT_KEY[agent]
    attempt = _attempt_no(state, agent)
    t0 = time.monotonic()
    updates: dict = {
        "current_agent": agent,
        "agent_call_log": list(state.get("agent_call_log") or []),
        "error_history": list(state.get("error_history") or []),
        "retry_counts": dict(state.get("retry_counts") or {}),
    }

    log.info(f"[Manager/Dispatch] >>> {agent}（第 {attempt} 次调用）")
    try:
        bus = current_bus_context()
        if bus is not None:
            output = dispatcher.dispatch_over_bus(
                agent,
                state,
                bus,
                trace_id=str(state.get("trace_id") or "") or "trc_0000000000000000",
            )
            # 闭环内部按**原形状**消费交付物（核验表与下一条腿都是按原形状写的）：
            # 契约应答里带着 native，就用它；没有就退回整份应答（本地接法的产物）。
            if isinstance(output, dict) and isinstance(output.get("native"), dict):
                output = output["native"]
        else:
            output = dispatcher.dispatch(agent, state)
    except Exception as e:
        elapsed = round(time.monotonic() - t0, 2)
        # 超时判据用平台错误分类（`LoopDispatchTimeout` 的 code=E_TIMEOUT），
        # 不猜消息文本——总线路径的超时消息是中文，按子串猜必然落成执行错误。
        timed_out = isinstance(e, AegisError) and e.code is ErrorCode.TIMEOUT
        err_type = ERROR_AGENT_TIMEOUT if timed_out or "timeout" in str(e).lower() else ERROR_AGENT_EXECUTION
        updates["agent_call_log"].append(
            {"agent": agent, "attempt": attempt, "status": "exception", "error": str(e)[:300], "elapsed_s": elapsed}
        )
        updates["error_history"].append({"agent": agent, "error_type": err_type, "message": str(e)[:300], "attempt": attempt})
        # Agent 留在队首，由 result_check → retry_decision 决定去留
        # from_dispatch_exception 标记：result_check 据此透传（区别于交付物核验）
        updates["check_result"] = {
            "verdict": "failed",
            "agent": agent,
            "error_type": err_type,
            "message": str(e)[:300],
            "from_dispatch_exception": True,
        }
        log.info(f"[Manager/Dispatch] <<< {agent} 调用异常: {err_type}: {str(e)[:120]}")
        return updates

    elapsed = round(time.monotonic() - t0, 2)
    status = str(output.get("status", ""))
    updates["agent_call_log"].append(
        {"agent": agent, "attempt": attempt, "status": status or "(无status键)", "error": "", "elapsed_s": elapsed}
    )
    updates[result_key] = output
    # 清空上一轮核验结论（重试成功后不得沿用旧 failed 判定）
    updates["check_result"] = {"verdict": "pending", "agent": agent}

    # 注意：是否"成功"留给 result_check 判定，这里只做结构落地
    log.info(f"[Manager/Dispatch] <<< {agent} 返回 status={status}（{elapsed}s）")
    return updates
