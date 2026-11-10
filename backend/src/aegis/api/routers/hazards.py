"""灾种闭环入口：一次完整的 取数 → 研判 → 仿真 → 预警 → 审计 编排（演练/研判请求形态）。

闭环在总线上的身份是 `plan.hazard_workflow` 这一项能力（本体 `plan.loop01`）。平台收到
请求后经网关派一条标准请求，等闭环交回报告——闭环内部对五个兄弟本体的派单**同样走总线**，
这条端点因此也是"全部智能体都接总线"的一块拼图：没有它，闭环编排就没有平台侧的请求入口。

它是**演练/研判请求形态**，不是生产预警热路径：闭环产出的预警是草稿（`alert_status=draft`），
仿真腿全程 mock 标记；生产链路仍走 `pipeline/stages.py` 的逐段派发。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from aegis.api.deps import get_container
from aegis.container import PlatformContainer
from aegis.domain.enums import Action, AgentType
from aegis.domain.messages import new_trace_id
from aegis.errors import AegisError

#: 闭环五条腿串行跑，LLM 档一次 11–20s，全程可能到分钟级。AgentMessage 的
#: `deadline_ms` 上限是 600_000，这里给请求方留旋钮但封在契约上限内。
_MAX_DEADLINE_MS = 600_000
_DEFAULT_DEADLINE_MS = 300_000


class HazardWorkflowRequest(BaseModel):
    """灾种闭环入参。`hazard_type` 显式必填——闭环不猜灾种。"""

    hazard_type: str = Field(min_length=2, max_length=32)
    task_type: str = Field(
        default="full_analysis",
        pattern="^(full_analysis|data_only|risk_only|simulation_only|warning_only|feedback_only)$",
    )
    region: str = Field(default="", max_length=64)
    user_request: str = Field(default="", max_length=2000)
    event_id: str = Field(default="", pattern=r"^(evt_[0-9a-f]{12})?$")
    analysis_time: str = Field(default="", max_length=40)
    require_simulation: bool = False
    preset_scenario_configs: list[dict[str, Any]] = Field(default_factory=list, max_length=16)
    timeout_ms: int = Field(default=_DEFAULT_DEADLINE_MS, ge=5_000, le=_MAX_DEADLINE_MS)


def build_router() -> APIRouter:
    router = APIRouter()

    @router.post("/api/v1/hazards/workflow", tags=["hazards"])
    async def hazards_workflow(payload: HazardWorkflowRequest, ctn: PlatformContainer = Depends(get_container)) -> dict[str, Any]:
        """运行一次灾种闭环，返回闭环报告（五腿交付物 + 任务状态 + 错误历史）。

        闭环找不到承接本体（灾种模块没装配）或某条腿超期/报错时，闭环报告里会如实
        记录（`task_status` / `error_history`）——只有"闭环本体根本不在册"才走 503。
        """
        try:
            reply = await ctn.gateway.dispatch(
                AgentType.PLAN,
                Action.PLAN_HAZARD_WORKFLOW,
                payload.model_dump(exclude={"timeout_ms"}),
                trace_id=new_trace_id(),
                capability="hazard_workflow",
                deadline_ms=payload.timeout_ms,
                ttl_ms=payload.timeout_ms + 5_000,
                priority=2,
            )
        except AegisError as exc:
            raise HTTPException(status_code=503, detail={"code": exc.code.value, "message": exc.message}) from exc
        return dict(reply.payload)

    return router
