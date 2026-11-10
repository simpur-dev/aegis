"""灾种闭环本体的契约入口（`hazard_workflow`）。

一次完整的 取数 → 研判 → 仿真 → 预警 → 审计 编排，作为总线上的**一个本体**存在：
平台派一条 `plan.hazard_workflow` 请求过来，本体跑完整条闭环并交回报告。

【它和平台链路的关系】
平台链路（`pipeline/stages.py`）是**逐事件流式**的：感知 → 研判 → 决策 → 执行 → 反馈，
每段各派一单。闭环是**一次一事件**的纵深模式：同一个事件把五段跑完，还带返工环。
两条模式共用同一条总线、同一批本体、同一套契约——区别只是编排者是谁。

【闭环内部也走总线】
本体向五个兄弟派单时，走的是**总线**（`manager_agent/dispatcher.py` 的总线接法），
不是进程内函数直调：直调会绕过消息信封、deadline、幂等与错误分类，等于在自己包里
私开一套接线——那样"接了总线"就只是一句口号。

【它不做什么】
- 不发布预警：闭环产出的预警是**草稿**，`alert_status` 恒为 draft、通道全未实现；
- 不持有触达证据：触达证据只认平台交付腿的真回执；
- 不进生产热路径：闭环是演练/研判请求形态，仿真腿全程 mock 标记。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from aegis.agents.agentkit import AgentKit, Reply
from aegis.agents.hazards.bus_context import BusContext
from aegis.agents.hazards.contracts import hazard_workflow_request
from aegis.agents.hazards.errors import HazardRejected
from aegis.agents.hazards.pool import run_blocking
from aegis.domain.enums import Action
from aegis.domain.messages import AgentMessage
from aegis.errors import ErrorCode

log = logging.getLogger("aegis.agents.hazards.manager_agent.agent")


@dataclass
class HazardWorkflowAgent(AgentKit):
    """`hazard_workflow` 的契约入口：闭环请求 → 闭环报告。"""

    #: 兄弟本体地址（capability → 寻址信息）。装配层注入——"我该找谁"是装配事实。
    siblings: dict[str, Any] = field(default_factory=dict)
    #: 闭环内部派单的事务预算（毫秒）。
    inner_deadline_ms: int = 60_000

    async def handle(self, request: AgentMessage) -> Reply:
        if request.action != Action.PLAN_HAZARD_WORKFLOW.value:
            raise HazardRejected(
                ErrorCode.SCHEMA_INVALID,
                f"本本体只处理 {Action.PLAN_HAZARD_WORKFLOW.value}，收到 {request.action}",
                detail={"action": request.action},
            )
        payload = request.payload or {}
        hazard_type = str(payload.get("hazard_type") or "")
        if not hazard_type:
            raise HazardRejected(ErrorCode.SCHEMA_INVALID, "闭环请求必须给 hazard_type")

        from aegis.agents.hazards.manager_agent.main import run_manager_agent

        contract = hazard_workflow_request(
            hazard_type=hazard_type,
            task_type=str(payload.get("task_type") or "full_analysis"),
            region=str(payload.get("region") or ""),
            user_request=str(payload.get("user_request") or ""),
            event_id=str(payload.get("event_id") or ""),
            analysis_time=str(payload.get("analysis_time") or ""),
            require_simulation=bool(payload.get("require_simulation")),
            preset_scenario_configs=list(payload.get("preset_scenario_configs") or []),
        )

        bus = BusContext(
            transport=self.transport,
            caller_id=self.agent_id,
            siblings=self.siblings,
            deadline_ms=self.inner_deadline_ms,
            # 闭环图是同步的（LangGraph 节点在 executor 线程里跑），总线请求必须回到
            # 平台自己的事件循环上发——总线与循环是绑定的。
            loop=asyncio.get_running_loop(),
        )
        try:
            report = await run_blocking(
                run_manager_agent,
                hazard_type=contract["hazard_type"],
                task_type=contract["task_type"],
                user_request=contract["user_request"],
                region=contract["region"],
                analysis_time=contract["analysis_time"],
                event_id=contract["event_id"],
                require_simulation=contract["require_simulation"],
                preset_scenario_configs=contract["preset_scenario_configs"] or None,
                bus=bus,
                trace_id=request.trace_id,
            )
        except Exception as exc:
            log.warning("灾种闭环异常", extra={"err": f"{type(exc).__name__}: {exc}"[:200]})
            raise HazardRejected(
                ErrorCode.INTERNAL,
                f"灾种闭环异常：{type(exc).__name__}: {exc}"[:300],
                retryable=True,
            ) from exc

        return Reply(payload=self._to_contract(report))

    @staticmethod
    def _to_contract(report: dict[str, Any]) -> dict[str, Any]:
        """闭环报告 → 契约应答。

        交付物（data_result / risk_result / …）原样带回：闭环的价值就在这五份东西，
        收窄成"一段总结"等于把产物扔了。
        """
        return {
            "event_id": str(report.get("event_id") or ""),
            "hazard_type": str(report.get("hazard_type") or ""),
            "region": str(report.get("region") or ""),
            "task_status": str(report.get("task_status") or ""),
            "agent_status": report.get("agent_status") or {},
            "human_review_required": bool(report.get("human_review_required")),
            "final_report": report.get("final_report") or {},
            "error_message": str(report.get("error_message") or ""),
            "data_result": report.get("data_result") or {},
            "risk_result": report.get("risk_result") or {},
            "simulation_result": report.get("simulation_result") or {},
            "warning_result": report.get("warning_result") or {},
            "feedback_result": report.get("feedback_result") or {},
        }


__all__ = ["HazardWorkflowAgent"]
