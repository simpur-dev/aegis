"""灾种模块在总线上寻址兄弟本体的上下文。

闭环（Manager）原来在**进程内直接调用**五个专业 Agent 的 `main` 函数。那条接法绕开了
平台智能体层的整套契约：没有消息信封、没有 deadline、没有 `msg_id` 幂等、没有类型化
错误、没有时延埋点——等于在自己包里私开了一套接线，平台层的门禁与观测全都照不到它。

本模块把这条接线换成总线：闭环按 **capability** 找到兄弟本体的 `agent_id`，发一条标准
请求，等一条标准回执。这样一来：

- 兄弟本体可以跑在**别的进程**里（平台的 `AEGIS_AGENT_RUNTIME=off` 形态就是这么用的）；
- 每次调用都留下信封级的可观测记录与时延埋点；
- 失败走的是平台统一的错误分类（超时 / 无承接方 / 本体报错），闭环据此决定重试或跳过。

**兄弟地址由装配层注入**（工厂知道谁注册成什么 id），不由被调方自己声明——"我该找谁"
是装配事实，不是运行时猜测。查不到就如实报错：闭环宁可记一次失败，也不该悄悄退回到
进程内直调，那等于把刚拆掉的旁路又接回去。

**预算传导**：外层请求的 `deadline_ms` 是整条闭环的总预算（五腿串行 + 重试都要从
这里面出）。`BusContext` 记下起点时刻，每条腿的预算 = min(单腿上限, 剩余预算)——
重试再多也不允许超过外层承诺，预算耗尽就快速失败，而不是让每条腿都磨满 65s 后
由外层超时砍掉（那会留下占着执行器线程的僵尸闭环）。
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import time
from collections.abc import Mapping
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any

from aegis.bus import subjects
from aegis.bus.transport import BusTransport
from aegis.domain.enums import Action, AgentType, MessageKind
from aegis.domain.messages import make_request
from aegis.errors import AegisError, DeadlineExceededError, ErrorCode

log = logging.getLogger("aegis.agents.hazards.bus_context")

#: 闭环内部派单的回复口前缀。与网关的 `reply.gw*` 分开：两者的并发量级与生命周期不同，
#: 混用会让网关的序号空间被闭环的事务撑大，排查时也分不清回执是谁的。
_INBOX_SEQ = itertools.count(1)

#: 单腿预算的默认值与上限。五腿串行时 5×60s=300s 恰好是外层默认预算——没有余量，
#: 任何节点开销都会压线，所以单腿默认 60s 但**必须**被外层剩余预算收缩。
_LEG_BUDGET_DEFAULT_MS = 60_000
#: 外层预算到单腿预算的拆分系数：五腿串行，每腿至多拿总预算的 1/5。
_LEGS_PER_LOOP = 5
#: 剩余预算低于这个值就不再派腿：一次派单连网关的最低 deadline（100ms）都撑不起。
_MIN_USABLE_BUDGET_MS = 1_000

#: 当前协程链上的总线上下文。LangGraph 的节点跑在 executor 线程里，拿不到外层的
#: 局部变量；用 ContextVar 把"这一轮闭环该往哪派"带进节点。取不到就是"这轮闭环
#: 走本地直调"（离线自测/单机演示），不是错误。
_BUS_CONTEXT: ContextVar[BusContext | None] = ContextVar("aegis_hazard_bus_context", default=None)


class LoopDispatchTimeout(AegisError):
    """闭环内部派单超期。

    单独成类是因为闭环要把"超期"与"执行失败"分开记账（`AGENT_TIMEOUT` vs
    `AGENT_EXECUTION_ERROR`）——按消息文本猜错型（`"timeout" in str(e)`）在中文
    消息下必然失手。
    """

    code = ErrorCode.TIMEOUT


def new_loop_inbox() -> str:
    """给闭环的一次内部派单分配独立回执口（并发事务不能共用一个 inbox）。"""
    return f"reply.ld{next(_INBOX_SEQ):08d}.inbox"


def set_bus_context(context: BusContext | None) -> Token:
    """把总线上下文挂到当前协程链上，返回还原用的 token。"""
    return _BUS_CONTEXT.set(context)


def reset_bus_context(token: Token) -> None:
    _BUS_CONTEXT.reset(token)


def current_bus_context() -> BusContext | None:
    return _BUS_CONTEXT.get()


@dataclass(frozen=True, slots=True)
class SiblingRef:
    """一个兄弟本体的地址：它承接哪项能力、属于哪一段、注册成什么 id。"""

    capability: str
    agent_type: AgentType
    agent_id: str


@dataclass(frozen=True, slots=True)
class BusContext:
    """闭环寻址兄弟本体的上下文。

    `outer_deadline_ms` 是整条闭环的总预算（来自外层请求的 `deadline_ms`）；
    `started_mono` 记预算起点。两者一起把"剩余预算"变成每次派单都能算的量。
    """

    transport: BusTransport
    caller_id: str
    siblings: Mapping[str, SiblingRef]
    deadline_ms: int = _LEG_BUDGET_DEFAULT_MS
    #: 平台事件循环。闭环图是同步的（LangGraph 节点跑在 executor 线程里），总线请求
    #: 必须回到平台自己的循环上发——总线与循环是绑定的，在别的循环里发等于换了一条线。
    loop: asyncio.AbstractEventLoop | None = None
    #: 外层总预算与起点；None 表示调用方没有约束（离线自测），此时只用单腿默认预算。
    outer_deadline_ms: int | None = None
    started_mono: float = field(default_factory=time.monotonic)

    def remaining_budget_ms(self) -> int | None:
        """外层预算还剩多少毫秒；没有外层约束时返回 None（不受限）。"""
        if self.outer_deadline_ms is None:
            return None
        elapsed = (time.monotonic() - self.started_mono) * 1000
        return max(0, int(self.outer_deadline_ms - elapsed))

    def leg_budget_ms(self) -> int:
        """本次派单可用的单腿预算：单腿默认值与剩余预算取小者。

        剩余预算按五腿拆分（`_LEGS_PER_LOOP`）——不能把剩余全押在一条腿上，
        否则第一条腿磨完预算，后面四条腿连失败的机会都没有。
        """
        remaining = self.remaining_budget_ms()
        budget = min(self.deadline_ms, max(1, (remaining // _LEGS_PER_LOOP) if remaining is not None else self.deadline_ms))
        return max(1_000, int(budget)) if remaining is None or remaining > _MIN_USABLE_BUDGET_MS else 1_000

    def ensure_budget(self) -> int:
        """派腿前先看预算还在不在；耗尽就快速失败，不发起注定超期的请求。"""
        remaining = self.remaining_budget_ms()
        if remaining is not None and remaining <= _MIN_USABLE_BUDGET_MS:
            raise AegisError(
                f"闭环预算已耗尽（外层 {self.outer_deadline_ms}ms，已用 {int(self.outer_deadline_ms - remaining)}ms）：不再派腿",
                detail={"caller": self.caller_id, "outer_deadline_ms": self.outer_deadline_ms},
                retryable=False,
            )
        return self.leg_budget_ms()

    async def request(
        self,
        capability: str,
        action: Action,
        payload: dict[str, Any],
        *,
        trace_id: str,
        timeout_ms: int | None = None,
    ) -> dict[str, Any]:
        """向承接 `capability` 的兄弟发一条标准请求，返回它的应答载荷。

        失败一律抛 `AegisError`：闭环的 `dispatch_node` 统一把它转成错误记录，
        由 `retry_decision` 决定重试还是跳过——错误不被吞掉，也不让闭环自己发明的
        异常类型漏到平台层。
        """
        ref = self.siblings.get(capability)
        if ref is None:
            raise AegisError(
                f"闭环没有登记 {capability} 的承接本体：这条腿接不到，不退回进程内直调",
                detail={"capability": capability, "caller": self.caller_id},
            )
        budget = int(timeout_ms or self.deadline_ms)
        request = make_request(
            source=self.caller_id,
            target=ref.agent_id,
            action=action.value,
            payload=payload,
            trace_id=trace_id,
            reply_to=new_loop_inbox(),
            deadline_ms=budget,
        )
        try:
            reply = await self.transport.request(
                subjects.agent_in(ref.agent_type, ref.agent_id),
                request,
                timeout_ms=budget,
            )
        except DeadlineExceededError as exc:
            raise LoopDispatchTimeout(
                f"闭环派单超期：capability={capability} agent={ref.agent_id}",
                detail={"capability": capability, "agent_id": ref.agent_id, "deadline_ms": budget},
                retryable=True,
            ) from exc
        if reply.kind is MessageKind.ERROR:
            raise AegisError(
                f"承接方报错：capability={capability} agent={ref.agent_id}：{reply.payload.get('message', '')}"[:300],
                detail={"capability": capability, "agent_id": ref.agent_id, "payload": dict(reply.payload)},
                retryable=bool(reply.payload.get("retryable", True)),
            )
        return dict(reply.payload)

    def request_sync(
        self,
        capability: str,
        action: Action,
        payload: dict[str, Any],
        *,
        trace_id: str,
        timeout_ms: int | None = None,
    ) -> dict[str, Any]:
        """同步侧入口：闭环图是同步的，节点跑在 executor 线程里。

        总线请求必须回到平台自己的事件循环上发（总线与循环绑定），所以这里把协程
        投递回那个循环并等它出结果——当前线程让出的是"等一次总线事务"的时间，不是
        事件循环本身，不会把平台堵死。

        预算口径：单腿预算受**剩余预算**收缩（`ensure_budget` / `leg_budget_ms`）；
        预算耗尽直接快速失败。等待上限比 transport 预算略宽 1s——transport 超时会先
        到，协程随即自我收尾，不会留下继续跑的幽灵请求。
        """
        if self.loop is None:
            raise AegisError(
                "闭环没有绑定平台事件循环：总线请求无处可发",
                detail={"capability": capability, "caller": self.caller_id},
            )
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self.loop:
            raise AegisError(
                "request_sync 在平台事件循环线程内被调用：future.result 会堵死循环、协程永远得不到调度。"
                "闭环图必须跑在 executor 线程里（run_blocking_on 的专用执行器）",
                detail={"capability": capability, "caller": self.caller_id},
            )
        budget = self.ensure_budget() if timeout_ms is None else int(timeout_ms)
        coro = self.request(capability, action, payload, trace_id=trace_id, timeout_ms=budget)
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        try:
            return future.result(timeout=budget / 1000 + 1)
        except TimeoutError as exc:
            raise LoopDispatchTimeout(
                f"闭环派单未在预算内返回：capability={capability} 预算 {budget}ms",
                detail={"capability": capability, "budget_ms": budget},
                retryable=True,
            ) from exc


__all__ = [
    "BusContext",
    "LoopDispatchTimeout",
    "SiblingRef",
    "current_bus_context",
    "new_loop_inbox",
    "reset_bus_context",
    "set_bus_context",
]
