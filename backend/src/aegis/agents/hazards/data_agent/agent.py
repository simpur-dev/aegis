"""数据质量门本体（`data_quality`）：判"这份数据够不够研判"。

研判段在派单之前先问一句：够才值得花一次 LLM 调用，不够就把理由留在台账上、让降级
路径接手——而不是照研判一遍再发现数据根本不够。

两条输入路径，同一个动作：

- **生产路径**：平台推送了证据包（请求里的 `evidence`），本校验平台推来的**真实**证据；
- **闭环/演练路径**：请求里 `fetch=true`，本模块走自带的取数-清洗-校验链（数据源是
  Mock 源，产出全程带 mock 标记）。

两条路径的应答字段名**完全一致**（`is_valid` / `completeness_ratio` /
`validation_reason` / `sufficient` / `reason` / `missing_data`），调用方不必分叉。
取数路径额外带回 `processed_data`——那是下游因子引擎要的输入，不是给平台看的。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from aegis.agents.agentkit import AgentKit, Reply
from aegis.agents.hazards.contracts import data_quality_response
from aegis.agents.hazards.data_agent.tools.evidence_quality_tool import evidence_quality_tool
from aegis.agents.hazards.errors import HazardRejected
from aegis.agents.hazards.pool import run_blocking
from aegis.domain.enums import Action
from aegis.domain.messages import AgentMessage
from aegis.errors import ErrorCode


@dataclass
class DataQualityAgent(AgentKit):
    """`data_quality` 的契约入口：证据包 → 研判准入判据。"""

    async def handle(self, request: AgentMessage) -> Reply:
        if request.action != Action.ASSESS_DATA_QUALITY.value:
            raise HazardRejected(
                ErrorCode.SCHEMA_INVALID,
                f"本本体只处理 {Action.ASSESS_DATA_QUALITY.value}，收到 {request.action}",
                detail={"action": request.action},
            )
        payload = request.payload or {}
        evidence = payload.get("evidence") or {}
        if isinstance(evidence, dict) and evidence.get("observations") is not None:
            verdict = evidence_quality_tool(evidence, region_code=str(payload.get("region_code") or ""))
            verdict["is_mock"] = bool((evidence.get("provenance") or {}).get("is_mock"))
            return Reply(payload=data_quality_response(verdict))
        if payload.get("fetch"):
            return Reply(payload=await self._fetch_and_validate(payload))
        raise HazardRejected(
            ErrorCode.SCHEMA_INVALID,
            "数据质量门请求既没有证据包也没有 fetch=true：没有数据可判",
            detail={"action": request.action},
        )

    async def _fetch_and_validate(self, payload: dict[str, Any]) -> dict[str, Any]:
        """闭环/演练路径：取数 → 清洗 → 校验，并把取数产物带回给下游。"""
        from aegis.agents.hazards.data_agent.main import run_data_agent

        hazard_type = str(payload.get("hazard_type") or "")
        if not hazard_type:
            raise HazardRejected(ErrorCode.SCHEMA_INVALID, "取数路径必须给 hazard_type")
        task = str(payload.get("task") or "获取并处理观测数据")
        state = await run_blocking(run_data_agent, task, hazard_type=hazard_type)

        quality = state.get("data_quality") or {}
        sufficiency = state.get("data_sufficiency") or {}
        processed = state.get("processed_data") or {}
        verdict = data_quality_response(
            {
                "is_valid": quality.get("is_valid"),
                "completeness_ratio": quality.get("completeness_ratio"),
                "validation_reason": quality.get("validation_reason"),
                "sufficient": sufficiency.get("sufficient"),
                "reason": sufficiency.get("reason"),
                "missing_data": state.get("missing_data") or [],
            }
        )
        verdict["is_mock"] = bool(processed.get("is_mock", True))
        verdict["data_source"] = str(processed.get("data_source") or "mock")
        verdict["processed_data"] = processed
        verdict["status"] = str(state.get("status") or "")
        # 原生交付物：闭环的下一条腿（因子引擎）按原形状吃它
        verdict["native"] = {
            "processed_data": processed,
            "data_quality": quality,
            "missing_data": list(state.get("missing_data") or []),
            "data_sufficiency": sufficiency,
            "status": str(state.get("status") or ""),
            "hazard_type": str(state.get("hazard_type") or ""),
        }
        return verdict


__all__ = ["DataQualityAgent"]
