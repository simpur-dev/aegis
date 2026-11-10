"""写动作：演练发起与人工上报。

这两个动作是"执行类必须人工确认"的最小闭环：`preview` 说清会影响什么，`execute` 复用
平台既有服务（`IngestService` / `ReportIntake`），助手不复制任何业务逻辑。

确认与落库由执行器经 `control_actions` 统一承担，这里只保证"动作实现只有一份"——对话路径与
确认路径调的是同一个 `execute`，助手不复制任何业务逻辑。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from aegis.domain.operator import OperatorRole
from aegis.services.assistant.base import (
    Action,
    ActionContext,
    ActionDomain,
    ActionError,
    ActionResult,
    ActionSpec,
    ImpactPreview,
    ResolvedTargets,
)

_REGION = r"^[0-9A-Z]{6,24}$"


class DrillStartParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenario: Literal["surge", "normal"] = "surge"
    ticks: int = Field(default=2, ge=1, le=5)
    region_code: str | None = Field(default=None, pattern=_REGION)


class ReportSubmitParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: str = Field(min_length=4, max_length=2_000)
    region_code: str = Field(pattern=_REGION)
    reporter: str = Field(default="web-assistant", min_length=2, max_length=64)
    hazard_type: str | None = Field(default=None, max_length=32)


class ReportIntakePort(Protocol):
    async def submit(self, *, note: str, region_code: str, reporter: str = ..., hazard_hint: str | None = ...) -> dict[str, Any]: ...


class DrillStart(Action):
    spec = ActionSpec(
        name="drill.start",
        title="发起一次演练",
        domain=ActionDomain.DRILL,
        required_role=OperatorRole.DUTY,
        params=DrillStartParams,
        needs_confirm=True,
        writes=True,
        previewable=True,
        example="发起一次演练",
    )

    def __init__(self, run: Callable[..., Awaitable[dict[str, Any]]], *, available: bool) -> None:
        self._run = run
        self._available = available

    async def preview(self, targets: ResolvedTargets, params: BaseModel, ctx: ActionContext) -> ImpactPreview:
        assert isinstance(params, DrillStartParams)
        return ImpactPreview(
            audience_count=0,
            channels=("站内",),
            irreversible=("会在真实总线上产生新的链路与任务单元",),
            warnings=(f"演练模式 {params.scenario}，{params.ticks} 轮；产物标记为演练，不对外发布预警",),
        )

    async def execute(self, targets: ResolvedTargets, params: BaseModel, ctx: ActionContext) -> ActionResult:
        assert isinstance(params, DrillStartParams)
        if not self._available:
            raise ActionError("E_DRILL_UNAVAILABLE", "模拟器未启用（AEGIS_SIMULATOR_ENABLED=false），演练腿不可用")
        drill = await self._run(scenario=params.scenario, ticks=params.ticks, region_code=params.region_code)
        return ActionResult(data={"drill": drill}, summary=f"演练完成，{drill.get('regions', 0)} 个区域")


class ReportSubmit(Action):
    spec = ActionSpec(
        name="report.submit",
        title="提交人工上报",
        domain=ActionDomain.WARNING,
        required_role=OperatorRole.DUTY,
        params=ReportSubmitParams,
        needs_confirm=True,
        writes=True,
        previewable=True,
        example="我要上报：沟道泥位抬升，下游有村庄",
    )

    def __init__(self, intake: ReportIntakePort) -> None:
        self._intake = intake

    async def preview(self, targets: ResolvedTargets, params: BaseModel, ctx: ActionContext) -> ImpactPreview:
        assert isinstance(params, ReportSubmitParams)
        return ImpactPreview(
            audience_count=0,
            channels=("三路解析链路",),
            irreversible=("上报一旦受理会生成预警与任务单元，撤回要走正式流程",),
            warnings=(f"上报区域 {params.region_code}；低置信度会自动开人工核签工单",),
        )

    async def execute(self, targets: ResolvedTargets, params: BaseModel, ctx: ActionContext) -> ActionResult:
        assert isinstance(params, ReportSubmitParams)
        report = await self._intake.submit(
            note=params.note,
            region_code=params.region_code,
            reporter=params.reporter,
            hazard_hint=params.hazard_type,
        )
        return ActionResult(data={"report": report}, summary="上报已受理")
