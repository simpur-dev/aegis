"""NATS JetStream 总线实现（生产运行）。

弱网可靠性依赖 JetStream 持久流 + 服务器端保留策略；幂等去重由平台网关按 msg_id 兜底。
保留与重投不是"传输层默认值"：流的 `max_age`、持久消费者的 `ack_wait`/`max_deliver`
都在这里显式给出，并在订阅前把既存消费者对账到同一口径（见 `_prepare_consumer`）。
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
# `agent.*.in.>` 是本体专属收件箱（`agent.<type>.in.<agent_id>`）：网关按选中本体派发，
# 只改 `agent.*.in` 会让专属收件箱的消息落在流外，durable 订阅直接建不起来。
_STREAMS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("DATA", ("data.>",)),
    ("AGENT", ("agent.*.in", "agent.*.in.>", "agent.*.out", "agent.*.hb")),
    ("WF", ("workflow.>",)),
    ("OPS", ("ops.>", "platform.alert.>")),
    ("REPLY", ("reply.>",)),
)

# 服务端按 msg_id 去重的窗口，单位**秒**：nats-py 序列化时再换算成纳秒，
# 直接传纳秒会被乘第二次（1.2e20）顶出 int64，JetStream 回 "invalid JSON" 拒绝建流。
DUPLICATE_WINDOW_SECONDS = 120

# 流的保留期，单位**秒**（nats-py 序列化时换算成纳秒）。传输层只是投递缓冲，
# 事实源在各仓储；按"过期重放是否有害"分两档：
# - AGENT/REPLY 只留 1h：请求过期后再重放，只会让智能体执行一条平台早已判超时的动作；
# - DATA/WF/OPS 留 24h–7d：断点续传仍能在一个班次内补齐漏收。
# max_msgs 同时兜底条数，两者任一先到都会淘汰旧消息。
_STREAM_MAX_AGE_SECONDS: dict[str, float] = {
    "DATA": 24 * 3600,
    "AGENT": 3600,
    "WF": 24 * 3600,
    "OPS": 7 * 24 * 3600,
    "REPLY": 3600,
}

# 持久消费者的回执窗口，单位**秒**：必须盖住一次协同事务的预算（settings.sla_agent_txn_ms，
# 默认 60s）再加余量。短于预算时，服务端会把"仍在处理"的请求判为未回执并重投，
# 同一动作被执行两遍。装配点（integrations/channels.build_transport）按设置推导后传入。
ACK_WAIT_MARGIN_SECONDS = 5.0
DEFAULT_ACK_WAIT_SECONDS = 60.0 + ACK_WAIT_MARGIN_SECONDS

# 毒消息的最多投递次数：有限次后放弃（消息仍在流里到期淘汰），避免无限重投占满消费者。
DEFAULT_MAX_DELIVER = 5


class NatsBus(BusTransport):
    name = "nats"

    def __init__(
        self,
        url: str = "nats://127.0.0.1:4222",
        *,
        stream_prefix: str = "AEGIS",
        max_reconnect_attempts: int = 30,
        ack_wait_seconds: float = DEFAULT_ACK_WAIT_SECONDS,
        max_deliver: int = DEFAULT_MAX_DELIVER,
    ) -> None:
        super().__init__()
        self._url = url
        self._prefix = stream_prefix
        self._max_reconnect_attempts = max_reconnect_attempts
        self._ack_wait_seconds = float(ack_wait_seconds)
        self._max_deliver = int(max_deliver)
        self._nc: NatsClient | None = None
        self._js: JetStreamContext | None = None
        self._subs: list[Any] = []

    @property
    def ack_wait_seconds(self) -> float:
        return self._ack_wait_seconds

    @property
    def max_deliver(self) -> int:
        return self._max_deliver

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
                max_age=_STREAM_MAX_AGE_SECONDS[suffix],
                max_msgs=1_000_000,
            )
            try:
                await self._js.add_stream(config)
            except NatsAPIError as exc:
                # 既存流的配置与本次不同（典型：新增了本体专属收件箱的 subject）时，add_stream
                # 会以 "already exists" 拒绝；这不是故障，而是需要把流就地升级到新配置。
                failure: Exception = exc
                updater = getattr(self._js, "update_stream", None)
                if callable(updater):
                    try:
                        await updater(config)
                        continue
                    except Exception as update_exc:
                        failure = update_exc
                # 走到这里就是真失败：JetStream 没启用、同名流配置冲突、无响应节点。
                # 把它咽进 debug 会带出一条"已连接但每条发布都失败"的总线——
                # 现场只会看到预警迟迟不出现，而查不到是总线没起来。
                await self.close()
                raise BusNotReadyError(
                    f"NATS 流 {config.name} 创建失败: {failure}",
                    detail={"stream": config.name, "server_code": getattr(failure, "code", None)},
                ) from failure
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

    async def _prepare_consumer(self, stream: str, subject: str, *, durable: str, deliver_group: str | None) -> Any:
        """把持久消费者的回执参数对齐到平台口径，再交给 nats-py 绑定。

        nats-py 在消费者已存在时**丢弃调用方传的 config**（`subscribe` 读回服务端配置），
        所以只在 bind 时传参数，等于让旧消费者永远停在默认值上（ack_wait=30s、无限重投）。
        这里先按 create-or-update 语义对账一次：新建时带上全部参数；既存时只改回执两项，
        并保留原 deliver_subject——其他实例可能正绑在它上面，换 subject 等于把人踢下线。
        `deliver_group` 必须与绑定时传的 queue 一致：带组却没按队列订阅（或反之）会被 nats-py 当场拒绝。
        """
        from nats.js import api as js_api
        from nats.js.errors import NotFoundError

        if self._js is None or self._nc is None:
            raise RuntimeError("NATS 未连接")

        desired = js_api.ConsumerConfig(
            durable_name=durable,
            deliver_group=deliver_group,
            filter_subject=subject,
            ack_policy=js_api.AckPolicy.EXPLICIT,
            ack_wait=self._ack_wait_seconds,
            max_deliver=self._max_deliver,
        )
        try:
            info = await self._js.consumer_info(stream, durable)
        except NotFoundError:
            desired.deliver_subject = self._nc.new_inbox()
            try:
                await self._js.add_consumer(stream, config=desired)
            except Exception as exc:
                raise BusNotReadyError(
                    f"持久消费者 {stream}/{durable} 创建失败: {exc}",
                    detail={"stream": stream, "durable": durable},
                ) from exc
            return desired

        existing = info.config
        if existing.ack_wait == self._ack_wait_seconds and existing.max_deliver == self._max_deliver:
            return existing
        upgraded = existing.evolve(ack_wait=self._ack_wait_seconds, max_deliver=self._max_deliver)
        try:
            await self._js.add_consumer(stream, config=upgraded)
        except Exception as exc:
            raise BusNotReadyError(
                f"持久消费者 {stream}/{durable} 回执参数升级失败: {exc}",
                detail={
                    "stream": stream,
                    "durable": durable,
                    "ack_wait": [existing.ack_wait, self._ack_wait_seconds],
                    "max_deliver": [existing.max_deliver, self._max_deliver],
                },
            ) from exc
        log.info(
            "持久消费者已升级回执参数",
            extra={"stream": stream, "durable": durable, "ack_wait": self._ack_wait_seconds, "max_deliver": self._max_deliver},
        )
        return upgraded

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
            bind_queue = durable if queue else ""
            config = await self._prepare_consumer(stream, subject, durable=durable, deliver_group=bind_queue or None)
            js_sub = await self._js.subscribe(
                subject,
                stream=stream,
                # nats-py 里 queue 就是消费者名：同时给 queue 与 durable 且两者不等会被直接拒绝
                # （"cannot create queue subscription ... to consumer ..."）。一个 durable 消费者
                # 本身就是一个队列组，组名沿用消费者名，多实例用同一 durable 订阅即负载均衡。
                queue=bind_queue,
                durable=durable,
                cb=_cb,
                manual_ack=False,
                config=config,
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
