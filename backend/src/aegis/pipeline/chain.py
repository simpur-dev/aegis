"""灾害响应链路的**固定顺序编排器**（M1 五段）。

D7 编排归一（§4.6）后这里只剩三件事：按 `STAGE_ORDER` 的顺序调用段、根跨度 `hazard_chain` 的
结构与状态、收尾（`board.finish()` 补 skipped + `on_result` 落库）。每段"怎么判"与"怎么记自己的账"
在 `pipeline/stages.py`，产物形状在 `pipeline/result.py`，跨段的中转在 `pipeline/board.py`。

工作流的"标准五段"模板（`pipeline/workflow_chain.py` + `workflow` 的 `chain_stage` 节点）
调的是同一个 `HazardStages.run_stage`，所以两条编排路径共用判定与账本样本；
parity 用例 `tests/unit/test_chain_workflow_parity.py` 断的正是这件事，也是 chain 退役的前置门。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from aegis.domain.messages import AgentMessage, TelemetryReading, TriggerHit, WarningRecord, new_event_id, new_trace_id
from aegis.pipeline.board import ChainBoard
from aegis.pipeline.result import ChainResult
from aegis.pipeline.span import run_under_root
from aegis.pipeline.stages import HazardStages
from aegis.services.risk_engine import RiskVerdict


class HazardResponseChain:
    """按固定顺序跑五段。段的判定与记账都在 `HazardStages`——本类不重复一遍。"""

    def __init__(self, stages: HazardStages, *, on_result: Callable[[ChainResult], Awaitable[None]] | None = None) -> None:
        self._stages = stages
        self._on_result = on_result

    @property
    def stages(self) -> HazardStages:
        """段实现本身：工作流节点与 parity 用例持的是同一个对象，不是"复制一份逻辑"。"""
        return self._stages

    @property
    def warnings(self) -> dict[str, WarningRecord]:
        return self._stages.warnings

    async def handle_agent_message(self, message: AgentMessage) -> None:
        """网关上行事件：感知段的智能体触发命中由此进入 `HazardStages`。"""
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
        """一次灾害事件（感知→研判→决策→执行→反馈）= 一条可第三方引用的链路。

        `preset_hits`/`preset_verdict` 是人工上报腿的入口（架构文档 §6.2 场景二）：灾情文本经三路
        融合解析后已经产出触发命中与定级建议，链路从感知段之后**照常走同一条**——同一套量测、
        同一条 trace、同一份降级留痕，而不是平行实现第二条预警链。`intake` 只改感知段的说明文字。
        """
        trace = trace_id or new_trace_id()
        region = region_code or (readings[0].region_code if readings else "UNKNOWN")

        async def body() -> ChainResult:
            board = ChainBoard(
                result=ChainResult(trace_id=trace, event_id=event_id or new_event_id()),
                trace=trace,
                region=region,
                readings=readings,
                intake=intake,
                preset_hits=preset_hits,
                preset_verdict=preset_verdict,
            )
            # 早退口径：无命中→后四段记 skipped（board.finish 补）；研判无结论→就停在两段。
            await self._stages.run_stage("perceive", board)
            if board.result.hits:
                await self._stages.run_stage("assess", board)
                if board.result.verdict is not None:
                    for name in ("plan", "execute", "feedback"):
                        await self._stages.run_stage(name, board)
            board.finish()
            if self._on_result is not None:
                await self._on_result(board.result)
            return board.result

        return await run_under_root(trace, body, region=region, reading_count=len(readings), intake=intake)

    async def process_many(self, grouped: dict[str, list[TelemetryReading]]) -> list[ChainResult]:
        """多区域并发处理（一区域一条链路，互不阻塞）。"""
        return list(await asyncio.gather(*(self.process(readings, region_code=region) for region, readings in grouped.items())))
