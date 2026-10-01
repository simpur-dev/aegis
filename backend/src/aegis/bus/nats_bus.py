"""NATS JetStream 总线实现（生产运行）。

弱网可靠性依赖 JetStream 持久流 + 服务器端保留策略；幂等去重由平台网关按 msg_id 兜底。
参数名严格对齐 nats-py 真实签名（见 tests/unit/test_nats_api_contract.py 的守卫测试），
避免"只有连上真实 NATS 才暴露"的接口漂移。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from aegis.bus.subjects import subject_matches
from aegis.bus.transport import BusTransport, RawCallback, Subscription
from aegis.errors import BusNotReadyError

if TYPE_CHECKING:
    from nats.aio.client import Client as NatsClient
    from nats.js import JetStreamContext

log = logging.getLogger("aegis.bus.nats")

# 流划分：subject 前缀 → 流后缀。durable 消费者必须绑定到命中的流。
_STREAMS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("DATA", ("data.>",)),
    ("AGENT", ("agent.*.in", "agent.*.out", "agent.*.hb")),
    ("WF", ("workflow.>",)),
    ("OPS", ("ops.>", "platform.alert.>")),
    ("REPLY", ("reply.>",)),
)

# 服务端按 msg_id 去重的窗口，单位**秒**：nats-py 序列化时再换算成纳秒，
# 直接传纳秒会被乘第二次（1.2e20）顶出 int64，JetStream 回 "invalid JSON" 拒绝建流。
DUPLICATE_WINDOW_SECONDS = 120


class NatsBus(BusTransport):
    name = "nats"

    def __init__(
        self,
        url: str = "nats://127.0.0.1:4222",
        *,
        stream_prefix: str = "AEGIS",
        max_reconnect_attempts: int = 30,
    ) -> None:
        super().__init__()
        self._url = url
        self._prefix = stream_prefix
        self._max_reconnect_attempts = max_reconnect_attempts
        self._nc: NatsClient | None = None
        self._js: JetStreamContext | None = None
        self._subs: list[Any] = []

    # ---------- 生命周期 ----------

    async def connect(self) -> None:
        try:
            import nats
            from nats.js import api as js_api
            from nats.js.errors import APIError as NatsAPIError
        except ImportError as exc:  # pragma: no cover - 依赖缺失时给出明确指引
            raise RuntimeError("NATS 总线需要 nats-py 依赖：uv sync --extra nats") from exc

        self._nc = await nats.connect(
            self._url,
            error_cb=self._on_error,
            allow_reconnect=True,
            max_reconnect_attempts=self._max_reconnect_attempts,
        )
        self._js = self._nc.jetstream()
        for suffix, patterns in _STREAMS:
            config = js_api.StreamConfig(
                name=f"{self._prefix}_{suffix}",
                subjects=list(patterns),
                duplicate_window=DUPLICATE_WINDOW_SECONDS,
                max_msgs=1_000_000,
            )
            try:
                await self._js.add_stream(config)
            except NatsAPIError as exc:
                # STREAM.CREATE 对"同名同配置的既存流"是幂等的（服务端直接 200）。
                # 因此走到这里就是真失败：JetStream 没启用、同名流配置冲突、无响应节点。
                # 把它咽进 debug 会带出一条"已连接但每条发布都失败"的总线——
                # 现场只会看到预警迟迟不出现，而查不到是总线没起来。
                await self.close()
                raise BusNotReadyError(
                    f"NATS 流 {config.name} 创建失败: {exc}",
                    detail={"stream": config.name, "server_code": getattr(exc, "code", None)},
                ) from exc
        self._connected = True
        log.info("NATS 已连接", extra={"url": self._url})

    async def close(self) -> None:
        for sub in list(self._subs):
            try:
                await sub.drain()
            except Exception as exc:  # pragma: no cover - 关停期异常不外抛
                log.warning("订阅 drain 失败", extra={"error": str(exc)})
        self._subs.clear()
        if self._nc is not None:
            await self._nc.close()
        self._connected = False

    @staticmethod
    async def _on_error(exc: Exception) -> None:
        # 必须是协程：nats-py 在 connect() 里就地校验回调类型，同步函数会抛
        # InvalidCallbackTypeError——真实总线连不上，而替身客户端永远发现不了。
        log.error("NATS 连接异常", extra={"error": str(exc)})

    # ---------- 收发 ----------

    def stream_for(self, subject: str) -> str | None:
        """按 subject 命中流名；durable 消费者必须指名所属流。"""
        for suffix, patterns in _STREAMS:
            if any(subject_matches(pattern, subject) for pattern in patterns):
                return f"{self._prefix}_{suffix}"
        return None

    async def _publish_raw(self, subject: str, payload: bytes) -> None:
        if self._js is None:
            raise RuntimeError("NATS 未连接")
        await self._js.publish(subject, payload)

    async def _subscribe_raw(
        self,
        subject: str,
        callback: RawCallback,
        *,
        queue: str | None = None,
        durable: str | None = None,
    ) -> Subscription:
        if self._nc is None:
            raise RuntimeError("NATS 未连接")

        async def _cb(msg: Any) -> None:
            await callback(msg.data)

        sub = Subscription(id=f"nats_{id(_cb)}", subject=subject, queue=queue, durable=durable)

        if durable:
            if self._js is None:
                raise RuntimeError("NATS 未连接")
            stream = self.stream_for(subject)
            if stream is None:
                raise ValueError(f"durable 订阅的 subject 未归属任何流: {subject}")
            js_sub = await self._js.subscribe(
                subject,
                stream=stream,
                # nats-py 里 queue 就是消费者名：同时给 queue 与 durable 且两者不等会被直接拒绝
                # （"cannot create queue subscription ... to consumer ..."）。一个 durable 消费者
                # 本身就是一个队列组，组名沿用消费者名，多实例用同一 durable 订阅即负载均衡。
                queue=durable if queue else "",
                durable=durable,
                cb=_cb,
                manual_ack=False,
            )
            self._subs.append(js_sub)

            async def _cancel_durable() -> None:
                await js_sub.drain()
                if js_sub in self._subs:
                    self._subs.remove(js_sub)

            sub._cancel = _cancel_durable
            return sub

        core_sub = await self._nc.subscribe(subject, queue=queue or "", cb=_cb)
        self._subs.append(core_sub)

        async def _cancel_core() -> None:
            await core_sub.drain()
            if core_sub in self._subs:
                self._subs.remove(core_sub)

        sub._cancel = _cancel_core
        return sub
