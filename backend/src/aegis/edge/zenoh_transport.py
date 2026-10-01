"""Eclipse Zenoh 站点↔网关传输（POC）：实现 BusTransport 语义 + 边缘有界缓冲重放 + store/query。

量测到的 API 事实（eclipse-zenoh 1.10.1）：Python 侧无 asyncio 集成——没有 zenoh.asyncio 模块，
open/declare_*/put/get 全为阻塞调用，回调由 zenoh 自有线程触发。因此本模块把所有 zenoh 调用
压到单一写线程执行，事件循环只 await Future；单写线程同时给出跨进程消息的全序
（asyncio.to_thread 走线程池，并发 put 会乱序——弱网重放场景不可接受）。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import queue
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from aegis.bus.subjects import is_valid_subject
from aegis.bus.transport import BusTransport, MessageHandler, RawCallback, Subscription
from aegis.domain.messages import AgentMessage
from aegis.edge.errors import EdgeQueryError, KeyExprMappingError, ZenohOperationError, ZenohUnavailableError
from aegis.edge.keyexpr import (
    decode_chunk,
    encode_chunk,
    is_pattern,
    pattern_to_keyexpr,
    subject_to_keyexpr,
)
from aegis.edge.offline_buffer import EdgeOfflineBuffer
from aegis.errors import DeadlineExceededError

log = logging.getLogger("aegis.edge.zenoh")

JSON_ENCODING = "application/json"
# zenoh 分块禁止 '#'、'?'、'$'；'?' 是 selector 参数分隔符，故只在拼选择子时临时引入。
SELECTOR_PARAM_CHARS = frozenset("?&=")
PRIORITY_TO_ZENOH: Mapping[int, str] = {
    1: "REAL_TIME",
    2: "INTERACTIVE_HIGH",
    3: "INTERACTIVE_LOW",
    4: "DATA_LOW",
    5: "DATA",
}
BUFFER_QUERY_SUBJECT = "edge.buffer.query"
DEFAULT_PRIORITY_NAME = "DATA"


def _import_zenoh() -> Any:
    try:
        import zenoh
    except ImportError as exc:  # pragma: no cover - 依赖缺失路径
        raise ZenohUnavailableError(
            "缺少 eclipse-zenoh 运行时：pip install eclipse-zenoh（PyPI 上的 zenoh 包已废弃）",
            detail={"reason": str(exc)},
        ) from exc
    return zenoh


def aegis_priority_to_zenoh_name(priority: int) -> str:
    """契约 priority 1(最高)~5(最低) 映射到 zenoh Priority 名；未知值回落 DATA。"""
    return PRIORITY_TO_ZENOH.get(int(priority), DEFAULT_PRIORITY_NAME)


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class ZenohLinkSettings:
    """链路参数：全部来自环境变量，POC 不接入平台配置面。"""

    mode: str = "peer"
    listen_endpoints: tuple[str, ...] = ()
    connect_endpoints: tuple[str, ...] = ()
    shared_memory: bool = False
    batch_size: int = 65_535
    multicast_scouting: bool = True
    multicast_loop: bool = True
    max_items: int = 10_000
    max_bytes: int = 32 * 1024 * 1024
    ttl_seconds: float = 30.0
    replay_interval_ms: int = 20
    replay_batch: int = 256
    link_poll_ms: int = 100

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> ZenohLinkSettings:
        source: Mapping[str, str] = os.environ if env is None else env

        def _tuple(key: str) -> tuple[str, ...]:
            raw = (source.get(key) or "").strip()
            return tuple(item.strip() for item in raw.split(",") if item.strip())

        def _int(key: str, default: int) -> int:
            return int(source.get(key) or default)

        return cls(
            mode=source.get("AEGIS_ZENOH_MODE") or "peer",
            listen_endpoints=_tuple("AEGIS_ZENOH_LISTEN"),
            connect_endpoints=_tuple("AEGIS_ZENOH_CONNECT"),
            shared_memory=_as_bool(source.get("AEGIS_ZENOH_SHM"), False),
            batch_size=_int("AEGIS_ZENOH_BATCH_SIZE", 65_535),
            multicast_scouting=_as_bool(source.get("AEGIS_ZENOH_SCOUT"), True),
            multicast_loop=_as_bool(source.get("AEGIS_ZENOH_MULTICAST_LOOP"), True),
            max_items=_int("AEGIS_ZENOH_MAX_ITEMS", 10_000),
            max_bytes=_int("AEGIS_ZENOH_MAX_BYTES", 32 * 1024 * 1024),
            ttl_seconds=float(source.get("AEGIS_ZENOH_TTL_S") or 30),
            replay_interval_ms=_int("AEGIS_ZENOH_REPLAY_MS", 20),
            replay_batch=_int("AEGIS_ZENOH_REPLAY_BATCH", 256),
            link_poll_ms=_int("AEGIS_ZENOH_LINK_POLL_MS", 100),
        )


class _SerialWorker:
    """把阻塞的 zenoh 调用串行到一个守护线程上，结果以线程安全方式回灌 asyncio Future。"""

    def __init__(self, name: str = "aegis-zenoh-writer") -> None:
        self._queue: queue.SimpleQueue[Any] = queue.SimpleQueue()
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            fn, args, kwargs, loop, future = item
            try:
                result = fn(*args, **kwargs)
            except BaseException as exc:
                # 异常原样回灌给 await 侧：调用方需要看到 ZError 原文来判断链路状态。
                if loop is None:
                    continue
                loop.call_soon_threadsafe(_set_exception, future, exc)
            else:
                if loop is None:
                    continue
                loop.call_soon_threadsafe(_set_result, future, result)

    async def call(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        self._queue.put((fn, args, kwargs, loop, future))
        return await future

    def stop(self) -> None:
        self._queue.put(None)
        self._thread.join(timeout=2.0)


def _set_result(future: asyncio.Future[Any], result: Any) -> None:
    if not future.done():
        future.set_result(result)


def _set_exception(future: asyncio.Future[Any], exc: BaseException) -> None:
    if not future.done():
        future.set_exception(exc)


def _json_default(obj: Any) -> Any:
    """ZBytes 等 C 扩展类型不是 int 子类，json 无法直接序列化：按字节兜底。"""
    as_bytes = getattr(obj, "to_bytes", None)
    if callable(as_bytes):
        return bytes(as_bytes()).decode("utf-8", "replace")
    return str(obj)


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, separators=(",", ":"), default=_json_default).encode()


def _decode_json(raw: bytes) -> Any:
    return json.loads(raw.decode())


def _peek_msg_id(payload: bytes) -> str | None:
    try:
        parsed = _decode_json(payload)
    except Exception:
        return None
    msg_id = parsed.get("msg_id") if isinstance(parsed, dict) else None
    return msg_id if isinstance(msg_id, str) else None


def _stringify(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, bool | int | float):
        return str(value)
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def selector_for(subject: str, params: Mapping[str, Any] | None = None) -> str:
    """构造带参数的选择子：subject 走可逆映射，参数值按实测非法字符集转义后百分位安全。"""
    keyexpr = pattern_to_keyexpr(subject)
    if not params:
        return keyexpr
    rendered = ";".join(f"{key}={encode_chunk(_stringify(value))}" for key, value in params.items())
    return f"{keyexpr}?{rendered}"


def selector_params(query: Any) -> dict[str, Any]:
    """queryable 侧解析选择子参数（与 selector_for 互逆）。"""
    out: dict[str, Any] = {}
    for key, value in list(query.parameters):
        text = str(value)
        try:
            out[str(key)] = decode_chunk(text)
        except KeyExprMappingError:
            out[str(key)] = text
    return out


class ZenohEdgeBus(BusTransport):
    """站点/网关两侧的 zenoh 会话：断链期本地有界存留，复链期按 seq 保序重放。"""

    name = "zenoh"

    def __init__(
        self,
        *,
        mode: str = "peer",
        listen_endpoints: Sequence[str] = (),
        connect_endpoints: Sequence[str] = (),
        buffer: EdgeOfflineBuffer | None = None,
        buffering: bool = True,
        replay_interval_s: float = 0.02,
        replay_batch: int = 256,
        link_poll_interval_s: float = 0.1,
        query_reply_timeout_s: float = 5.0,
        shared_memory: bool = False,
        batch_size: int = 65_535,
        multicast_scouting: bool = False,
        multicast_loop: bool = True,
        zid: str | None = None,
        session_factory: Callable[[Any], Any] | None = None,
    ) -> None:
        super().__init__()
        self._mode = mode
        self._listen = tuple(listen_endpoints)
        self._connect = tuple(connect_endpoints)
        self._buffer = buffer if buffer is not None else EdgeOfflineBuffer(default_ttl_seconds=None)
        self._buffering = buffering
        self._replay_interval = replay_interval_s
        self._replay_batch = replay_batch
        self._link_poll = link_poll_interval_s
        self._query_reply_timeout_s = query_reply_timeout_s
        self._shared_memory = shared_memory
        self._batch_size = batch_size
        self._multicast_scouting = multicast_scouting
        self._multicast_loop = multicast_loop
        self._zid = zid
        self._session_factory = session_factory
        self._zenoh: Any = None
        self._session: Any = None
        self._worker = _SerialWorker()
        self._publishers: dict[str, Any] = {}
        self._match_flags: dict[str, dict[str, bool | None]] = {}
        self._publisher_priorities: dict[str, str] = {}
        self._entities: list[Any] = []
        self._replay_task: asyncio.Task[None] | None = None
        self._link_task: asyncio.Task[None] | None = None
        self._closed = False
        self._sub_seq = 0
        self._warned_queue = False
        # 观测计数：报告与断言读这里，不在热路径打日志。
        self.sent_direct = 0
        self.sent_via_buffer = 0
        self.dropped_without_buffer = 0
        self.replay_batches = 0
        self.replay_messages = 0
        self.replay_failures = 0
        self.link_up_observations = 0
        self.link_down_observations = 0
        self.unsupported_queue_groups = 0
        self.durable_requests = 0
        self.callback_thread_names: set[str] = set()

    @property
    def buffer(self) -> EdgeOfflineBuffer:
        return self._buffer

    # ---------- 构造与生命周期 ----------

    @classmethod
    def from_env(cls, *, buffer: EdgeOfflineBuffer | None = None) -> ZenohEdgeBus:
        settings = ZenohLinkSettings.from_env()
        shared = (
            buffer
            if buffer is not None
            else EdgeOfflineBuffer(
                max_items=settings.max_items,
                max_bytes=settings.max_bytes,
                default_ttl_seconds=settings.ttl_seconds,
            )
        )
        return cls(
            mode=settings.mode,
            listen_endpoints=settings.listen_endpoints,
            connect_endpoints=settings.connect_endpoints,
            buffer=shared,
            replay_interval_s=settings.replay_interval_ms / 1000,
            replay_batch=settings.replay_batch,
            link_poll_interval_s=settings.link_poll_ms / 1000,
            shared_memory=settings.shared_memory,
            batch_size=settings.batch_size,
            multicast_scouting=settings.multicast_scouting,
            multicast_loop=settings.multicast_loop,
        )

    def build_config(self) -> Any:
        """组 zenoh Config；键名取自本版本 get_json 实测输出的配置树。"""
        zenoh = self._zenoh or _import_zenoh()
        config = zenoh.Config()
        config.insert_json5("mode", json.dumps(self._mode))
        if self._listen:
            config.insert_json5("listen/endpoints", json.dumps(list(self._listen)))
        if self._connect:
            config.insert_json5("connect/endpoints", json.dumps(list(self._connect)))
        if self._zid:
            config.insert_json5("id", json.dumps(self._zid))
        config.insert_json5("transport/shared_memory/enabled", "true" if self._shared_memory else "false")
        config.insert_json5("transport/link/tx/batch_size", str(self._batch_size))
        if self._multicast_scouting:
            config.insert_json5("scouting/multicast/enabled", "true")
            config.insert_json5("scouting/multicast/listen", "true")
            config.insert_json5("scouting/multicast/autoconnect", json.dumps(["peer", "router"]))
            if self._multicast_loop:
                # 键名与取值类型按 eclipse-zenoh 1.10.1 实测：
                # join_interval / join_messages 在这一版根本不存在（insert 直接 "unknown key"），
                # interface 只收字符串（写数组报 invalid type），故这里用 json.dumps(str)。
                config.insert_json5("scouting/multicast/interface", json.dumps("loopback/127.0.0.1"))
        else:
            config.insert_json5("scouting/multicast/autoconnect", "[]")
        return config

    async def connect(self) -> None:
        if self._connected:
            return
        self._zenoh = _import_zenoh()
        config = self.build_config()
        factory = self._session_factory or self._zenoh.open
        try:
            self._session = await self._worker.call(factory, config)
        except ZenohUnavailableError:
            raise
        except Exception as exc:
            raise ZenohUnavailableError(
                "zenoh 会话打开失败",
                detail={"listen": list(self._listen), "connect": list(self._connect), "reason": str(exc)},
            ) from exc
        self._connected = True
        self._closed = False
        self._link_task = asyncio.create_task(self._watch_link(), name="zenoh-link-watch")
        if self._buffering:
            self._replay_task = asyncio.create_task(self._replay_loop(), name="zenoh-replay")

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._connected = False
        tasks = [task for task in (self._replay_task, self._link_task) if task is not None]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._replay_task = None
        self._link_task = None
        session, self._session = self._session, None
        if session is not None:
            try:
                await self._worker.call(self._teardown, session)
            except Exception as exc:  # pragma: no cover - 对端已消失时的正常噪声
                log.debug("zenoh 实体回收异常", extra={"error": str(exc)})
        if self._buffer.in_flight:
            self._buffer.requeue()
        self._publishers.clear()
        self._match_flags.clear()
        self._worker.stop()

    def _teardown(self, session: Any) -> None:
        """写线程内回收：先 publisher/listener，再 subscriber/queryable，最后关会话。"""
        for publisher in self._publishers.values():
            # 对端先消失时 undeclare 必然报错：关停路径只尽力回收，不为此中断后续实体。
            with contextlib.suppress(Exception):
                publisher.undeclare()
        for entity in reversed(self._entities):
            with contextlib.suppress(Exception):
                entity.undeclare()
        self._entities.clear()
        if not session.is_closed():
            session.close()

    # ---------- 发布 ----------

    async def publish(self, subject: str, message: AgentMessage) -> None:
        """覆写只为记录该 subject 的 QoS 映射：优先级在 declare_publisher 时生效。"""
        self._publisher_priorities[subject] = aegis_priority_to_zenoh_name(message.priority)
        await super().publish(subject, message)

    def _publisher_for(self, subject: str) -> Any:
        """写线程内调用：publisher 声明实测 ~9ms，必须按 subject 缓存。"""
        cached = self._publishers.get(subject)
        if cached is not None:
            return cached
        zenoh = self._zenoh
        keyexpr = subject_to_keyexpr(subject)
        priority_name = self._publisher_priorities.get(subject, DEFAULT_PRIORITY_NAME)
        publisher = self._session.declare_publisher(
            keyexpr,
            encoding=JSON_ENCODING,
            congestion_control=zenoh.CongestionControl.BLOCK,
            priority=getattr(zenoh.Priority, priority_name),
        )
        flags: dict[str, bool | None] = {"matching": None}
        listener = publisher.declare_matching_listener(lambda status: flags.__setitem__("matching", bool(status.matching)))
        self._publishers[subject] = publisher
        self._match_flags[subject] = flags
        self._entities.append(listener)
        return publisher

    def set_publisher_priority(self, subject: str, priority: int) -> str:
        """声明前设置某 subject 的 zenoh 优先级名；已声明的需先 undeclare。"""
        name = aegis_priority_to_zenoh_name(priority)
        if subject in self._publishers:
            raise ZenohOperationError(
                "publisher 已声明，QoS 不可在重声明之外热改",
                detail={"subject": subject, "priority": priority},
            )
        self._publisher_priorities[subject] = name
        return name

    async def _publish_raw(self, subject: str, payload: bytes) -> None:
        self._require_ready()
        if not is_valid_subject(subject):
            raise KeyExprMappingError("发布 subject 不是合法的 aegis subject", detail={"subject": subject})
        outcome = await self._worker.call(self._send_or_buffer, subject, payload)
        if outcome == "sent":
            self.sent_direct += 1
        elif outcome == "buffered":
            self.sent_via_buffer += 1
        else:
            self.dropped_without_buffer += 1

    def _send_or_buffer(self, subject: str, payload: bytes) -> str:
        """写线程：matching 判定与 put 同线程完成，避免快照过期导致的漏存。"""
        if self._session is None or self._session.is_closed():
            if not self._buffering:
                return "dropped"
            self._buffer.put(subject, payload, msg_id=_peek_msg_id(payload))
            return "buffered"
        publisher = self._publisher_for(subject)
        cached = self._match_flags[subject]["matching"]
        matched = bool(publisher.matching_status.matching) if cached is None else cached
        self._match_flags[subject]["matching"] = matched
        if matched:
            publisher.put(payload)
            return "sent"
        if not self._buffering:
            return "dropped"
        self._buffer.put(subject, payload, msg_id=_peek_msg_id(payload))
        return "buffered"

    # ---------- 订阅 ----------

    async def _subscribe_raw(
        self,
        subject: str,
        callback: RawCallback,
        *,
        queue: str | None = None,
        durable: str | None = None,
    ) -> Subscription:
        self._require_ready()
        if queue:
            self.unsupported_queue_groups += 1
            if not self._warned_queue:
                self._warned_queue = True
                log.warning(
                    "zenoh 无队列组：同 key expression 的多个订阅者各收一份（扇出而非负载均衡）",
                    extra={"queue": queue, "subject": subject},
                )
        if durable:
            self.durable_requests += 1
        keyexpr = self.subscription_keyexpr(subject)
        loop = asyncio.get_running_loop()
        self._sub_seq += 1
        holder: dict[str, Any] = {"sub": None, "delivered": 0, "errors": 0}
        await self._worker.call(self._declare_subscriber, keyexpr, callback, loop, holder)
        subscription = Subscription(
            id=f"zenohs_{self._sub_seq:06d}",
            subject=subject,
            queue=queue,
            durable=durable,
        )

        async def _cancel() -> None:
            sub = holder.get("sub")
            if sub is None:
                return
            holder["sub"] = None
            try:
                await self._worker.call(sub.undeclare)
            except Exception as exc:
                log.debug("subscriber undeclare 失败", extra={"error": str(exc)})
            if sub in self._entities:
                self._entities.remove(sub)

        subscription._cancel = _cancel
        return subscription

    @staticmethod
    def subscription_keyexpr(subject: str) -> str:
        keyexpr = pattern_to_keyexpr(subject) if is_pattern(subject) else subject_to_keyexpr(subject)
        if set(keyexpr) & set(SELECTOR_PARAM_CHARS):
            raise KeyExprMappingError(
                "订阅 subject 含 selector 保留字符，无法表达为 zenoh 选择子",
                detail={"subject": subject, "keyexpr": keyexpr},
            )
        return keyexpr

    def _declare_subscriber(
        self,
        keyexpr: str,
        callback: RawCallback,
        loop: asyncio.AbstractEventLoop,
        holder: dict[str, Any],
    ) -> None:
        def _on_sample(sample: Any) -> None:
            self.callback_thread_names.add(threading.current_thread().name)
            try:
                raw = bytes(sample.payload)
            except Exception as exc:
                holder["errors"] += 1
                log.warning("样本解码失败", extra={"keyexpr": keyexpr, "error": str(exc)})
                return
            try:
                asyncio.run_coroutine_threadsafe(self._deliver(callback, raw, holder), loop)
            except RuntimeError:  # pragma: no cover - 关停竞态
                holder["errors"] += 1

        sub = self._session.declare_subscriber(keyexpr, _on_sample)
        holder["sub"] = sub
        self._entities.append(sub)

    @staticmethod
    async def _deliver(callback: RawCallback, raw: bytes, holder: dict[str, Any]) -> None:
        try:
            await callback(raw)
            holder["delivered"] += 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            holder["errors"] += 1
            log.warning("订阅回调异常", extra={"error": str(exc)})

    # ---------- 请求/响应（zenoh 原生 queryable） ----------

    async def serve(self, subject: str, handler: MessageHandler) -> Subscription:
        """把 subject 注册为 queryable：对端 query()/request_reply() 走原生请求-响应。"""
        self._require_ready()
        keyexpr = self.subscription_keyexpr(subject)
        loop = asyncio.get_running_loop()
        self._sub_seq += 1
        holder: dict[str, Any] = {"queryable": None, "errors": 0, "handled": 0}
        await self._worker.call(self._declare_queryable, keyexpr, handler, loop, holder)
        subscription = Subscription(id=f"zenohq_{self._sub_seq:06d}", subject=subject)

        async def _cancel() -> None:
            queryable = holder.get("queryable")
            if queryable is None:
                return
            holder["queryable"] = None
            await self._worker.call(queryable.undeclare)

        subscription._cancel = _cancel
        return subscription

    def _declare_queryable(
        self,
        keyexpr: str,
        handler: MessageHandler,
        loop: asyncio.AbstractEventLoop,
        holder: dict[str, Any],
    ) -> None:
        def _on_query(query: Any) -> None:
            self.callback_thread_names.add(threading.current_thread().name)
            try:
                future = asyncio.run_coroutine_threadsafe(self._answer(query, handler, keyexpr), loop)
                future.result(timeout=self._query_reply_timeout_s)
                holder["handled"] += 1
            except Exception as exc:
                holder["errors"] += 1
                log.warning("queryable 处理失败", extra={"keyexpr": keyexpr, "error": str(exc)})

        queryable = self._session.declare_queryable(keyexpr, _on_query, complete=True)
        holder["queryable"] = queryable
        self._entities.append(queryable)

    async def _answer(self, query: Any, handler: MessageHandler, keyexpr: str) -> None:
        raw = bytes(query.payload) if query.payload is not None else None
        if raw is None:
            await self._worker.call(query.reply_err, _json_bytes({"error": "缺少请求载荷"}))
            return
        try:
            message = AgentMessage.decode(raw)
        except Exception as exc:
            await self._worker.call(query.reply_err, _json_bytes({"error": f"契约解析失败: {exc}"}))
            return
        try:
            reply = await handler(message)
        except Exception as exc:
            await self._worker.call(query.reply_err, _json_bytes({"error": str(exc)}))
            return
        if reply is not None:
            try:
                key = self._reply_key(keyexpr, query)
            except KeyExprMappingError as exc:
                # 应答不下去也要让对端知道原因：静默不回会让对方的 query 一直等到超时，
                # 看上去像"链路慢"，实际是"这条查询本来就无法应答"。
                await self._worker.call(query.reply_err, _json_bytes({"error": exc.message, "detail": exc.detail}))
                return
            await self._worker.call(query.reply, key, reply.encode(), encoding=JSON_ENCODING)

    @staticmethod
    def _reply_key(queryable_keyexpr: str, query: Any) -> str:
        """应答必须落在具体 key 上：queryable 为通配时改用查询自带的具体 ke。"""
        if "*" not in queryable_keyexpr:
            return queryable_keyexpr
        queried = str(query.key_expr)
        if "*" not in queried:
            return queried
        raise KeyExprMappingError("无法为通配查询构造具体应答 ke", detail={"query": queried})

    async def request_reply(
        self,
        subject: str,
        message: AgentMessage,
        *,
        params: Mapping[str, Any] | None = None,
        timeout_ms: int = 2_000,
    ) -> AgentMessage:
        """原生请求-响应（首个合法应答即返回）：用于测 RTT，不走回执 subject 订阅。"""
        self._require_ready()
        selector = selector_for(subject, params)
        loop = asyncio.get_running_loop()
        future: asyncio.Future[AgentMessage] = loop.create_future()

        def _on_reply(reply: Any) -> None:
            sample = reply.ok
            if sample is None:
                return
            try:
                candidate = AgentMessage.decode(bytes(sample.payload))
            except Exception as exc:
                log.debug("应答解码失败", extra={"error": str(exc)})
                return
            if candidate.causation_id != message.msg_id:
                return
            loop.call_soon_threadsafe(_set_result, future, candidate)

        try:
            await self._worker.call(
                self._session.get,
                selector,
                _on_reply,
                payload=message.encode(),
                encoding=JSON_ENCODING,
                timeout=timeout_ms / 1000,
            )
        except Exception as exc:
            raise EdgeQueryError("query 下发失败", detail={"selector": selector, "reason": str(exc)}) from exc
        try:
            return await asyncio.wait_for(future, timeout=timeout_ms / 1000)
        except TimeoutError as exc:
            raise DeadlineExceededError(
                f"zenoh 原生请求超期: subject={subject}",
                detail={"msg_id": message.msg_id, "selector": selector, "timeout_ms": timeout_ms},
            ) from exc

    async def query(
        self,
        subject: str,
        message: AgentMessage | None = None,
        *,
        params: Mapping[str, Any] | None = None,
        timeout_ms: int = 500,
    ) -> list[AgentMessage]:
        """store/query 扇出：等满 timeout 收齐所有匹配 queryable 的应答。"""
        self._require_ready()
        selector = selector_for(subject, params)
        collected: list[AgentMessage] = []

        def _on_reply(reply: Any) -> None:
            sample = reply.ok
            if sample is None:
                return
            try:
                collected.append(AgentMessage.decode(bytes(sample.payload)))
            except Exception as exc:
                log.debug("query 应答解码失败", extra={"error": str(exc)})

        payload = message.encode() if message is not None else b"{}"
        try:
            await self._worker.call(
                self._session.get,
                selector,
                _on_reply,
                payload=payload,
                encoding=JSON_ENCODING,
                timeout=timeout_ms / 1000,
            )
        except Exception as exc:
            raise EdgeQueryError("query 下发失败", detail={"selector": selector, "reason": str(exc)}) from exc
        await asyncio.sleep(timeout_ms / 1000 + 0.05)
        return collected

    # ---------- 边缘 store/query ----------

    async def serve_edge_buffer(self, subject: str = BUFFER_QUERY_SUBJECT) -> Subscription:
        """把本地缓冲暴露为 queryable：网关可在重放发生之前就查到边缘存了什么。"""
        self._require_ready()
        keyexpr = self.subscription_keyexpr(subject)
        self._sub_seq += 1
        holder: dict[str, Any] = {"queryable": None, "queries": 0}
        await self._worker.call(self._declare_buffer_queryable, keyexpr, holder)
        subscription = Subscription(id=f"zenohb_{self._sub_seq:06d}", subject=subject)

        async def _cancel() -> None:
            queryable = holder.get("queryable")
            if queryable is None:
                return
            holder["queryable"] = None
            await self._worker.call(queryable.undeclare)

        subscription._cancel = _cancel
        return subscription

    def _declare_buffer_queryable(self, keyexpr: str, holder: dict[str, Any]) -> None:
        """一次查询一份完整应答：边缘缓冲的内容是一个集合，不是一个流。

        本版本 zenoh 的 queryable 对同一 ke 连发多条 reply 时，请求侧只收得到其中一条
        （本机实测：rows 有 1 条、两次 reply 都执行了，callback 只被触发一次）。
        逐条 reply 还会让"查到一半"看起来像"就这么多"，改成整份一次返回：
        要么拿到完整清单，要么什么都没有——弱网里后者的判断价值高得多。
        """

        def _on_query(query: Any) -> None:
            self.callback_thread_names.add(threading.current_thread().name)
            params = selector_params(query)
            pattern = str(params.get("subject") or "*")
            limit = params.get("limit")
            include_in_flight = _as_bool(params.get("include_in_flight"), True)
            rows = self._buffer.query(
                pattern,
                limit=None if limit in (None, "") else int(str(limit)),
                include_in_flight=include_in_flight,
            )
            holder["queries"] = int(holder.get("queries", 0)) + 1
            answer = {
                "station": self._buffer.station_id,
                "pattern": pattern,
                "count": len(rows),
                "items": [
                    {
                        "seq": row.seq,
                        "subject": row.subject,
                        "msg_id": row.msg_id,
                        "enqueued_at": row.enqueued_at,
                        "size": len(row.payload),
                        "payload": _maybe_json(bytes(row.payload)),
                    }
                    for row in rows
                ],
            }
            query.reply(keyexpr, _json_bytes(answer), encoding=JSON_ENCODING)
            query.drop()

        queryable = self._session.declare_queryable(keyexpr, _on_query, complete=True)
        holder["queryable"] = queryable
        self._entities.append(queryable)

    async def query_edge_buffer(
        self,
        pattern: str,
        *,
        limit: int | None = None,
        timeout_ms: int = 200,
    ) -> list[dict[str, Any]]:
        """网关侧：查对端边缘缓冲区内容（只读，不消费、不触发重放）。"""
        self._require_ready()
        params: dict[str, Any] = {"subject": pattern}
        if limit is not None:
            params["limit"] = limit
        selector = selector_for(BUFFER_QUERY_SUBJECT, params)
        found: list[dict[str, Any]] = []

        def _on_reply(reply: Any) -> None:
            sample = reply.ok
            if sample is None:
                return
            try:
                record = _decode_json(bytes(sample.payload))
            except Exception:
                return
            if isinstance(record, dict) and isinstance(record.get("items"), list):
                found.extend(item for item in record["items"] if isinstance(item, dict))

        await self._worker.call(
            self._session.get,
            selector,
            _on_reply,
            payload=b"{}",
            encoding=JSON_ENCODING,
            timeout=timeout_ms / 1000,
        )
        await asyncio.sleep(timeout_ms / 1000 + 0.05)
        return found

    # ---------- 链路监视与重放 ----------

    async def _watch_link(self) -> None:
        while not self._closed:
            try:
                up = await self._worker.call(self._probe_link)
            except Exception:
                up = False
            if up:
                self.link_up_observations += 1
                self._buffer.note_link_up()
            else:
                self.link_down_observations += 1
                self._buffer.note_link_down()
            await asyncio.sleep(self._link_poll)

    def _probe_link(self) -> bool:
        """写线程：'有对端 peer' 视为链路可用；无 peer 时只要有 matched publisher 也算通。"""
        session = self._session
        if session is None or session.is_closed():
            return False
        if list(session.info.peers_zid()):
            return True
        return any(flag is True for flags in self._match_flags.values() for flag in [flags["matching"]])

    async def _replay_loop(self) -> None:
        while not self._closed:
            await asyncio.sleep(self._replay_interval)
            if not self._buffer.replay_due:
                continue
            try:
                sent = await self._worker.call(self._replay_once)
            except Exception as exc:
                self.replay_failures += 1
                log.debug("重放批次失败", extra={"error": str(exc)})
                continue
            if sent:
                self.replay_batches += 1
                self.replay_messages += sent

    def _replay_once(self) -> int:
        batch = self._buffer.drain(self._replay_batch)
        if not batch:
            return 0
        try:
            for sample in batch:
                self._publisher_for(sample.subject).put(sample.payload)
        except Exception:
            self._buffer.requeue()
            raise
        self._buffer.ack(batch)
        return len(batch)

    # ---------- 观测 ----------

    def snapshot(self) -> dict[str, object]:
        return {
            "name": self.name,
            "connected": self.connected,
            "mode": self._mode,
            "listen": list(self._listen),
            "connect": list(self._connect),
            "buffering": self._buffering,
            "publishers": len(self._publishers),
            "entities": len(self._entities),
            "sent_direct": self.sent_direct,
            "sent_via_buffer": self.sent_via_buffer,
            "dropped_without_buffer": self.dropped_without_buffer,
            "replay_batches": self.replay_batches,
            "replay_messages": self.replay_messages,
            "replay_failures": self.replay_failures,
            "link_up_observations": self.link_up_observations,
            "link_down_observations": self.link_down_observations,
            "unsupported_queue_groups": self.unsupported_queue_groups,
            "durable_requests": self.durable_requests,
            "callback_threads": sorted(self.callback_thread_names),
            "buffer": self._buffer.snapshot(),
        }


def _maybe_json(raw: bytes) -> Any:
    try:
        return _decode_json(raw)
    except Exception:
        return raw.decode("utf-8", "replace")


__all__ = [
    "BUFFER_QUERY_SUBJECT",
    "ZenohEdgeBus",
    "ZenohLinkSettings",
    "aegis_priority_to_zenoh_name",
    "selector_for",
    "selector_params",
]
