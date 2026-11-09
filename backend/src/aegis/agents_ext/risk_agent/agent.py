"""外部风险研判服务的**契约入口**：直接实现 `assess.v1`，不经任何适配层。

本类只做四件事：动作校验、请求解析、证据获取（推送 → 拉取）、把图运行结果
转成 `AssessResponse` 载荷。语义翻译不存在了——请求与响应都是平台 `aegis.domain`
里的同一套 DTO，外部实现与 builtin/mock 平级。

错误一律抛 `AssessRejected`：`AgentKit._on_request` 按 `error_code` / `retryable` /
`error_detail` 把它转成 `kind=error` 回执，网关据此记 `outcome=error`、平台走降级轨。
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, TypeVar

from pydantic import ValidationError

from aegis.agents.agentkit import AgentKit, Reply
from aegis.agents_ext.risk_agent.errors import AssessRejected
from aegis.agents_ext.risk_agent.evidence_pull import pull_evidence
from aegis.agents_ext.risk_agent.main import run_risk_agent
from aegis.domain.assess import CONTRACT_VERSION, AssessErrorCode, AssessRequest
from aegis.domain.enums import Action
from aegis.domain.messages import AgentMessage

log = logging.getLogger("aegis.agents_ext.risk_agent.agent")

_R = TypeVar("_R")


@dataclass
class RiskAssessAgent(AgentKit):
    """`assess.hazard` 的外部实现：统一契约进，统一契约出。

    研判图是同步的，整段放**专用线程池**执行：默认的 `asyncio.to_thread` 用的是全进程
    共享执行器，真模型一次研判占一个槽 11–20s，会和检索/分析旁路的 to_thread 抢线程；
    专用池把这条腿的占用隔离出来，容量由 `pool_workers`（部署键
    `AEGIS_EXT_ASSESS_POOL_WORKERS`）决定。
    """

    pool_workers: int = 8
    _pool: ThreadPoolExecutor = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.pool_workers < 1:
            raise ValueError(f"研判线程池大小必须 ≥1，收到 {self.pool_workers}")
        self._pool = ThreadPoolExecutor(max_workers=self.pool_workers, thread_name_prefix="aegis-ext-assess")

    async def _run_in_pool(self, func: Callable[..., _R], /, *args: Any, **kwargs: Any) -> _R:
        """在专用池里跑阻塞腿，并像 `asyncio.to_thread` 一样把请求上下文带进线程。"""
        loop = asyncio.get_running_loop()
        ctx = contextvars.copy_context()
        return await loop.run_in_executor(self._pool, functools.partial(ctx.run, func, *args, **kwargs))

    async def stop(self) -> None:
        await super().stop()
        # 先退订再关池：在跑的研判线程取消不掉（ADR-0012），只取消还没开始的排队任务。
        self._pool.shutdown(wait=False, cancel_futures=True)

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
            evidence = await self._run_in_pool(pull_evidence, req.region_code, req.window)
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
            result = await self._run_in_pool(run_risk_agent, req, evidence, agent_id=self.agent_id)
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
