"""预警文案素材本体的契约入口（`warn_content`）。

【只产素材，不持发布权】
本模块原有的预警链路止步于"草稿"：`alert_status` 恒为 `draft`、官方等级恒为空、
发布/短信/邮件等通道全部 `not_implemented`。这不是没做完，是**刻意的边界**——
一次真实预警的发布权在平台执行内环（受众分群、防幻觉数字闸、藏文译文），
触达证据只认平台交付腿的真回执。

并入平台后这条边界照旧：本体只把 CAP 1.2 报文、建议动作与解释文本交给平台，
应答里**没有**"已发布"、没有通道回执、没有官方等级。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from aegis.agents.agentkit import AgentKit, Reply
from aegis.agents.core.meta import trace_id_var
from aegis.agents.hazards.contracts import warn_content_response, with_native
from aegis.agents.hazards.errors import HazardRejected
from aegis.agents.hazards.pool import run_blocking
from aegis.agents.hazards.provenance import hazard_model_info
from aegis.domain.enums import Action
from aegis.domain.messages import AgentMessage
from aegis.errors import ErrorCode

log = logging.getLogger("aegis.agents.hazards.warning_agent.agent")


@dataclass
class WarningContentAgent(AgentKit):
    """`warn_content` 的契约入口：定级结论 → 文案素材（不含发布动作）。"""

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
        if request.action != Action.EXECUTE_WARN_CONTENT.value:
            raise HazardRejected(
                ErrorCode.SCHEMA_INVALID,
                f"本本体只处理 {Action.EXECUTE_WARN_CONTENT.value}，收到 {request.action}",
                detail={"action": request.action},
            )
        payload = request.payload or {}
        hazard_type = str(payload.get("hazard_type") or "")
        if not hazard_type:
            raise HazardRejected(ErrorCode.SCHEMA_INVALID, "预警素材必须给 hazard_type")

        from aegis.agents.hazards.warning_agent.main import run_warning_agent

        task = str(payload.get("task") or "生成预警草稿")
        try:
            result = await run_blocking(
                run_warning_agent,
                task,
                hazard_type,
                self._risk_result(payload),
                payload.get("simulation_result") or None,
                payload.get("data_result") or None,
            )
        except Exception as exc:
            log.warning("预警素材生成异常", extra={"err": f"{type(exc).__name__}: {exc}"[:200]})
            raise HazardRejected(
                ErrorCode.INTERNAL,
                f"预警素材生成异常：{type(exc).__name__}: {exc}"[:300],
                retryable=True,
            ) from exc

        draft = result.get("warning_result") if isinstance(result.get("warning_result"), dict) else result
        return Reply(
            payload=with_native(warn_content_response(self._to_material(draft)), draft),
            model_info=self._provenance(),
        )

    @staticmethod
    def _risk_result(payload: dict[str, Any]) -> dict[str, Any]:
        """把调用方给的定级结论归一成预警腿看得懂的形状。

        平台路径给的是平台口径（`risk_level`/`rationale`/`assessment`），闭环路径给的是
        原生风险 dict（`assessment`/`risk_level`/`status`）。两边字段名本就大半重合，
        这里只补齐缺失的键——**不替调用方编等级**：给什么等级就用什么等级。
        """
        risk = payload.get("risk_result") or {}
        if not isinstance(risk, dict):
            return {}
        normalized = dict(risk)
        normalized.setdefault("status", "success")
        normalized.setdefault("assessment", {})
        normalized.setdefault("risk_level", payload.get("level") or 0)
        return normalized

    @staticmethod
    def _to_material(draft: dict[str, Any]) -> dict[str, Any]:
        """预警草稿 → 文案素材。原模块那些"草稿态"字段一律不外泄。"""
        alert = draft.get("alert") or {}
        explanation = draft.get("explanation") or {}
        cap = draft.get("cap") or {}
        return {
            "explanation_text": str(explanation.get("text") or ""),
            "suggested_actions": list(alert.get("suggested_actions") or []),
            "cap_xml": str(cap.get("xml") or ""),
            "cap_dict": cap.get("dict") if isinstance(cap.get("dict"), dict) else {},
            "based_on": list(alert.get("based_on") or []),
            "warning_level": str(alert.get("warning_level") or ""),
            "target_region": str(alert.get("target_region") or ""),
            "is_mock": bool(alert.get("mock_only", False)),
        }


__all__ = ["WarningContentAgent"]
