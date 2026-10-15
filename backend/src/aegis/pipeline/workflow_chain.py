"""用工作流引擎跑同一条五段链路（D7 编排归一 §4.6 的目标形态）。

与 `HazardResponseChain` 的区别只有一处：段的调用顺序由 DAG 的边决定，而不是由 Python 写死。
段的判定、段的记账、根跨度、落库回调全部复用同一批对象（`HazardStages.run_stage`、
`pipeline/span.py`、`ChainBoard`），所以两条路径可被 parity 用例逐项对齐——
`tests/unit/test_chain_workflow_parity.py` 全绿是 chain 退役的前置门。

早退用边条件表达：感知段没命中就不往研判走、研判没出结论就不往决策走，
与固定顺序版"停在两段 + 后四段记 skipped"是同一件事的两种写法。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from aegis.domain.messages import AgentMessage, TelemetryReading, TriggerHit, WarningRecord, new_event_id, new_trace_id
from aegis.pipeline.board import ChainBoard, ChainBoards
from aegis.pipeline.result import ChainResult
from aegis.pipeline.span import run_under_root
from aegis.pipeline.stages import HazardStages
from aegis.services.risk_engine import RiskVerdict
from aegis.workflow.engine import WorkflowEngine

#: 标准五段模板名（定义在 `workflow/templates.py` 的 `standard_chain_template`）。
STANDARD_CHAIN_TEMPLATE = "标准五段处置链路"


def stage_branch(stage: str, board: ChainBoard) -> str:
    """段产物到边条件的映射：早退的判据只在这里写一遍。"""
    if stage == "perceive":
        return "hit" if board.result.hits else "no_hit"
    if stage == "assess":
        return "verdict" if board.result.verdict is not None else "no_verdict"
    return ""


class ChainStations:
    """按 trace 开/收盘，并把"跑某一段"做成注入给工作流节点的可调用。

    单独成对象是因为装配顺序：引擎要有服务、服务要有一段执行口，而引擎又是链路入口要用的——
    把这一段拆出来，容器就能先造 stations、再拿它去装引擎，不必绕圈。
    """

    def __init__(self, stages: HazardStages, boards: ChainBoards | None = None) -> None:
        self._stages = stages
        self._boards = boards or ChainBoards()

    @property
    def boards(self) -> ChainBoards:
        return self._boards

    def open(self, board: ChainBoard) -> ChainBoard:
        return self._boards.attach(board)

    def close(self, trace: str) -> ChainBoard | None:
        return self._boards.close(trace)

    async def __call__(self, stage: str, payload: dict[str, Any]) -> dict[str, Any]:
        """工作流 `chain_stage` 节点的唯一入口。

        盘面找不到属于装配错误：必须响亮抛出（引擎按 on_failure 记成节点失败），
        不能悄悄跑出一段没有归属的结论——那正是"两条路径各记各的账"的开端。
        """
        board = self._boards.get(str(payload.get("board_trace", "")))
        recorded = await self._stages.run_stage(stage, board)
        outcome = recorded.as_dict()
        outcome["branch"] = stage_branch(stage, board)
        outcome["degradations"] = len(board.result.degradations)
        return outcome


class WorkflowChain:
    """工作流编排的链路入口。对外形状与 `HazardResponseChain` 一致。"""

    def __init__(
        self,
        stages: HazardStages,
        engine: WorkflowEngine,
        *,
        stations: ChainStations | None = None,
        template_name: str = STANDARD_CHAIN_TEMPLATE,
        on_result: Callable[[ChainResult], Awaitable[None]] | None = None,
    ) -> None:
        self._stages = stages
        self._engine = engine
        self._stations = stations or ChainStations(stages)
        self._template_name = template_name
        self._on_result = on_result

    @property
    def stages(self) -> HazardStages:
        return self._stages

    @property
    def stations(self) -> ChainStations:
        return self._stations

    @property
    def warnings(self) -> dict[str, WarningRecord]:
        return self._stages.warnings

    async def handle_agent_message(self, message: AgentMessage) -> None:
        await self._stages.handle_agent_message(message)

    async def process(
        self,
        readings: list[TelemetryReading],
        *,
        trace_id: str | None = None,
        event_id: str | None = None,
        region_code: str | None = None,
        preset_hits: list[TriggerHit] | None = None,
        preset_verdict: RiskVerdict | None = None,
        intake: str = "telemetry",
    ) -> ChainResult:
        """开一盘 → 起实例跑模板 → 收盘 → 补 skipped → 落库回调。

        `preset_hits`/`preset_verdict` 与固定顺序版同义（人工上报腿的入口）。它们经盘面传给各段，
        不进实例 payload：payload 是要落库的，而 `TriggerHit`/`RiskVerdict` 是运行时对象。
        """
        trace = trace_id or new_trace_id()
        region = region_code or (readings[0].region_code if readings else "UNKNOWN")
        board = self._stations.open(
            ChainBoard(
                result=ChainResult(trace_id=trace, event_id=event_id or new_event_id()),
                trace=trace,
                region=region,
                readings=readings,
                intake=intake,
                preset_hits=preset_hits,
                preset_verdict=preset_verdict,
            )
        )

        async def body() -> ChainResult:
            try:
                await self._engine.start_from_name(
                    self._template_name, trace_id=trace, payload={"board_trace": board.trace, "source": "hazard_chain"}
                )
            finally:
                self._stations.close(trace)
            board.finish()
            if self._on_result is not None:
                await self._on_result(board.result)
            return board.result

        return await run_under_root(trace, body, region=region, reading_count=len(readings), intake=intake)

    async def process_many(self, grouped: dict[str, list[TelemetryReading]]) -> list[ChainResult]:
        """多区域并发：一区域一个实例、一盘，互不阻塞（与固定顺序版同语义）。"""
        return list(await asyncio.gather(*(self.process(readings, region_code=region) for region, readings in grouped.items())))
