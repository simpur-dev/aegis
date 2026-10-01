"""MQTT 推送腿接入：站端/边缘网关 publish → 平台 subscribe → 摄取轮次取空。

为什么中间必须有一段有界缓冲：`DataSource` 契约是拉取式（每轮 `collect()` 一次），
MQTT 是推送式。站端突发（暴雨期几十个站同报）如果直接进事件循环，会把摄取轮次拖爆，
而 `IngestService` 的 per-source 超时会把整轮判失败。所以这里：
    回调 → 解析 → 有界队列（满则丢最旧并计数）→ 摄取轮次取空。
丢弃必须可计数、可对外看见；静默丢数比丢数本身更糟。

载荷形状（本文件的唯一约定，用例逐条钉住）：
    topic: {prefix}/{station_id}[/{子路径}]                例 field/RG-540121-01
    body : {"region_code": "540121", "observed_at": "…Z",
            "metrics": {"rain_10min": 12.5, "debris_level": 0.4}}      # 批量
    或   {"region_code": "540121", "observed_at": "…Z",
          "metric": "rain_10min", "value": 12.5}                        # 单条
未登记的观测量不丢弃：单位记成 `unknown`（见 connectors.metrics）。现场增加传感器时
"先接进来、单位待补"比"消息被平台吃掉"有价值得多。

平台运行限制：aiomqtt 走 paho 的 asyncio 支持，依赖 `loop.add_reader/add_writer`。
Linux（生产容器部署）天然可用；Windows 上必须换 Selector 事件循环，否则会收到一个
光秃秃的 `NotImplementedError`——本模块会把它翻译成可操作的提示写进 `last_error`。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC
from typing import Any, Protocol

from aegis.connectors.base import DataSource
from aegis.connectors.metrics import unit_for
from aegis.domain.messages import TelemetryReading, now_iso, parse_iso, utc_now

log = logging.getLogger("aegis.connectors.mqtt")

MessageSink = Callable[[str, bytes], Awaitable[None]]
DEFAULT_TOPIC_PREFIX = "field"
# 温度类允许负值（高原夜间），其余观测量出现负值基本是传感器故障，标 suspect 让下游自己判
_NEGATIVE_OK = frozenset({"air_temperature_c", "freeze_thaw_cycles"})


class MqttPayloadError(ValueError):
    """载荷不符合约定：按原因计数，不进摄取链路。"""


@dataclass(slots=True)
class MqttCounters:
    """推送侧的台账。全部是纯内存计数，读它不触网、不阻塞。"""

    received: int = 0
    parsed_readings: int = 0
    rejected: dict[str, int] = field(default_factory=dict)
    dropped_overflow: int = 0

    def note_reject(self, reason: str) -> None:
        self.rejected[reason] = self.rejected.get(reason, 0) + 1

    def as_dict(self) -> dict[str, object]:
        return {
            "received_messages": self.received,
            "parsed_readings": self.parsed_readings,
            "rejected": dict(sorted(self.rejected.items())),
            "dropped_overflow": self.dropped_overflow,
        }


def station_from_topic(topic: str, prefix: str = DEFAULT_TOPIC_PREFIX) -> str:
    """取 topic 里紧跟前缀的那一段作为站点编号；前缀不匹配返回空串。"""
    parts = [segment for segment in (topic or "").strip().lstrip("/").split("/") if segment]
    head = prefix.strip("/") if prefix else ""
    if head:
        expected = head.split("/")
        if parts[: len(expected)] != expected:
            return ""
        parts = parts[len(expected) :]
    return parts[0] if parts else ""


def _observed_at(body: dict[str, Any]) -> str:
    raw = body.get("observed_at")
    if isinstance(raw, str) and raw:
        try:
            moment = parse_iso(raw)
        except ValueError:
            raise MqttPayloadError("observed_at 不是合法 ISO8601") from None
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return utc_now().isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _pairs_from(body: dict[str, Any]) -> list[tuple[str, Any]]:
    metrics = body.get("metrics")
    if isinstance(metrics, dict):
        return [(str(key), value) for key, value in metrics.items()]
    if "metric" in body or "value" in body:
        metric = body.get("metric")
        if not isinstance(metric, str) or not metric:
            raise MqttPayloadError("单条形状的 metric 必须是非空字符串")
        return [(metric, body.get("value"))]
    raise MqttPayloadError("载荷既没有 metrics 也没有 metric/value")


def readings_from_mqtt(
    topic: str, payload: bytes | str, *, prefix: str = DEFAULT_TOPIC_PREFIX, source: str = "mqtt"
) -> list[TelemetryReading]:
    """把一条 MQTT 消息解析成读数列表。不合约定的地方一律抛 `MqttPayloadError(原因)`。"""
    station_id = station_from_topic(topic, prefix)
    if not station_id:
        raise MqttPayloadError("topic 不含站点段（应为 {prefix}/{station_id}）")

    try:
        text = payload.decode() if isinstance(payload, bytes) else payload
        body = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MqttPayloadError(f"载荷不是合法 JSON: {type(exc).__name__}") from None
    if not isinstance(body, dict):
        raise MqttPayloadError("载荷顶层必须是对象")

    region_code = str(body.get("region_code") or "").strip().upper()
    if len(region_code) < 6:
        raise MqttPayloadError("region_code 缺失或短于 6 位行政区划码")
    observed = _observed_at(body)

    readings: list[TelemetryReading] = []
    for metric, raw_value in _pairs_from(body):
        if raw_value is None or isinstance(raw_value, bool):
            continue
        try:
            value = float(raw_value)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(value):
            continue
        readings.append(
            TelemetryReading(
                station_id=station_id,
                metric=metric,
                value=value,
                unit=unit_for(metric),
                region_code=region_code[:24],
                observed_at=observed,
                ingested_at=now_iso(),
                source=source,
                quality_flag="ok" if value >= 0 or metric in _NEGATIVE_OK else "suspect",
            )
        )
    if not readings:
        raise MqttPayloadError("载荷里没有任何可用的数值观测量")
    return readings


class MqttClientPort(Protocol):
    """MQTT 客户端面：适配器只认这三个成员，aiomqtt 的细节留在实现里。

    拆成协议而不是直接在 `MqttSource` 里 import aiomqtt，为的是让解析与背压这两件真正
    容易出错的事在无 broker 的环境下被测到（也包括"连接一直失败"这种故障形状）。
    """

    async def start(self, sink: MessageSink) -> None: ...

    async def stop(self) -> None: ...

    @property
    def connected(self) -> bool: ...

    def status(self) -> dict[str, object]: ...


class MqttSource(DataSource):
    """推送转拉取的桥：`_on_message` 入队，`collect()` 取空。"""

    name = "mqtt"

    def __init__(self, client: MqttClientPort, *, prefix: str = DEFAULT_TOPIC_PREFIX, buffer_limit: int = 5_000) -> None:
        if buffer_limit <= 0:
            raise ValueError(f"buffer_limit 必须为正: {buffer_limit}")
        self._client = client
        self._prefix = prefix
        self._limit = buffer_limit
        self._queue: deque[TelemetryReading] = deque()
        self._counters = MqttCounters()
        self._started = False

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        await self._client.start(self._on_message)

    async def stop(self) -> None:
        if not self._started:
            return
        self._started = False
        await self._client.stop()

    async def collect(self) -> list[TelemetryReading]:
        """取空当前缓冲：一次取完，同一轮内不会重复上报。"""
        drained: list[TelemetryReading] = []
        while self._queue:
            drained.append(self._queue.popleft())
        return drained

    async def _on_message(self, topic: str, payload: bytes) -> None:
        self._counters.received += 1
        try:
            readings = readings_from_mqtt(topic, payload, prefix=self._prefix)
        except MqttPayloadError as exc:
            self._counters.note_reject(str(exc))
            log.debug("MQTT 载荷被拒", extra={"topic": topic[:120], "reason": str(exc)})
            return
        for reading in readings:
            if len(self._queue) >= self._limit:
                # 丢最旧保最新：预警判的是"现在有没有超阈值"，旧读数已被上一轮用过或已过期
                self._queue.popleft()
                self._counters.dropped_overflow += 1
            self._queue.append(reading)
            self._counters.parsed_readings += 1

    def status(self) -> dict[str, object]:
        return {
            "connected": self._client.connected,
            "buffered": len(self._queue),
            "buffer_limit": self._limit,
            **self._client.status(),
            **self._counters.as_dict(),
        }


def _describe_failure(exc: Exception) -> str:
    """把两种"看起来像 bug 其实是环境"的失败翻译成人能照着改的话。"""
    if isinstance(exc, NotImplementedError):
        return (
            "NotImplementedError: 当前事件循环不支持 add_reader/add_writer —— Windows 需改用 Selector 事件循环（生产 Linux 容器无此限制）"
        )
    return f"{type(exc).__name__}: {exc}"


class AiomqttClient:
    """aiomqtt 实现的 `MqttClientPort`：断线指数退避重连，凭据只在连接参数里、不进状态面。"""

    def __init__(
        self,
        *,
        host: str,
        port: int = 1883,
        topic_prefix: str = DEFAULT_TOPIC_PREFIX,
        username: str = "",
        password: str = "",
        qos: int = 1,
        keepalive_seconds: int = 30,
        client_id: str = "",
        incoming_queue_limit: int = 5_000,
        backoff_base_seconds: float = 0.5,
        backoff_cap_seconds: float = 30.0,
    ) -> None:
        if not host:
            raise ValueError("MQTT 接入需要主机地址")
        self._host = host
        self._port = int(port)
        self._filter = f"{topic_prefix.strip('/')}/#"
        self._username = username or None
        self._password = password or None
        self._qos = max(0, min(2, int(qos)))
        self._keepalive = int(keepalive_seconds)
        self._client_id = client_id or None
        self._queue_limit = max(1, int(incoming_queue_limit))
        self._backoff_base = backoff_base_seconds
        self._backoff_cap = backoff_cap_seconds
        self._task: asyncio.Task[None] | None = None
        self._sink: MessageSink | None = None
        self._connected = False
        self._reconnects = 0
        self._last_error = ""
        self._stopped = asyncio.Event()

    @property
    def target(self) -> str:
        return f"{self._host}:{self._port}"

    @property
    def connected(self) -> bool:
        return self._connected

    def status(self) -> dict[str, object]:
        # 只有目标地址与运行计数：用户名/口令永远不会出现在这里
        return {
            "broker": self.target,
            "topic_filter": self._filter,
            "qos": self._qos,
            "reconnects": self._reconnects,
            "last_error": self._last_error,
        }

    async def start(self, sink: MessageSink) -> None:
        if self._task is not None:
            return
        self._sink = sink
        self._stopped = asyncio.Event()
        self._task = asyncio.create_task(self._run(), name="aegis-mqtt-subscriber")

    async def stop(self) -> None:
        self._stopped.set()
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._connected = False

    def _open(self) -> Any:
        import aiomqtt

        return aiomqtt.Client(
            hostname=self._host,
            port=self._port,
            username=self._username,
            password=self._password,
            identifier=self._client_id,
            keepalive=self._keepalive,
            # 入站队列有界：无界队列在站端突发时会把平台内存吃光。设了上界，背压退回 broker
            # 侧（QoS1 的消息留在 broker 队列里，不会被静默丢弃）。
            max_queued_incoming_messages=self._queue_limit,
        )

    async def _run(self) -> None:
        attempt = 0
        while not self._stopped.is_set():
            try:
                client = self._open()
                async with client:
                    await client.subscribe(self._filter, qos=self._qos)
                    self._connected = True
                    attempt = 0
                    async for message in client.messages:
                        if self._sink is not None:
                            await self._sink(str(message.topic.value), bytes(message.payload))
            except asyncio.CancelledError:
                self._connected = False
                raise
            except Exception as exc:
                self._connected = False
                self._last_error = _describe_failure(exc)
                self._reconnects += 1
                attempt += 1
                delay = min(self._backoff_cap, self._backoff_base * (2 ** (attempt - 1)))
                log.warning(
                    "MQTT 订阅断开，退避后重连",
                    extra={"broker": self.target, "attempt": attempt, "delay_s": round(delay, 2), "error": self._last_error},
                )
                try:
                    await asyncio.wait_for(self._stopped.wait(), timeout=delay)
                except TimeoutError:
                    continue
        self._connected = False
