"""情景仿真本体的契约入口（`scenario_sim`）。

【它是什么，不是什么】
情景推演是灾种链的**演练能力**：模型层全是 `mock_` 前缀的合成指标，
`is_real_prediction` 恒为 False，产出不是现场预测。

所以本体的位置被刻意限定在**闭环/演练路径**：它不参与生产链路的研判与发布，
产出也不会进任何对外发布的预警文案。谁要是在生产链路里看到这条腿的产出，那一定是
接错了——应答里全程带着 `is_mock` 与 `model_info`，就是为了让这件事一眼可辨。
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

log = logging.getLogger("aegis.agents.hazards.simulation_agent.agent")


@dataclass
class ScenarioSimAgent(AgentKit):
    """`scenario_sim` 的契约入口：情景参数 + 上游交付物 → 推演结论。"""

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
        if request.action != Action.ASSESS_SCENARIO_SIM.value:
            raise HazardRejected(
                ErrorCode.SCHEMA_INVALID,
                f"本本体只处理 {Action.ASSESS_SCENARIO_SIM.value}，收到 {request.action}",
                detail={"action": request.action},
            )
        payload = request.payload or {}
        hazard_type = str(payload.get("hazard_type") or "")
        if not hazard_type:
            raise HazardRejected(ErrorCode.SCHEMA_INVALID, "情景仿真必须给 hazard_type")

        from aegis.agents.hazards.simulation_agent.main import run_simulation_agent

        task = str(payload.get("task") or "设计情景并模拟推演")
        try:
            result = await run_blocking(
                run_simulation_agent,
                task,
                payload.get("data_result") or {},
                payload.get("risk_result") or {},
                hazard_type,
                preset_scenario_configs=list(payload.get("preset_scenario_configs") or []) or None,
            )
        except Exception as exc:
            log.warning("情景仿真异常", extra={"err": f"{type(exc).__name__}: {exc}"[:200]})
            raise HazardRejected(
                ErrorCode.INTERNAL,
                f"情景仿真异常：{type(exc).__name__}: {exc}"[:300],
                retryable=True,
            ) from exc

        return Reply(
            payload=with_native(self._to_contract(result, hazard_type=hazard_type), result),
            model_info=self._provenance(),
        )

    @staticmethod
    def _to_contract(result: dict[str, Any], *, hazard_type: str) -> dict[str, Any]:
        """原生产出 → 契约应答。**mock 标记必须原样带出去**，不许在上层被洗掉。"""
        model_info = dict(result.get("model_info") or {})
        return {
            "hazard_type": hazard_type,
            "status": str(result.get("status") or ""),
            "simulation_results": result.get("simulation_results") or [],
            "comparison": result.get("comparison") or {},
            "missing_inputs": list(result.get("missing_inputs") or [])[:32],
            "unsupported_reason": str(result.get("unsupported_reason") or ""),
            "model_info": model_info,
            "is_mock": True,
            "is_real_prediction": bool(model_info.get("is_real_prediction", False)),
        }


__all__ = ["ScenarioSimAgent"]
