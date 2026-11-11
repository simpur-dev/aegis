"""链内一致性审计本体的契约入口（`chain_audit`）。

【与触达归因是两件事】
平台反馈段的 `feedback.attribute` 问的是"预警发出去为什么没效果"（触达归因）；
本本体问的是"**这条链自己有没有自相矛盾**"——灾种、等级、状态在各交付物之间对不对得上。

两件事的输入完全不同：触达归因要 `reach_stats`（通道回执统计），链内审计要的是
**各腿的交付物**。所以它们是两项能力、两个本体，不该挤成一条腿。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from aegis.agents.agentkit import AgentKit, Reply
from aegis.agents.core.meta import trace_id_var
from aegis.agents.hazards.contracts import with_native
from aegis.agents.hazards.errors import HazardRejected
from aegis.agents.hazards.pool import run_blocking
from aegis.agents.hazards.provenance import hazard_model_info
from aegis.domain.enums import Action
from aegis.domain.messages import AgentMessage
from aegis.errors import ErrorCode

log = logging.getLogger("aegis.agents.hazards.feedback_agent.agent")


@dataclass
class ChainAuditAgent(AgentKit):
    """`chain_audit` 的契约入口：各腿交付物 → 链内一致性审计结论。"""

    def _provenance(self):
        """D10 凭证：这条结论由哪条提示词、哪个模型产出。

        提示词版本来自唯一注册表；模型名取本体的 provider（未配置时留空，
        **不臆造模型名**）。因子引擎的版本不在这里重复——它在 payload 的
        `assessment.engine_version` 与业务 `model_info` dict 里。
        """
        from aegis.agents.hazards.llm import current_hazard_model_name

        return hazard_model_info(self.agent_id, model=current_hazard_model_name())

    async def handle(self, request: AgentMessage) -> Reply:
        """总线入口：挂 trace_id 后交给具体实现。

        节点在图里跑、拿不到总线信封，trace_id 靠 ContextVar 带进去——
        `pool.run_blocking` 的 `copy_context()` 保证切线程后仍然可读。
        """
        token = trace_id_var.set(request.trace_id)
        try:
            return await self._handle(request)
        finally:
            trace_id_var.reset(token)

    async def _handle(self, request: AgentMessage) -> Reply:
        if request.action != Action.FEEDBACK_CHAIN_AUDIT.value:
            raise HazardRejected(
                ErrorCode.SCHEMA_INVALID,
                f"本本体只处理 {Action.FEEDBACK_CHAIN_AUDIT.value}，收到 {request.action}",
                detail={"action": request.action},
            )
        payload = request.payload or {}
        hazard_type = str(payload.get("hazard_type") or "")
        if not hazard_type:
            raise HazardRejected(ErrorCode.SCHEMA_INVALID, "链内审计必须给 hazard_type")

        from aegis.agents.hazards.feedback_agent.main import run_feedback_agent

        task = str(payload.get("task") or "复核本次分析链路")
        try:
            result = await run_blocking(
                run_feedback_agent,
                task,
                hazard_type,
                payload.get("data_result") or {},
                payload.get("risk_result") or {},
                payload.get("simulation_result") or None,
                payload.get("warning_result") or None,
                event_id=str(payload.get("event_id") or ""),
            )
        except Exception as exc:
            log.warning("链内审计异常", extra={"err": f"{type(exc).__name__}: {exc}"[:200]})
            raise HazardRejected(
                ErrorCode.INTERNAL,
                f"链内审计异常：{type(exc).__name__}: {exc}"[:300],
                retryable=True,
            ) from exc

        return Reply(
            payload=with_native(self._to_contract(result, hazard_type=hazard_type), result),
            model_info=self._provenance(),
        )

    @staticmethod
    def _to_contract(result: dict[str, Any], *, hazard_type: str) -> dict[str, Any]:
        """审计结论 → 契约应答。平台读 `summary` 与 `improvement_suggestions`；闭环读全量。"""
        summary = dict(result.get("summary") or {})
        return {
            "hazard_type": hazard_type,
            "status": str(result.get("status") or ""),
            "feedback_id": str(result.get("feedback_id") or ""),
            "event_id": str(result.get("event_id") or ""),
            "summary": summary,
            "issue_count": int(summary.get("issue_count") or 0),
            "actionable_issue_count": int(summary.get("actionable_issue_count") or 0),
            "needs_human_review_count": int(summary.get("needs_human_review_count") or 0),
            "feedback_items": list(result.get("feedback_items") or [])[:64],
            "improvement_suggestions": list(result.get("improvement_suggestions") or [])[:32],
            "manager_actions": list(result.get("manager_actions") or [])[:16],
            "reflection_result": result.get("reflection_result") or {},
            "safety_declarations": result.get("safety_declarations") or {},
        }


__all__ = ["ChainAuditAgent"]
