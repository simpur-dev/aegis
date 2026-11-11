"""共享熔断内核的门禁：状态机边界、冷却恢复、失败分类口径。

为什么单独一份：熔断是**跨切关注点**——失败计数、冷却窗口、试探名额——它一旦有两条
实现（平台一份、灾种一份），"上游挂了"这件事在两侧就会给出两种行为。灾种侧那份在
2026-11-10 就已带冷却窗口，平台侧那份却是"一开闸就永久跳过"（上游恢复了也永远用不上）。
现在只有 `agents/core/breaker.py` 一份实现，本文件是它的行为契约。

`clock` 可注入：冷却窗口是时间相关的状态机，边界必须能**确定性地**测出来，
不能靠 sleep 赌。
"""

from __future__ import annotations

import httpx
import openai

from aegis.agents.core.breaker import LlmBreaker, LlmUnavailableError, is_transport_failure


class _Clock:
    """可推进的假时钟。"""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _status_error(status: int, message: str = "provider error") -> Exception:
    request = httpx.Request("POST", "https://provider.invalid/chat/completions")
    response = httpx.Response(status, request=request)
    return openai.APIStatusError(message, response=response, body=None)


class TestStateMachine:
    def test_闭合态放行(self) -> None:
        breaker = LlmBreaker(threshold=2, cooldown_seconds=60.0)
        assert breaker.acquire() is True
        assert breaker.state() == "closed"

    def test_达阈值开闸(self) -> None:
        breaker = LlmBreaker(threshold=2, cooldown_seconds=60.0)
        breaker.note_failure()
        assert breaker.state() == "closed", "未达阈值不该开闸"
        breaker.note_failure()
        assert breaker.state() == "open"

    def test_开闸后冷却窗口内拒绝(self) -> None:
        clock = _Clock()
        breaker = LlmBreaker(threshold=1, cooldown_seconds=60.0, clock=clock)
        breaker.note_failure()
        assert breaker.acquire() is False, "冷却窗口内必须拒绝：调用方该立刻走确定性兜底"
        assert breaker.available() is False

    def test_冷却窗口到期放行一次试探(self) -> None:
        clock = _Clock()
        breaker = LlmBreaker(threshold=1, cooldown_seconds=60.0, clock=clock)
        breaker.note_failure()
        clock.advance(59.0)
        assert breaker.acquire() is False, "窗口没过就该拒绝"
        clock.advance(2.0)
        assert breaker.acquire() is True, "窗口已过：放行一次试探"
        assert breaker.state() == "half_open"

    def test_试探名额只放一个(self) -> None:
        clock = _Clock()
        breaker = LlmBreaker(threshold=1, cooldown_seconds=60.0, clock=clock)
        breaker.note_failure()
        clock.advance(61.0)
        assert breaker.acquire() is True, "第一个拿到试探名额"
        assert breaker.acquire() is False, "恢复瞬间不该被打穿：其余继续拒绝"

    def test_试探成功即闭合(self) -> None:
        clock = _Clock()
        breaker = LlmBreaker(threshold=1, cooldown_seconds=60.0, clock=clock)
        breaker.note_failure()
        clock.advance(61.0)
        breaker.acquire()
        breaker.note_success()
        assert breaker.state() == "closed"
        assert breaker.failures == 0
        assert breaker.acquire() is True

    def test_试探失败重新开闸并重新计时(self) -> None:
        clock = _Clock()
        breaker = LlmBreaker(threshold=1, cooldown_seconds=60.0, clock=clock)
        breaker.note_failure()
        clock.advance(61.0)
        breaker.acquire()
        breaker.note_failure()
        assert breaker.state() == "open"
        clock.advance(30.0)
        assert breaker.acquire() is False, "重新开闸后冷却窗口从头算"

    def test_可用性查询不改状态也不占名额(self) -> None:
        """平台网关要先筛候选再逐个尝试——筛选阶段占掉试探名额会让并发试探互相挤掉。"""
        clock = _Clock()
        breaker = LlmBreaker(threshold=1, cooldown_seconds=60.0, clock=clock)
        breaker.note_failure()
        clock.advance(61.0)
        assert breaker.available() is True
        assert breaker.state() == "open", "纯查询不该把状态改成半开"
        assert breaker.acquire() is True, "名额还在，筛选没占掉"
        assert breaker.acquire() is False

    def test_复位等于一次成功(self) -> None:
        breaker = LlmBreaker(threshold=1)
        breaker.note_failure()
        assert breaker.state() == "open"
        breaker.reset()
        assert breaker.state() == "closed"
        assert breaker.failures == 0

    def test_按键分账互不干扰(self) -> None:
        breaker = LlmBreaker(threshold=1)
        breaker.note_failure("qwen-plus")
        assert breaker.state("qwen-plus") == "open"
        assert breaker.state("qwen-max") == "closed", "另一个模型不该被这条账拖累"
        assert breaker.failures_for("qwen-plus") == 1
        assert breaker.failures_for("qwen-max") == 0

    def test_并发下试探名额只放一个(self) -> None:
        import threading

        clock = _Clock()
        breaker = LlmBreaker(threshold=1, cooldown_seconds=60.0, clock=clock)
        breaker.note_failure()
        clock.advance(61.0)
        granted: list[bool] = []
        lock = threading.Lock()

        def worker() -> None:
            ok = breaker.acquire()
            with lock:
                granted.append(ok)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert sum(1 for ok in granted if ok) == 1, f"试探名额应只放一个，实际放了 {sum(granted)}"


class TestFailureClassification:
    """哪些失败该让模型下架，哪些不该。"""

    def test_连接错误计入熔断(self) -> None:
        assert is_transport_failure(openai.APIConnectionError(request=httpx.Request("POST", "https://x.invalid"))) is True

    def test_超时计入熔断(self) -> None:
        assert is_transport_failure(openai.APITimeoutError(request=httpx.Request("POST", "https://x.invalid"))) is True

    def test_5xx计入熔断(self) -> None:
        assert is_transport_failure(_status_error(503)) is True

    def test_429计入熔断(self) -> None:
        assert is_transport_failure(_status_error(429)) is True

    def test_4xx不计入熔断(self) -> None:
        """4xx 是请求本身错了：重试没有意义，却会让模型被永久下架。"""
        assert is_transport_failure(_status_error(400)) is False
        assert is_transport_failure(_status_error(401)) is False
        assert is_transport_failure(_status_error(404)) is False

    def test_语义错误不计入熔断(self) -> None:
        assert is_transport_failure(ValueError("模型返回的 JSON 非法")) is False

    def test_超时异常计入熔断(self) -> None:
        assert is_transport_failure(TimeoutError("读超时")) is True


class TestUnavailableError:
    def test_带错误码且是异常(self) -> None:
        from aegis.errors import ErrorCode

        err = LlmUnavailableError("LLM 熔断中")
        assert isinstance(err, Exception)
        assert err.code is ErrorCode.NOT_READY
