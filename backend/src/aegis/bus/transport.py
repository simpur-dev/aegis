"""总线传输抽象层。

平台与智能体之间的唯一通道。`request()` 在基类实现（订阅回执 subject → 发布 → 限时等待），
替代 NexusMind 的文件轮询 IPC（`app/services/simulation_ipc.py`），把时延量级从秒级轮询降到毫秒级。
"""

from __future__ import annotations

import abc
import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from aegis.domain.messages import AgentMessage, now_iso, parse_iso
from aegis.errors import BusNotReadyError, DeadlineExceededError

try:  # 高性能 JSON：缺失时回退标准库
    import orjson

    def encode(model: AgentMessage) -> bytes:
        return orjson.dumps(model.model_dump(exclude_none=True))

    def decode(raw: bytes) -> AgentMessage:
        return AgentMessage.model_validate(orjson.loads(raw))

except ImportError:  # pragma: no cover
    import json

    def encode(model: AgentMessage) -> bytes:
        return json.dumps(model.model_dump(exclude_none=True)).encode()

    def decode(raw: bytes) -> AgentMessage:
        return AgentMessage.model_validate(json.loads(raw.decode()))


RawCallback = Callable[[bytes], Awaitable[None]]
MessageHandler = Callable[[AgentMessage], Awaitable[AgentMessage | None]]
EventHandler = Callable[[AgentMessage], Awaitable[None]]


@dataclass
class Subscription:
    id: str
    subject: str
    queue: str | None = None
    durable: str | None = None
    delivered: int = 0
    errors: int = 0
    _cancel: Callable[[], Awaitable[None]] = field(repr=False, default=lambda: asyncio.sleep(0))

    async def cancel(self) -> None:
        await self._cancel()


class BusTransport(abc.ABC):
    """传输后端接口。memory 实现用于单测与离线开发，NATS JetStream 用于运行部署。"""

    name: str = "abstract"

    def __init__(self) -> None:
        self._connected = False

    async def __aenter__(self) -> BusTransport:
        await self.connect()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    @property
    def connected(self) -> bool:
        return self._connected

    @abc.abstractmethod
    async def connect(self) -> None: ...

    @abc.abstractmethod
    async def close(self) -> None: ...

    @abc.abstractmethod
    async def _publish_raw(self, subject: str, payload: bytes) -> None: ...

    @abc.abstractmethod
    async def _subscribe_raw(
        self,
        subject: str,
        callback: RawCallback,
        *,
        queue: str | None = None,
        durable: str | None = None,
    ) -> Subscription: ...

    async def publish(self, subject: str, message: AgentMessage) -> None:
        self._require_ready()
        await self._publish_raw(subject, encode(message))

    async def subscribe(
        self,
        subject: str,
        handler: EventHandler,
        *,
        queue: str | None = None,
        durable: str | None = None,
    ) -> Subscription:
        self._require_ready()

        async def _cb(raw: bytes) -> None:
            await handler(decode(raw))

        return await self._subscribe_raw(subject, _cb, queue=queue, durable=durable)

    async def request(
        self,
        subject: str,
        message: AgentMessage,
        *,
        timeout_ms: int | None = None,
    ) -> AgentMessage:
        """请求-响应。超期抛 DeadlineExceededError，由网关记 E_TIMEOUT 并触发 fallback。"""
        self._require_ready()
        if not message.reply_to:
            raise ValueError("request 消息缺少 reply_to")
        budget_ms = timeout_ms or message.deadline_ms or 10_000

        loop = asyncio.get_running_loop()
        future: asyncio.Future[AgentMessage] = loop.create_future()

        async def _on_reply(raw: bytes) -> None:
            if future.done():
                return  # 迟到响应：事务已判超时，丢弃并交由去重层记录
            reply = decode(raw)
            if reply.causation_id != message.msg_id:
                return
            if not future.done():
                future.set_result(reply)

        sub = await self._subscribe_raw(message.reply_to, _on_reply)
        try:
            await self._publish_raw(subject, encode(message))
            try:
                return await asyncio.wait_for(future, timeout=budget_ms / 1000)
            except TimeoutError as exc:
                raise DeadlineExceededError(
                    f"事务超期: action={message.action} subject={subject}",
                    detail={
                        "msg_id": message.msg_id,
                        "trace_id": message.trace_id,
                        "deadline_ms": budget_ms,
                        "sent_at": message.ts,
                        "timed_out_at": now_iso(),
                        "latency_ms": round(
                            (parse_iso(now_iso()) - parse_iso(message.ts)).total_seconds() * 1000,
                            3,
                        ),
                    },
                ) from exc
        finally:
            await sub.cancel()

    def _require_ready(self) -> None:
        if not self._connected:
            raise BusNotReadyError(f"总线未连接: {self.name}")


async def drain_pending(transport: BusTransport, *, timeout: float = 5.0) -> None:
    """等本地在途投递排空——演练/压测计时前必须做一次，否则量到的是"投递还没跑完"而不是链路时延。

    只有内存总线有"本地在途队列"这个概念：NATS JetStream 在 publish 侧就要到 ack，
    客户端没有可排空的东西。所以这里对不支持 idle 的传输直接返回，而不是让脚本
    在换成真实总线时撞一个 AttributeError。
    """
    idle = getattr(transport, "idle", None)
    if callable(idle):
        await idle(timeout=timeout)
