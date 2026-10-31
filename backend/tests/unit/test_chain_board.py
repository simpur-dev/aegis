"""链路盘面的开/收契约：同一 trace 只允许一张开着的盘。

盘面是五段之间传递 `ChainResult` 的唯一通道，覆盖一张开着的盘会让两条链路互相接管状态；
这里钉住"重复开盘必须响亮失败、收盘后可以重开"两条边界。
"""

from __future__ import annotations

import pytest

from aegis.domain.chain import ChainResult
from aegis.domain.messages import new_event_id
from aegis.pipeline.board import ChainBoard, ChainBoardDuplicate, ChainBoards

TRACE = "trc_" + "7" * 16
REGION = "540121"


def board(trace: str = TRACE) -> ChainBoard:
    return ChainBoard(
        result=ChainResult(trace_id=trace, event_id=new_event_id()),
        trace=trace,
        region=REGION,
        readings=[],
    )


def test_重复开盘被拒且原盘不被覆盖() -> None:
    boards = ChainBoards()
    first = boards.open(result=board().result, trace=TRACE, region=REGION, readings=[])

    with pytest.raises(ChainBoardDuplicate, match="已经有一张开着的盘"):
        boards.open(result=board().result, trace=TRACE, region=REGION, readings=[])

    assert boards.get(TRACE) is first, "拒绝之后原盘必须还在，否则后一条链路把自己的失败变成了前一条的丢盘"
    assert boards.snapshot()["open_boards"] == 1


def test_重复挂盘被拒() -> None:
    boards = ChainBoards()
    boards.attach(board())

    with pytest.raises(ChainBoardDuplicate):
        boards.attach(board())

    assert boards.snapshot()["open_boards"] == 1


def test_收盘后可以重开同一trace() -> None:
    """重试/回放会复用 trace：合法路径是"先收再开"，不是覆盖一张仍在跑的盘。"""
    boards = ChainBoards()
    first = boards.open(result=board().result, trace=TRACE, region=REGION, readings=[])
    assert boards.close(TRACE) is first

    second = boards.open(result=board().result, trace=TRACE, region=REGION, readings=[])
    assert boards.get(TRACE) is second
    assert boards.snapshot()["open_boards"] == 1
