"""事件循环策略不能被某个测试文件在 import 期永久换掉。

真事故形状：`tests/integration/test_mqtt_live.py` 为了 paho 的 `add_reader/add_writer`，
在模块顶层无条件 `set_event_loop_policy(WindowsSelector...)`。这行在本机整树跑的时候会影响
**同一进程里后续所有文件**的循环实现——后面的时序类断言就跑在另一套循环上，
而这事实在单跑一个文件时永远看不见（Linux 上更不会显形）。
收集顺序是 integration 早于 unit，所以这条放在 unit 里就能接到泄漏。
"""

from __future__ import annotations

import asyncio
import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Selector 策略只在 Windows 上存在，别处无从泄漏")


def test_前面的用例没把事件循环策略换走() -> None:
    policy = type(asyncio.get_event_loop_policy()).__name__
    assert policy != "WindowsSelectorEventLoopPolicy", (
        f"当前策略是 {policy}：某个文件在 import 期换了全局策略且没还原，此后同进程内每个用例跑的都不是默认循环（时序类结论的基线变了）"
    )
