"""P2 守卫：写动作频控、目标上限、失败熔断，以及 SSE keepalive 读实时停服标志。"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict

from aegis.api.assistant_api import _keepalive
from aegis.domain.operator import Operator, OperatorRole
from aegis.services.assistant.base import (
    Action,
    ActionContext,
    ActionDomain,
    ActionError,
    ActionResult,
    ActionSpec,
    ResolvedTargets,
    TargetRef,
)
from aegis.services.assistant.executor import Executor
from aegis.services.assistant.guard import MAX_TARGETS, FailureBreaker, WriteRateLimiter


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


DUTY_A = Operator(operator_id="duty-01", display_name="值班员甲", role=OperatorRole.DUTY)
DUTY_B = Operator(operator_id="duty-02", display_name="值班员乙", role=OperatorRole.DUTY)


def test_写动作频控每分钟十次且按会话隔离() -> None:
    clock = FakeClock()
    limiter = WriteRateLimiter(clock=clock)
    assert all(limiter.allow("s1") for _ in range(10))
    assert limiter.allow("s2"), "别的会话不该被一起限流"
    assert not limiter.allow("s1")


def test_频控是滑窗过期后恢复而不是永久封禁() -> None:
    clock = FakeClock()
    limiter = WriteRateLimiter(clock=clock)
    for _ in range(10):
        assert limiter.allow("s1")
    clock.advance(61)
    assert limiter.allow("s1")


def test_会话回收时频控计数一并丢掉() -> None:
    limiter = WriteRateLimiter(clock=FakeClock())
    for _ in range(10):
        assert limiter.allow("s1")
    limiter.forget("s1")
    assert limiter.allow("s1")


def test_连续失败三次熔断六十秒后恢复() -> None:
    clock = FakeClock()
    breaker = FailureBreaker(clock=clock)
    breaker.record_failure("drill.start")
    breaker.record_failure("drill.start")
    assert breaker.remaining("drill.start") == 0, "才两次，不该熔断"
    breaker.record_failure("drill.start")
    assert breaker.remaining("drill.start") == pytest.approx(60.0)
    clock.advance(61)
    assert breaker.remaining("drill.start") == 0


def test_成功一次清零同窗口内的失败计数() -> None:
    breaker = FailureBreaker(clock=FakeClock())
    breaker.record_failure("x")
    breaker.record_success("x")
    breaker.record_failure("x")
    breaker.record_failure("x")
    assert breaker.remaining("x") == 0, "成功后重新计数，两次失败还不该熔断"


class _CountParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    count: int = 1


class _BulkRead(Action):
    """批量读动作替身：用来把"解析出 N 个目标"和"执行失败"两件事分开测。"""

    spec = ActionSpec(
        name="demo.bulk_read",
        title="批量读取",
        domain=ActionDomain.QUERY,
        required_role=OperatorRole.DUTY,
        params=_CountParams,
    )

    def __init__(self, count: int = 1, *, error: Exception | None = None) -> None:
        self.count = count
        self.error = error
        self.calls = 0

    async def resolve(self, params: BaseModel, ctx: ActionContext) -> ResolvedTargets:
        return ResolvedTargets(tuple(TargetRef(kind="wrn", id=f"wrn_{index:04d}") for index in range(self.count)))

    async def execute(self, targets: ResolvedTargets, params: BaseModel, ctx: ActionContext) -> ActionResult:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return ActionResult(summary="ok")


@pytest.mark.asyncio
async def test_一次解析超过五十个目标被拒且没有真的执行() -> None:
    action = _BulkRead(MAX_TARGETS + 1)
    with pytest.raises(ActionError) as caught:
        await Executor().run(action, {}, ActionContext(session_id="s"))
    assert caught.value.code == "E_ACTION_TOO_MANY_TARGETS"
    assert action.calls == 0


@pytest.mark.asyncio
async def test_刚好五十个目标放行() -> None:
    action = _BulkRead(MAX_TARGETS)
    outcome = await Executor().run(action, {}, ActionContext(session_id="s"))
    assert outcome.status == "executed"


@pytest.mark.asyncio
async def test_连续失败三次后熔断期内不再尝试执行() -> None:
    clock = FakeClock()
    executor = Executor(breaker=FailureBreaker(clock=clock))
    action = _BulkRead(1, error=RuntimeError("腿断了"))
    ctx = ActionContext(session_id="s")
    for _ in range(3):
        with pytest.raises(ActionError) as caught:
            await executor.run(action, {}, ctx)
        assert caught.value.code == "E_ACTION_EXECUTION_FAILED"
    with pytest.raises(ActionError) as caught:
        await executor.run(action, {}, ctx)
    assert caught.value.code == "E_ACTION_RATE_LIMITED"
    assert action.calls == 3, "熔断期内不能真的再去打那条坏腿"
    clock.advance(61)
    with pytest.raises(ActionError) as caught:
        await executor.run(action, {}, ctx)
    assert caught.value.code == "E_ACTION_EXECUTION_FAILED"
    assert action.calls == 4


@pytest.mark.asyncio
async def test_业务拒绝不参与熔断() -> None:
    executor = Executor(breaker=FailureBreaker(clock=FakeClock()))
    action = _BulkRead(1, error=ActionError("E_TARGET_NOT_FOUND", "目标不存在"))
    ctx = ActionContext(session_id="s")
    for _ in range(3):
        with pytest.raises(ActionError) as caught:
            await executor.run(action, {}, ctx)
        assert caught.value.code == "E_TARGET_NOT_FOUND"
    # 确定性拒绝不是"腿坏了"：第四次仍然进入执行
    with pytest.raises(ActionError):
        await executor.run(action, {}, ctx)
    assert action.calls == 4


@pytest.mark.asyncio
async def test_一个操作者触发熔断不影响其他操作者() -> None:
    """熔断必须按操作者：A 连续失败熔断后，B 调同一动作不应被 A 的熔断状态误伤。"""
    clock = FakeClock()
    executor = Executor(breaker=FailureBreaker(clock=clock))
    failing = _BulkRead(1, error=RuntimeError("腿断了"))
    ctx_a = ActionContext(session_id="s", operator=DUTY_A)
    for _ in range(3):
        with pytest.raises(ActionError) as caught:
            await executor.run(failing, {}, ctx_a)
        assert caught.value.code == "E_ACTION_EXECUTION_FAILED"
    with pytest.raises(ActionError) as caught:
        await executor.run(failing, {}, ctx_a)
    assert caught.value.code == "E_ACTION_RATE_LIMITED", "A 自己应已熔断"

    ctx_b = ActionContext(session_id="s", operator=DUTY_B)
    outcome = await executor.run(_BulkRead(1), {}, ctx_b)
    assert outcome.status == "executed", "B 不应被 A 的熔断牵连"


class _NeverDisconnected:
    async def is_disconnected(self) -> bool:
        return False


@pytest.mark.asyncio
async def test_keepalive读取实时停服标志(monkeypatch: pytest.MonkeyPatch) -> None:
    """捕获静态布尔的旧写法要等满源头的睡眠才会醒；这里必须在一个轮询周期内收摊。"""
    monkeypatch.setattr("aegis.api.assistant_api._DISCONNECT_POLL_SECONDS", 0.01)
    calls: list[bool] = []

    def stopping() -> bool:
        calls.append(True)
        # 前两轮还没关停、第三轮才翻真：一次性求值的旧写法会在开场读到 False 后永远不醒
        return len(calls) >= 3

    async def _idle() -> Any:
        await asyncio.sleep(5)
        yield "never"  # pragma: no cover - 正常路径不会走到这里

    started = time.monotonic()
    frames = [frame async for frame in _keepalive(_idle(), stopping=stopping, request=_NeverDisconnected())]
    elapsed = time.monotonic() - started
    assert frames == []
    assert calls, "keepalive 必须每轮询一次就问一次停服标志"
    assert elapsed < 1.0, f"停服标志没被实时读取：等了 {elapsed:.2f}s"
