"""五段链路的**唯一**编排入口（D7 §4.6 收口）：段的先后由 DAG 的边决定。

退役前这里是"两条路径之一"：`pipeline/chain.py` 用 Python 写死顺序，这里用边表达同一条顺序，
parity 用例逐项对齐两者的可观测物，全绿之后固定顺序那份才删（2026-10-16，过程见 REPORT 的 D7 第 4 步）。
段的判定与记账仍在 `HazardStages.run_stage`（两条路径本来就共用它），根跨度在 `pipeline/span.py`，
跨段中转在 `pipeline/board.py`——收口之后这一层只剩"什么时候跑哪段"这一件事。

早退用边条件表达：感知段没命中就不往研判走、研判没出结论就不往决策走，下游由 `board.finish()`
补 skipped。对外形状（`process` / `process_many` / `handle_agent_message` / `warnings`）沿用退役前，
调用点不需要知道顺序是谁决定的——这也是"退役不改口径"能成立的原因。

"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from aegis.domain.chain import ChainResult
from aegis.domain.messages import AgentMessage, TelemetryReading, TriggerHit, WarningRecord, new_event_id, new_trace_id
from aegis.domain.verdict import RiskVerdict
from aegis.pipeline.board import ChainBoard, ChainBoards
from aegis.pipeline.span import run_under_root
from aegis.pipeline.stages import HazardStages
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
    """链路入口：开一盘、起一次标准五段实例、收盘、补 skipped、落库回调。"""

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
            detail: dict[str, Any] = {}
            try:
                detail = await self._engine.start_from_name(
                    self._template_name, trace_id=trace, payload={"board_trace": board.trace, "source": "hazard_chain"}
                )
            finally:
                self._stations.close(trace)
            board.finish()
            _absorb_instance(board.result, detail)
            if self._on_result is not None:
                await self._on_result(board.result)
            return board.result

        return await run_under_root(trace, body, region=region, reading_count=len(readings), intake=intake)

    async def process_many(self, grouped: dict[str, list[TelemetryReading]]) -> list[ChainResult]:
        """多区域并发：一区域一个实例、一盘，互不阻塞（与固定顺序版同语义）。"""
        return list(await asyncio.gather(*(self.process(readings, region_code=region) for region, readings in grouped.items())))


def _absorb_instance(result: ChainResult, detail: dict[str, Any]) -> None:
    """把实例终态写进链路产物：段账全绿不等于图跑完了。

    引擎按 `on_failure` 中止、或人工节点把实例挂成 waiting 时，`stages` 里只有已跑的那几段，
    而它们各自都是 ok——不把实例终态带出来，`ChainResult.ok` 和 `chain_runs.ok` 就会为一条
    没走完的链路记"成功"。失败原因也一并进 `errors`：实例状态是状态，原因才是值班员要看的。
    """
    status = str(detail.get("status") or "")
    result.instance_status = status
    if status in ("failed", "aborted"):
        reason = str(detail.get("error") or "")
        result.errors.append(f"工作流实例未完成（{status}）：{reason}" if reason else f"工作流实例未完成（{status}）")
