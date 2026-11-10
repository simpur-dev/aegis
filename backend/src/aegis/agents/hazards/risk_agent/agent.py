"""因子引擎的**契约入口**：平台 `assess.v1` 的研判请求 → 定级结论。

本类只做四件事：动作校验、请求解析、证据获取（推送 → 拉取）、把图运行结果
转成契约应答。语义翻译不存在了——请求与响应都是平台 `aegis.domain` 里的同一套 DTO。

【它在链路里的位置：异构第二意见】
平台自建的研判内环（`aegis.agents.assess`）走 LLM 主结论 + 规则轨 + 双盲仲裁；本本体
走的是**确定性因子引擎**（`FACTOR-ENGINE-v0.1`，P/T/R + rule_hits），LLM 只用来组织
措辞。两腿引擎不同源，因此结论不一致时值得留痕——但**仲裁权仍在平台主研判手里**，
本腿不改主结论。

【两条输入路径】
- **平台路径**：请求带 `evidence`（平台推送的证据包），走 `assess.v1` 的 `AssessRequest`；
- **闭环路径**：请求带 `data_result`（闭环内部取数产物），把扁平事件聚合成因子观测。
  这一条**不会伪造平台 DTO**：演练路径的区域可能是中文地名，它就不是平台区域码，
  也不该被硬拗成。

错误一律抛 `AssessRejected`：`AgentKit._on_request` 按 `error_code` / `retryable` /
`error_detail` 把它转成 `kind=error` 回执，网关据此记 `outcome=error`、平台走降级轨。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from aegis.agents.agentkit import AgentKit, Reply
from aegis.agents.hazards.contracts import factor_risk_response, with_native
from aegis.agents.hazards.errors import HazardRejected
from aegis.agents.hazards.pool import run_blocking
from aegis.agents.hazards.risk_agent.errors import AssessRejected
from aegis.agents.hazards.risk_agent.evidence_pull import pull_evidence
from aegis.agents.hazards.risk_agent.main import LEVEL_FROM_PROVISIONAL, SOURCE_MARK, run_risk_agent
from aegis.domain.assess import CONTRACT_VERSION, AssessErrorCode, AssessRequest
from aegis.domain.enums import Action
from aegis.domain.messages import AgentMessage
from aegis.errors import ErrorCode

log = logging.getLogger("aegis.agents.hazards.risk_agent.agent")


@dataclass
class FactorAssessAgent(AgentKit):
    """`factor_risk_assess` 的契约入口：统一契约进，统一契约出。"""

    async def handle(self, request: AgentMessage) -> Reply:
        if request.action != Action.ASSESS_FACTOR_RISK.value:
            raise AssessRejected(
                AssessErrorCode.BAD_INPUT,
                f"本本体只处理 {Action.ASSESS_FACTOR_RISK.value}，收到 {request.action}",
                detail={"action": request.action},
            )
        payload = request.payload or {}
        if payload.get("data_result"):
            return Reply(payload=await self._assess_from_data_result(payload))
        return Reply(payload=await self._assess_from_request(payload))

    async def _assess_from_request(self, payload: dict[str, Any]) -> dict[str, Any]:
        """平台路径：assess.v1 请求 + 证据包 → 定级结论。"""
        try:
            req = AssessRequest.model_validate(payload)
        except ValidationError as exc:
            raise AssessRejected(
                AssessErrorCode.BAD_INPUT,
                f"研判请求不合 {CONTRACT_VERSION}：{len(exc.errors())} 处字段错误",
                detail={"errors": exc.errors(include_url=False)[:8]},
            ) from exc

        evidence = req.evidence
        if evidence is None:
            evidence = await run_blocking(pull_evidence, req.region_code, req.window)
        if evidence is None or not evidence.observations:
            raise AssessRejected(
                AssessErrorCode.INSUFFICIENT,
                f"没有可用的研判证据（region={req.region_code}, window={req.window}）：请求未推送证据，拉取腿也未配置或拉取失败",
                retryable=False,
                detail={"region_code": req.region_code, "window": req.window},
            )
        if evidence.region_code != req.region_code:
            raise AssessRejected(
                AssessErrorCode.BAD_INPUT,
                f"证据区域与请求不一致：证据 {evidence.region_code}，请求 {req.region_code}",
                detail={"evidence_region": evidence.region_code, "request_region": req.region_code},
            )

        try:
            result = await run_blocking(run_risk_agent, req, evidence, agent_id=self.agent_id)
        except AssessRejected:
            raise
        except Exception as exc:  # 图内部异常：如实报错误码，重试可能恢复
            log.warning("因子研判图异常", extra={"err": f"{type(exc).__name__}: {exc}"[:200]})
            raise AssessRejected(
                AssessErrorCode.FAILED,
                f"研判执行异常：{type(exc).__name__}: {exc}"[:300],
                retryable=True,
            ) from exc

        if result.region_code != req.region_code:
            raise AssessRejected(
                AssessErrorCode.BAD_INPUT,
                f"研判结果区域与请求不一致：{result.region_code} != {req.region_code}",
                detail={"result_region": result.region_code, "request_region": req.region_code},
            )
        return factor_risk_response(
            {
                "hazard_type": result.hazard_type.value,
                "region_code": result.region_code,
                "risk_level": int(result.risk_level),
                "confidence": result.confidence,
                "confidence_basis": result.confidence_basis,
                "rationale": result.rationale,
                "evidence_refs": result.evidence_refs,
                "source": result.source,
                "assessed_by": result.assessed_by,
                "human_review_suggested": result.human_review_suggested,
                "assessment": result.assessment,
                "data_provenance": result.data_provenance.model_dump(mode="json"),
                "status": "success",
            }
        )

    async def _assess_from_data_result(self, payload: dict[str, Any]) -> dict[str, Any]:
        """闭环路径：闭环内部取数产物 → 定级结论（走旧的 events 口径）。"""
        from aegis.agents.hazards.risk_agent.legacy import run_risk_agent_legacy

        hazard_type = str(payload.get("hazard_type") or "")
        if not hazard_type:
            raise HazardRejected(ErrorCode.SCHEMA_INVALID, "闭环路径必须给 hazard_type")
        data_result = payload.get("data_result") or {}
        task = str(payload.get("task") or "评估灾害风险")
        try:
            result = await run_blocking(run_risk_agent_legacy, task, data_result, hazard_type)
        except Exception as exc:
            log.warning("因子研判（闭环路径）异常", extra={"err": f"{type(exc).__name__}: {exc}"[:200]})
            raise HazardRejected(
                ErrorCode.INTERNAL,
                f"研判执行异常：{type(exc).__name__}: {exc}"[:300],
                retryable=True,
            ) from exc

        status = str(result.get("status") or "")
        if status not in ("success", ""):
            raise HazardRejected(
                ErrorCode.INTERNAL,
                f"因子研判未形成结论（status={status}）：{str(result.get('reasoning') or '')[:200]}",
                retryable=status == "failed",
            )
        provisional = str(result.get("risk_level") or "")
        level = LEVEL_FROM_PROVISIONAL.get(provisional)
        if level is None:
            # "算不出来"不能落成平台 5（无风险）：宁可拒绝，也不做假定级。
            raise HazardRejected(
                ErrorCode.INTERNAL,
                f"因子研判档位无法映射到平台等级：{provisional!r}",
                detail={"hazard_type": hazard_type},
            )
        events = (data_result.get("processed_data") or {}).get("events") or []
        region = str(events[0].get("region", "unknown")) if events and isinstance(events[0], dict) else "unknown"
        return with_native(
            factor_risk_response(
                {
                    "hazard_type": hazard_type,
                    "region_code": region,
                    "risk_level": int(level),
                    "confidence": float(result.get("confidence") or 0.0),
                    "confidence_basis": str(result.get("confidence_basis") or ""),
                    "rationale": str(result.get("reasoning") or ""),
                    "evidence_refs": [],
                    "source": SOURCE_MARK,
                    "assessed_by": self.agent_id,
                    "human_review_suggested": True,
                    "assessment": result.get("assessment") or {},
                    "data_provenance": result.get("data_provenance") or {},
                    "status": "success",
                    "risk_factors": result.get("risk_factors") or [],
                }
            ),
            result,
        )


__all__ = ["FactorAssessAgent"]
