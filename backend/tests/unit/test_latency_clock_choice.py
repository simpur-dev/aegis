"""时延测量用哪个时钟——不是风格问题，是读数真假问题。

在 Windows 上 `time.monotonic()` 走 `GetTickCount64()`，步进 **15.6 ms**：拿它量一条
0.3 ms 的语义交互轮次，读数恒为 0.0，`assistant_reply_ms` 的 ≤3s 判定就永远绿（实测：
p50/p95/max/mean 全为 0.0，而同一轮客户端墙钟是 11–33 ms）。账本自己的跨度计时用
`perf_counter`（见 tracer.py 模块说明），测量点却用了另一个时钟，等于两套精度。

另一类更隐蔽：把 `time.perf_counter()` 的起点交给只认 `time.monotonic()` 的下游，
两个不同纪元的数相减，负的那半会被 `max(..., 0)` 夹成"看起来合理"的 0。

这两条都在这里被钉住：源码层规定测量点只能用 perf_counter，行为层要求一轮真交互
必须量出大于 0 的毫秒数。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from aegis.config import Settings
from aegis.container import create_container

SRC = Path(__file__).resolve().parents[2] / "src" / "aegis"

#: 参与时延测量的模块：起点、终点、相减都只能有一个时钟纪元
MEASURING_FILES = (
    "services/assistant.py",
    "services/warning_service.py",
    "pipeline/chain.py",
    "container.py",
)


def _read(relative: str) -> str:
    path = SRC / relative
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        raise AssertionError(f"{relative} 读不到内容：文件挪走了就把这条门禁一起改掉，别删断言")
    return text


@pytest.mark.parametrize("relative", MEASURING_FILES)
def test_测量起点不得用_monotonic纪时钟(relative: str) -> None:
    text = _read(relative)
    bad = re.findall(r"^\s*(?:entered|done|started|started_at|t0)\s*=\s*time\.monotonic\(\)", text, re.MULTILINE)
    assert not bad, f"{relative} 用 time.monotonic() 起表：Windows 上步进 15.6 ms，毫秒级测量会读成 0（{bad}）"


@pytest.mark.parametrize("relative", MEASURING_FILES)
def test_测量点确实用了_perf_counter(relative: str) -> None:
    """正向对照：上一条禁掉了错误时钟，这一条保证正确的那个真的在用——否则两条一起成空文。"""
    assert "time.perf_counter()" in _read(relative), f"{relative} 里找不到 perf_counter：测量点被整体删掉了？"


def test_同一条测量链上不得混用两个时钟纪元() -> None:
    """warning_service 收 chain 传来的 started_at：起点与终点必须同为 perf_counter。"""
    chain = _read("pipeline/chain.py")
    draft = _read("services/warning_service.py")
    assert "started_at = time.perf_counter()" in chain, "链路起点不再用 perf_counter：下游的减法就成了跨纪元相减"
    # 只看赋值取时（文档字符串里讲历史时会提到 monotonic，那不算违规）
    assert re.search(r"=\s*time\.monotonic\(\)", draft) is None, (
        "预警生成段还在用 monotonic 取时：与 started_at 不同纪元，差值会被 max(...,0) 夹成 0"
    )


@pytest.mark.parametrize("relative", MEASURING_FILES)
def test_测量终点不得与起点分属两个时钟(relative: str) -> None:
    """跨纪元相减只能按源码形状判：本机上 monotonic 与 perf_counter 都从开机起算，
    减出来的数可能像个样（实测 0.3 ms 那一轮仍得正数），行为用例抓不住这种错。"""
    text = _read(relative)
    assert re.search(r"time\.monotonic\(\)\s*-\s*(?:started|started_at|entered|t0)\b", text) is None, (
        f"{relative} 用 monotonic 读数去减 perf_counter 起点"
    )


def test_上报接入时长不用墙上时钟() -> None:
    """`utc_now()` 差值 + `max(..., 0)` 的组合会把时钟回拨读成"0 秒接入"，考核项就此永绿。"""
    container = _read("container.py")
    assert "intake_seconds = max(time.perf_counter() - started, 0.0)" in container
    assert "utc_now() - started" not in container, "接入时长又回到墙上时钟：一次 NTP 步进就能把它变成 0 或负数"


@pytest.mark.asyncio
async def test_一轮真交互必须量出大于零的毫秒数() -> None:
    """行为层兜底：一轮真交互量不出毫秒数（恒为 0.0）就说明这个 KPI 根本没在被测。

    它钉得住两类"这个 KPI 没在被测"的失效：读数恒为 0（起点设在了干活之后，或被粗时钟
    量化掉），以及读数荒腔走板（跨纪元相减给出开机时长量级的数）。时钟选型本身仍由
    上面的源码门禁负责——两个时钟在本机都从开机起算，混用的偏差方向不定，行为用例判不住。
    """
    settings = Settings(env="dev", bus_backend="memory", delivery_mode="mock")
    container = create_container(settings, with_simulator=False)
    await container.start(with_mock_agents=False)
    try:
        assert container.assistant is not None, "助手出口没装配：这条门禁测不到东西，请直接报错而不是静默通过"
        frames = [event async for event in container.assistant.respond("最近发布了哪些预警", reporter="时钟门禁")]
        done = frames[-1]
        assert done.type == "done", frames
        elapsed = done.data["latency_ms"]
        assert 0 < elapsed < 5_000, f"内存里一轮交互量出 {elapsed} ms：要么没量到，要么量错了对象"
        stats = container.tracer.ledger.stats("assistant_reply_ms")
        assert stats.count == 1, f"这一轮没进账本：{stats.as_dict()}"
        assert 0 < stats.max < 5_000, f"账本读数 {stats.as_dict()}"
    finally:
        await container.shutdown()
