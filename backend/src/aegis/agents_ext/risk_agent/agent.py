"""外部风险研判服务的**契约入口**：直接实现 `assess.v1`，不经任何适配层。

本类只做四件事：动作校验、请求解析、证据获取（推送 → 拉取）、把图运行结果
转成 `AssessResponse` 载荷。语义翻译不存在了——请求与响应都是平台 `aegis.domain`
里的同一套 DTO，外部实现与 builtin/mock 平级。

错误一律抛 `AssessRejected`：`AgentKit._on_request` 按 `error_code` / `retryable` /
`error_detail` 把它转成 `kind=error` 回执，网关据此记 `outcome=error`、平台走降级轨。
"""

from __future__ import annotations

import asyncio
import logging

from pydantic import ValidationError

from aegis.agents.agentkit import AgentKit, Reply
from aegis.agents_ext.risk_agent.errors import AssessRejected
from aegis.agents_ext.risk_agent.evidence_pull import pull_evidence
from aegis.agents_ext.risk_agent.main import run_risk_agent
from aegis.domain.assess import CONTRACT_VERSION, AssessErrorCode, AssessRequest
from aegis.domain.enums import Action
from aegis.domain.messages import AgentMessage

log = logging.getLogger("aegis.agents_ext.risk_agent.agent")


class RiskAssessAgent(AgentKit):
    """`assess.hazard` 的外部实现：统一契约进，统一契约出。"""

    async def handle(self, request: AgentMessage) -> Reply:
        if request.action != Action.ASSESS_HAZARD.value:
            raise AssessRejected(
                AssessErrorCode.BAD_INPUT,
                f"本本体只处理 {Action.ASSESS_HAZARD.value}，收到 {request.action}",
                detail={"action": request.action},
            )
        try:
            req = AssessRequest.model_validate(request.payload or {})
        except ValidationError as exc:
            raise AssessRejected(
                AssessErrorCode.BAD_INPUT,
                f"研判请求不合 {CONTRACT_VERSION}：{len(exc.errors())} 处字段错误",
                detail={"errors": exc.errors(include_url=False)[:8]},
            ) from exc

        evidence = req.evidence
        if evidence is None:
            evidence = await asyncio.to_thread(pull_evidence, req.region_code, req.window)
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
            result = await asyncio.to_thread(run_risk_agent, req, evidence, agent_id=self.agent_id)
        except AssessRejected:
            raise
        except Exception as exc:  # 图内部异常：如实报错误码，重试可能恢复
            log.warning("外部研判图异常", extra={"err": f"{type(exc).__name__}: {exc}"[:200]})
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
        return Reply(payload=result.model_dump(mode="json"))


__all__ = ["RiskAssessAgent"]
