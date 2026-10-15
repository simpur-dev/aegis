"""一次链路在编排器之间的中转台：`ChainBoard`。

D7 归一要把五段跑成 DAG 节点，而节点是**分开被调用的**——它们必须看到同一个 `ChainResult`
（研判段的结论要能被决策段读到、降级留痕要累计、链路起点时钟要传给执行段）。
`ChainResult` 不是 JSON，不能在节点 output 里传来传去，所以按 trace 暂存在这里：
谁开的盘谁收盘，`run()` 里用 `finally` 保证不漏（漏一次就是每次预警留一个常驻对象）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from aegis.domain.messages import TelemetryReading, TriggerHit
from aegis.errors import AegisError, ErrorCode
from aegis.pipeline.result import STAGE_ORDER, ChainResult, StageResult
from aegis.services.risk_engine import RiskVerdict


class ChainBoardMissing(AegisError):
    """按 trace 找不到盘面：开盘/收盘的顺序错了（装配问题，不是运行时降级）。

    它必须是 `AegisError` 而不是 `KeyError`——工作流引擎只给类型化错误保留 message，
    否则节点上留下的是"节点执行异常: KeyError"，值班员得自己猜是哪一步没开盘。
    """

    code = ErrorCode.INTERNAL


@dataclass(slots=True)
class ChainBoard:
    """一条链路的进行中状态：产物 + 编排器需要传给各段的入参。

    `started_at` 是链路起点的 perf_counter 读数：`warning_generation_ms`（≤3min）量的就是
    "链路开始→预警就绪"，必须由起点传下来，在生成步里现取会恒为 0。
    用 perf_counter 而不是 monotonic：后者在 Windows 上步进 15.6 ms，毫秒级的段会把 0.3 ms
    量成 0，而那条判定就建在这个数上。
    """

    result: ChainResult
    trace: str
    region: str
    readings: list[TelemetryReading]
    intake: str = "telemetry"
    preset_hits: list[TriggerHit] | None = None
    preset_verdict: RiskVerdict | None = None
    started_at: float = field(default_factory=time.perf_counter)

    @property
    def reported(self) -> bool:
        return self.intake == "report"

    def finish(self) -> ChainResult:
        """收尾口径与原固定链路逐字一致：只在"无触发命中"时把后四段补成 skipped。

        研判无结论那条路径**不补**——原链路在那里就停在两段，把"早退"补成五条 skipped
        会改 `chain_runs.stages` 的长度，而 §4.6 第 4 条要求对外口径不变。
        """
        if not self.result.hits:
            self.result.stages.extend(StageResult(name, "skipped", True, 0.0, "无触发条件命中") for name in STAGE_ORDER[1:])
        return self.result


class ChainBoards:
    """按 trace 暂存盘面。只允许"开—收"成对使用，见 `WorkflowChainRunner.run` 的 finally。"""

    def __init__(self) -> None:
        self._open: dict[str, ChainBoard] = {}

    def attach(self, board: ChainBoard) -> ChainBoard:
        """挂一张已经备好的盘。开/挂/收三个动作同层，调用方不必知道内部字典叫什么。"""
        self._open[board.trace] = board
        return board

    def open(
        self,
        *,
        result: ChainResult,
        trace: str,
        region: str,
        readings: list[TelemetryReading],
        intake: str = "telemetry",
        preset_hits: list[TriggerHit] | None = None,
        preset_verdict: RiskVerdict | None = None,
    ) -> ChainBoard:
        board = ChainBoard(
            result=result,
            trace=trace,
            region=region,
            readings=list(readings),
            intake=intake,
            preset_hits=preset_hits,
            preset_verdict=preset_verdict,
        )
        return self.attach(board)

    def get(self, trace: str) -> ChainBoard:
        board = self._open.get(trace)
        if board is None:
            raise ChainBoardMissing(f"这条 trace 没有开过盘：{trace}", detail={"trace": trace})
        return board

    def close(self, trace: str) -> ChainBoard | None:
        return self._open.pop(trace, None)

    def snapshot(self) -> dict[str, Any]:
        """开着几张盘。漏收盘会在这里露出来，而不是悄悄吃内存。

        当前读者只有 parity 用例（`test_盘面在起图失败时也不漏`）：它验的是"起图抛异常也要收"。
        接进 `/api/v1/status` 让值班员看得见，是 chain 退役那一步（任务 D7-3）要一起做的事——
        在那之前不假装这一行已经上了状态面。
        """
        return {"open_boards": len(self._open), "traces": sorted(self._open)[:5]}
