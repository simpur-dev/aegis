"""读动作：查询、解释、预案问答。

读动作的共同点：不落 `control_actions`、不写审计、不需要确认。它们的价值在于"把平台已有的
事实取出来并说清楚"，所以每个动作的 `execute` 只做一次读取 + 一次确定性叙述，不夹带判断。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from aegis.domain.enums import HazardType
from aegis.domain.messages import StandardizedTaskUnit, WarningRecord
from aegis.domain.operator import OperatorRole
from aegis.services.assistant.base import (
    Action,
    ActionContext,
    ActionDomain,
    ActionResult,
    ActionSpec,
    ResolvedTargets,
    TargetRef,
)
from aegis.services.assistant.narrate import narrate_chain, narrate_plan, narrate_warning, trim_metrics
from aegis.services.response.task_parser import PLAYBOOKS

_WARNING_ID = r"^wrn_[0-9a-f]{6,24}$"
_TRACE_ID = r"^trc_[0-9a-f]{16}$"
_EVENT_ID = r"^evt_[0-9a-f]{12}$"
_TASK_ID = r"^stu_[0-9a-f]{16}$"
_REGION = r"^[0-9A-Z]{6,24}$"


class AgentDirectory(Protocol):
    """`AgentRegistry` 的只读面：动作只需要"在线几个、都有谁"。"""

    @property
    def online_count(self) -> int: ...

    def snapshot(self) -> list[dict[str, Any]]: ...


class GatewayCounters(Protocol):
    @property
    def counters(self) -> Mapping[str, int]: ...


class _Limit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    limit: int = Field(default=10, ge=1, le=50)


class EmptyParams(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WarningListParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    limit: int = Field(default=10, ge=1, le=50)
    region_code: str | None = Field(default=None, pattern=_REGION)
    released: bool | None = Field(default=None, description="true=只看已发布，false=只看未发布，null=全部")
    status: Literal["draft", "released", "delivered", "partial", "failed", "revoked"] | None = Field(
        default=None, description="按派生状态过滤；revoked=已人工撤销"
    )


class WarningExplainParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    warning_id: str = Field(pattern=_WARNING_ID)


class ChainListParams(_Limit):
    pass


class ChainExplainParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trace_id: str | None = Field(default=None, pattern=_TRACE_ID)
    event_id: str | None = Field(default=None, pattern=_EVENT_ID)

    @model_validator(mode="after")
    def _need_one(self) -> ChainExplainParams:
        if not self.trace_id and not self.event_id:
            raise ValueError("需要 trace_id 或 event_id 之一")
        return self


class TaskListParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    limit: int = Field(default=20, ge=1, le=100)
    region_code: str | None = Field(default=None, pattern=_REGION)


class TaskGetParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_unit_id: str = Field(pattern=_TASK_ID)


class StationListParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    limit: int = Field(default=20, ge=1, le=200)
    region_code: str | None = Field(default=None, pattern=_REGION)


class PlanAskParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hazard_type: str | None = Field(default=None, max_length=32)
    query: str = Field(default="", max_length=500)
    region_code: str | None = Field(default=None, pattern=_REGION)


class WarningList(Action):
    spec = ActionSpec(
        name="warning.list",
        title="查询预警",
        domain=ActionDomain.WARNING,
        required_role=OperatorRole.DUTY,
        params=WarningListParams,
        example="最近发布了哪些预警",
    )

    def __init__(self, warnings: Any) -> None:
        self._warnings = warnings

    async def execute(self, targets: ResolvedTargets, params: BaseModel, ctx: ActionContext) -> ActionResult:
        assert isinstance(params, WarningListParams)
        rows: list[WarningRecord] = self._warnings.list(
            limit=params.limit, region_code=params.region_code, released=params.released, status=params.status
        )
        items = [row.model_dump(mode="json") for row in rows]
        return ActionResult(data={"items": items, "count": len(items)}, summary=f"共 {len(items)} 条预警")


class WarningExplain(Action):
    spec = ActionSpec(
        name="warning.explain",
        title="解释一条预警（认知镜像）",
        domain=ActionDomain.WARNING,
        required_role=OperatorRole.DUTY,
        params=WarningExplainParams,
        example="解释 wrn_0123456789abcdef0123 为什么定这个等级",
    )

    def __init__(self, warnings: Any, chains: Any) -> None:
        self._warnings = warnings
        self._chains = chains

    async def resolve(self, params: BaseModel, ctx: ActionContext) -> ResolvedTargets:
        assert isinstance(params, WarningExplainParams)
        record = self._warnings.get(params.warning_id)
        label = params.warning_id if record is None else str(record.model_dump(mode="json").get("title_zh") or params.warning_id)
        return ResolvedTargets(items=(TargetRef("warning", params.warning_id, label),))

    async def execute(self, targets: ResolvedTargets, params: BaseModel, ctx: ActionContext) -> ActionResult:
        assert isinstance(params, WarningExplainParams)
        record = self._warnings.get(params.warning_id)
        if record is None:
            return ActionResult(data={"warning_id": params.warning_id, "found": False})
        dumped = record.model_dump(mode="json")
        chain = None
        trace_id = str(dumped.get("trace_id") or "")
        if trace_id:
            chain = _find_chain(self._chains, trace_id)
        facts = narrate_warning(dumped, chain)
        return ActionResult(
            data={"warning_id": params.warning_id, "found": True, "facts": facts, "text": facts},
            summary=facts,
        )


class ChainList(Action):
    spec = ActionSpec(
        name="chain.list",
        title="查询最近链路",
        domain=ActionDomain.QUERY,
        required_role=OperatorRole.DUTY,
        params=ChainListParams,
        example="最近的事件列表",
    )

    def __init__(self, chains: Any) -> None:
        self._chains = chains

    async def execute(self, targets: ResolvedTargets, params: BaseModel, ctx: ActionContext) -> ActionResult:
        assert isinstance(params, ChainListParams)
        rows = self._chains.latest(params.limit)
        items = [row.as_dict() for row in rows]
        return ActionResult(data={"items": items, "count": len(items)}, summary=f"共 {len(items)} 条链路")


class ChainExplain(Action):
    spec = ActionSpec(
        name="chain.explain",
        title="解释一条链路（认知镜像）",
        domain=ActionDomain.QUERY,
        required_role=OperatorRole.DUTY,
        params=ChainExplainParams,
        example="链路追踪 trc_0123456789abcdef 每段耗时",
    )

    def __init__(self, chains: Any) -> None:
        self._chains = chains

    async def resolve(self, params: BaseModel, ctx: ActionContext) -> ResolvedTargets:
        assert isinstance(params, ChainExplainParams)
        wanted = str(params.trace_id or params.event_id)
        return ResolvedTargets(items=(TargetRef("chain", wanted),))

    async def execute(self, targets: ResolvedTargets, params: BaseModel, ctx: ActionContext) -> ActionResult:
        assert isinstance(params, ChainExplainParams)
        wanted = str(params.trace_id or params.event_id)
        chain = _find_chain(self._chains, wanted)
        if chain is None:
            return ActionResult(data={"trace_id": wanted, "found": False, "note": "链路读视图只保留进程内最近事件"})
        facts = narrate_chain(chain)
        return ActionResult(data={"trace_id": wanted, "found": True, "facts": facts, "text": facts}, summary=facts)


class TaskList(Action):
    spec = ActionSpec(
        name="task.list",
        title="查询任务单元",
        domain=ActionDomain.TASK,
        required_role=OperatorRole.DUTY,
        params=TaskListParams,
        example="任务单元清单",
    )

    def __init__(self, tasks: Any) -> None:
        self._tasks = tasks

    async def execute(self, targets: ResolvedTargets, params: BaseModel, ctx: ActionContext) -> ActionResult:
        assert isinstance(params, TaskListParams)
        rows: list[StandardizedTaskUnit] = self._tasks.list(limit=params.limit, region_code=params.region_code)
        items = [row.model_dump(mode="json") for row in rows]
        return ActionResult(data={"items": items, "count": len(items)}, summary=f"共 {len(items)} 个任务单元")


class TaskGet(Action):
    spec = ActionSpec(
        name="task.get",
        title="查看任务单元详情",
        domain=ActionDomain.TASK,
        required_role=OperatorRole.DUTY,
        params=TaskGetParams,
        example="查看任务单元 stu_0123456789abcdef 的详情",
    )

    def __init__(self, tasks: Any) -> None:
        self._tasks = tasks

    async def resolve(self, params: BaseModel, ctx: ActionContext) -> ResolvedTargets:
        assert isinstance(params, TaskGetParams)
        return ResolvedTargets(items=(TargetRef("task", params.task_unit_id),))

    async def execute(self, targets: ResolvedTargets, params: BaseModel, ctx: ActionContext) -> ActionResult:
        assert isinstance(params, TaskGetParams)
        unit = self._tasks.get(params.task_unit_id)
        dumped = None if unit is None else unit.model_dump(mode="json")
        return ActionResult(
            data={"task": dumped},
            summary="未找到该任务单元" if dumped is None else str(dumped.get("objective", "")),
        )


class StationList(Action):
    spec = ActionSpec(
        name="station.list",
        title="查询站点台账",
        domain=ActionDomain.QUERY,
        required_role=OperatorRole.DUTY,
        params=StationListParams,
        example="站点台账里有哪些站",
    )

    def __init__(self, stations: Any) -> None:
        self._stations = stations

    async def execute(self, targets: ResolvedTargets, params: BaseModel, ctx: ActionContext) -> ActionResult:
        assert isinstance(params, StationListParams)
        rows = await self._stations.list_stations(region_code=params.region_code, limit=params.limit)
        items = [dict(row) for row in rows]
        return ActionResult(data={"items": items, "count": len(items)}, summary=f"共 {len(items)} 个站点")


class AgentList(Action):
    spec = ActionSpec(
        name="agent.list",
        title="查询在线智能体",
        domain=ActionDomain.QUERY,
        required_role=OperatorRole.DUTY,
        params=EmptyParams,
        example="现在有哪些在线智能体",
    )

    def __init__(self, registry: AgentDirectory, gateway: GatewayCounters) -> None:
        self._registry = registry
        self._gateway = gateway

    async def execute(self, targets: ResolvedTargets, params: BaseModel, ctx: ActionContext) -> ActionResult:
        items = self._registry.snapshot()
        agents = {"online": self._registry.online_count, "items": items, "gateway_counters": dict(self._gateway.counters)}
        return ActionResult(data={"agents": agents}, summary=f"在线智能体 {agents['online']} 个")


class MetricReport(Action):
    spec = ActionSpec(
        name="metric.report",
        title="查询指标量测",
        domain=ActionDomain.QUERY,
        required_role=OperatorRole.DUTY,
        params=EmptyParams,
        example="p95 时延达标吗",
    )

    def __init__(self, report: Callable[[], dict[str, Any]]) -> None:
        self._report = report

    async def execute(self, targets: ResolvedTargets, params: BaseModel, ctx: ActionContext) -> ActionResult:
        metrics = trim_metrics(self._report())
        return ActionResult(data={"metrics": metrics}, summary="指标量测")


class PlanAsk(Action):
    spec = ActionSpec(
        name="plan.ask",
        title="预案问答",
        domain=ActionDomain.QUERY,
        required_role=OperatorRole.DUTY,
        params=PlanAskParams,
        example="这个沟道泥位抬升要不要转移群众",
    )

    def __init__(self, recall: Callable[..., Awaitable[list[dict[str, Any]]]] | None) -> None:
        self._recall = recall

    async def execute(self, targets: ResolvedTargets, params: BaseModel, ctx: ActionContext) -> ActionResult:
        assert isinstance(params, PlanAskParams)
        hazard = HazardType(params.hazard_type) if params.hazard_type else HazardType.UNKNOWN
        steps = [
            {
                "task_type": step.task_type.value,
                "objective": step.objective,
                "owner_role": step.owner_role.value,
                "sla_seconds": step.sla_seconds,
                "capability": step.capability,
            }
            for step in PLAYBOOKS.get(hazard, PLAYBOOKS[HazardType.UNKNOWN])
        ]
        recalled: list[dict[str, Any]] = []
        degraded: str | None = None
        if self._recall is not None:
            try:
                recalled = list(await self._recall(query=params.query or hazard.cn, hazard_type=params.hazard_type, limit=3))
            except Exception as exc:  # 知识腿故障不阻断预案要点：如实标注降级，继续给主结论
                degraded = f"{type(exc).__name__}"
                recalled = [{"degraded": True, "reason": degraded}]
        facts = narrate_plan(hazard, steps, [case for case in recalled if not case.get("degraded")])
        return ActionResult(
            data={
                "hazard_type": hazard.value,
                "steps": steps,
                "cases": recalled,
                "degraded": degraded,
                "text": facts,
            },
            summary=facts,
        )


def _find_chain(chains: Any, wanted: str) -> dict[str, Any] | None:
    for row in chains.snapshot():
        data = row.as_dict()
        if wanted in (data.get("trace_id"), data.get("event_id")):
            return data
    return None
