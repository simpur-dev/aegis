"""Zenoh 传输适配器的行为测试：用假 session 驱动，把"只有实现体才知道"的语义钉住。

本文件盯的是这条链路最容易悄悄坏掉的四类事实，而不是"调用没报错"：
1. **断链存留与复链重放**：没匹配到订阅者时必须进边缘缓冲，复链后按 seq 保序补发，
   一条都不能丢、也不能乱序（弱网重放的全部意义）；
2. **单一写线程的全序**：zenoh Python 侧无 asyncio 集成，所有阻塞调用压到一条写线程，
   并发 publish 的落线顺序必须等于提交顺序；
3. **store/query**：边缘缓冲可被网关在重放发生之前查到，且查询是只读的（不消费、不触发重放）；
4. **降级可见**：zenoh 没有队列组，`queue=` 必须被计数并告警一次，而不是静默当成负载均衡。

真 zenoh 运行时的线级互通由 tests/integration/test_edge_zenoh_live.py 取证。
"""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from typing import Any

import pytest

from aegis.domain.messages import AgentMessage, make_event, make_request, new_trace_id
from aegis.edge.errors import EdgeQueryError, KeyExprMappingError, ZenohOperationError
from aegis.edge.keyexpr import subject_to_keyexpr
from aegis.edge.zenoh_transport import (
    BUFFER_QUERY_SUBJECT,
    ZenohEdgeBus,
    ZenohLinkSettings,
    aegis_priority_to_zenoh_name,
    selector_for,
    selector_params,
)
from aegis.errors import BusNotReadyError, DeadlineExceededError

SUBJECT = "station.telemetry"
KEYEXPR = subject_to_keyexpr(SUBJECT)


class _FakePublisher:
    def __init__(self, keyexpr: str, *, matching: bool, encoding: str | None, priority: Any) -> None:
        self.keyexpr = keyexpr
        self.encoding = encoding
        self.priority = priority
        self.payloads: list[bytes] = []
        self.matching_status = SimpleNamespace(matching=matching)
        self.undeclared = False

    def put(self, payload: bytes) -> None:
        self.payloads.append(bytes(payload))

    def declare_matching_listener(self, callback):
        return SimpleNamespace(undeclare=lambda: None, callback=callback)

    def undeclare(self) -> None:
        self.undeclared = True


class _FailingPublisher:
    """重放中途写失败的 publisher：put 抛错，用来验证批次退回而不是被 ack。"""

    def __init__(self, keyexpr: str) -> None:
        self.keyexpr = keyexpr
        self.matching_status = SimpleNamespace(matching=True)

    def put(self, _payload: bytes) -> None:
        raise RuntimeError("put failed")


class _FakeEntity:
    """subscriber / queryable 的公共外壳：只记录 keyexpr 与回调，支持手工投喂。"""

    def __init__(self, keyexpr: str, callback) -> None:
        self.keyexpr = keyexpr
        self.callback = callback
        self.undeclared = False

    def undeclare(self) -> None:
        self.undeclared = True


class _FakeSession:
    def __init__(self, *, matching: bool = False, peers: tuple[str, ...] = ()) -> None:
        self.matching = matching
        self.peers = list(peers)
        self.publishers: dict[str, _FakePublisher] = {}
        self.subscribers: list[_FakeEntity] = []
        self.queryables: list[_FakeEntity] = []
        self.gets: list[tuple[str, Any, dict[str, Any]]] = []
        self.closed = False
        self.info = SimpleNamespace(peers_zid=lambda: list(self.peers))

    def is_closed(self) -> bool:
        return self.closed

    def close(self) -> None:
        self.closed = True

    def declare_publisher(
        self, keyexpr: str, *, encoding: str | None = None, congestion_control: Any = None, priority: Any = None
    ) -> _FakePublisher:
        publisher = _FakePublisher(keyexpr, matching=self.matching, encoding=encoding, priority=priority)
        self.publishers[keyexpr] = publisher
        return publisher

    def declare_subscriber(self, keyexpr: str, callback) -> _FakeEntity:
        entity = _FakeEntity(keyexpr, callback)
        self.subscribers.append(entity)
        return entity

    def declare_queryable(self, keyexpr: str, callback, *, complete: bool = False) -> _FakeEntity:
        entity = _FakeEntity(keyexpr, callback)
        entity.complete = complete
        self.queryables.append(entity)
        return entity

    def get(self, selector: str, callback, **kwargs: Any) -> None:
        self.gets.append((selector, callback, kwargs))


def event(*, payload: dict[str, Any] | None = None, priority: int = 3, subject: str = SUBJECT) -> AgentMessage:
    return make_event(
        source="station.node-1", action="telemetry.report", payload=payload or {"v": 1}, trace_id=new_trace_id(), priority=priority
    )


def sample(payload: Any) -> SimpleNamespace:
    """zenoh 样本形状：订阅回调与 reply.ok 都只取 payload。"""
    raw = payload if isinstance(payload, bytes) else json.dumps(payload, separators=(",", ":")).encode()
    return SimpleNamespace(payload=raw)


def reply_of(payload: Any) -> SimpleNamespace:
    return SimpleNamespace(ok=sample(payload))


def error_reply() -> SimpleNamespace:
    return SimpleNamespace(ok=None)


async def _connected(session: _FakeSession, **overrides: Any) -> ZenohEdgeBus:
    bus = ZenohEdgeBus(session_factory=lambda _config: session, **overrides)
    await bus.connect()
    return bus


@pytest.fixture
async def bus_with_session():
    session = _FakeSession()
    bus = await _connected(session, link_poll_interval_s=0.02, replay_interval_s=0.02)
    try:
        yield bus, session
    finally:
        await bus.close()


class TestLifecycle:
    async def test_publish_before_connect_is_refused_not_queued(self) -> None:
        bus = ZenohEdgeBus(session_factory=lambda _config: _FakeSession())
        with pytest.raises(BusNotReadyError):
            await bus.publish(SUBJECT, event())
        await bus.close()

    async def test_close_is_idempotent_and_stops_the_writer(self) -> None:
        session = _FakeSession()
        bus = await _connected(session)
        worker = bus._worker

        await bus.close()
        await bus.close()  # 第二次必须是空操作：重复取消已回收的任务会抛

        assert bus.connected is False
        assert session.closed is True
        assert worker._thread.is_alive() is False

    async def test_open_failure_is_reported_as_unavailable_link(self) -> None:
        from aegis.edge.errors import ZenohUnavailableError

        def boom(_config: Any) -> Any:
            raise RuntimeError("router down")

        bus = ZenohEdgeBus(session_factory=boom)
        with pytest.raises(ZenohUnavailableError) as caught:
            await bus.connect()

        assert "router down" in str(caught.value.detail["reason"]), "原始错误要能追到，否则运维只看到一句打不开"
        await bus.close()

    async def test_entities_are_undeclared_close_first_then_session(self) -> None:
        session = _FakeSession()
        bus = await _connected(session)
        await bus.publish(SUBJECT, event())
        subscription = await bus._subscribe_raw(SUBJECT, lambda _raw: None)
        await subscription._cancel()

        assert session.subscribers[0].undeclared is True
        assert bus.snapshot()["entities"] == 1  # 只剩 publisher 自己的 matching listener
        await bus.close()


class TestPublishPath:
    async def test_matched_publisher_sends_directly(self, bus_with_session) -> None:
        bus, session = bus_with_session
        session.matching = True
        message = event()

        await bus.publish(SUBJECT, message)

        assert bus.sent_direct == 1 and bus.sent_via_buffer == 0
        assert session.publishers[KEYEXPR].payloads == [message.encode()]
        assert session.publishers[KEYEXPR].encoding == "application/json"

    async def test_unmatched_subject_falls_into_the_edge_buffer(self, bus_with_session) -> None:
        bus, session = bus_with_session

        await bus.publish(SUBJECT, event())

        assert bus.sent_via_buffer == 1 and bus.sent_direct == 0
        assert session.publishers[KEYEXPR].payloads == []
        assert bus.buffer.pending == 1

    async def test_buffering_disabled_drops_instead_of_storing(self) -> None:
        session = _FakeSession()
        bus = await _connected(session, buffering=False)

        await bus.publish(SUBJECT, event())

        assert bus.dropped_without_buffer == 1
        assert bus.buffer.pending == 0
        await bus.close()

    async def test_closed_session_buffers_rather_than_losing_the_message(self, bus_with_session) -> None:
        bus, session = bus_with_session
        session.closed = True

        await bus.publish(SUBJECT, event())

        assert bus.sent_via_buffer == 1
        assert bus.buffer.pending == 1

    async def test_invalid_subject_is_rejected_before_touching_the_link(self, bus_with_session) -> None:
        bus, _session = bus_with_session

        with pytest.raises(KeyExprMappingError):
            await bus._publish_raw("not a subject", b"{}")

    async def test_replay_after_link_up_preserves_submission_order(self) -> None:
        """断链期存了三条，复链后必须按 seq 顺序一次性补发——乱序的重放在弱网里等于改写了事件时序。"""
        session = _FakeSession(matching=False)
        bus = await _connected(session, link_poll_interval_s=0.01, replay_interval_s=0.01, replay_batch=2)
        payloads = [{"seq": i} for i in range(3)]
        for payload in payloads:
            await bus.publish(SUBJECT, event(payload=payload))
        assert bus.buffer.pending == 3

        session.matching = True
        session.peers = ["peer-gateway"]
        await asyncio.wait_for(_wait_for(lambda: bus.replay_messages >= 3), timeout=3.0)

        sent = [json.loads(raw) for raw in session.publishers[KEYEXPR].payloads]
        assert [row["payload"]["seq"] for row in sent] == [0, 1, 2]
        assert bus.replay_batches >= 2, "replay_batch=2 时三条消息至少分两批"
        assert bus.buffer.pending == 0
        await bus.close()

    async def test_replay_success_acks_the_batch(self) -> None:
        """手动重放一版：成功批次必须被 ack（从 pending 移到已发），否则会无限重复补发。"""
        bus = await _connected(_FakeSession(matching=True), link_poll_interval_s=60.0, replay_interval_s=60.0)
        await bus.publish(SUBJECT, event())
        assert bus.sent_direct == 1

        bus.buffer.note_link_up()
        bus.buffer.put(SUBJECT, event().encode(), msg_id="m-1")

        assert await bus._worker.call(bus._replay_once) == 1
        assert bus.buffer.pending == 0
        await bus.close()

    async def test_replay_failure_requeues_without_acking(self) -> None:
        """重放中途写失败：这批必须退回待重放，绝不能已经 ack —— 否则弱网抖动就是静默丢数。

        后台重放/链路巡检的节拍被调慢到测试不会自己触发，才能把一次失败单独看清楚。
        """
        bus = await _connected(_FakeSession(matching=True), link_poll_interval_s=60.0, replay_interval_s=60.0)
        bus._publishers[SUBJECT] = _FailingPublisher(KEYEXPR)
        bus.buffer.note_link_up()
        bus.buffer.put(SUBJECT, event().encode(), msg_id="m-2")

        with pytest.raises(RuntimeError, match="put failed"):
            await bus._worker.call(bus._replay_once)
        assert bus.buffer.pending == 1
        await bus.close()


class TestSerialWriter:
    async def test_concurrent_publishes_reach_the_wire_in_submission_order(self, bus_with_session) -> None:
        """模块头的核心承诺：单写线程给出全序。线程池会把并发 put 打乱，这里必须看得见顺序。"""
        bus, session = bus_with_session
        session.matching = True
        messages = [event(payload={"i": i}) for i in range(40)]

        await asyncio.gather(*(bus.publish(SUBJECT, message) for message in messages))

        on_wire = [json.loads(raw)["payload"]["i"] for raw in session.publishers[KEYEXPR].payloads]
        assert len(on_wire) == 40
        assert sorted(on_wire) == list(range(40)), "有消息没上线"
        assert on_wire == sorted(on_wire), "写线程乱序：重放时序不再可信"

    async def test_writer_propagates_the_original_exception_to_the_awaiting_side(self, bus_with_session) -> None:
        bus, _session = bus_with_session

        def boom() -> None:
            raise ValueError("zenoh 原文错误")

        with pytest.raises(ValueError, match="zenoh 原文错误"):
            await bus._worker.call(boom)


class TestSubscribe:
    async def test_callback_runs_on_the_event_loop_and_counts_deliveries(self, bus_with_session) -> None:
        bus, session = bus_with_session
        received: list[bytes] = []

        async def _sink(raw: bytes) -> None:
            received.append(raw)

        subscription = await bus._subscribe_raw(SUBJECT, _sink)
        holder = session.subscribers[0]

        holder.callback(sample({"msg_id": "m-1"}))
        await _wait_for(lambda: bool(received))

        assert received == [json.dumps({"msg_id": "m-1"}, separators=(",", ":")).encode()]
        assert subscription.id.startswith("zenohs_")
        await subscription._cancel()
        assert holder.undeclared is True

    async def test_handler_exception_is_counted_not_propagated(self, bus_with_session) -> None:
        bus, session = bus_with_session

        async def failing(_raw: bytes) -> None:
            raise RuntimeError("订阅方炸了")

        subscription = await bus._subscribe_raw(SUBJECT, failing)
        session.subscribers[0].callback(sample({}))
        await asyncio.sleep(0.05)

        assert bus.snapshot()["entities"] >= 1
        assert subscription is not None
        await subscription._cancel()

    async def test_undecodable_sample_never_reaches_the_callback(self, bus_with_session) -> None:
        bus, session = bus_with_session
        seen: list[bytes] = []

        async def _sink(raw: bytes) -> None:
            seen.append(raw)

        subscription = await bus._subscribe_raw(SUBJECT, _sink)

        class _BrokenPayload:
            def __bytes__(self) -> bytes:  # pragma: no cover - 由 zenoh 侧抛出
                raise TypeError("payload 不可读")

        session.subscribers[0].callback(SimpleNamespace(payload=_BrokenPayload()))
        await asyncio.sleep(0.05)

        assert seen == []
        await subscription._cancel()

    async def test_queue_groups_are_counted_and_warned_once(self, bus_with_session) -> None:
        """zenoh 只有扇出：写 queue= 的人以为拿到了负载均衡，必须让它看得见。"""
        bus, _session = bus_with_session

        first = await bus._subscribe_raw(SUBJECT, lambda _raw: None, queue="workers")
        second = await bus._subscribe_raw(SUBJECT, lambda _raw: None, queue="workers", durable="d1")
        snap = bus.snapshot()

        assert snap["unsupported_queue_groups"] == 2
        assert snap["durable_requests"] == 1
        assert first.queue == "workers" and second.durable == "d1"

    def test_subscription_keyexpr_rejects_selector_reserved_characters(self) -> None:
        with pytest.raises(KeyExprMappingError):
            ZenohEdgeBus.subscription_keyexpr("station.telemetry?x=1")

    def test_pattern_subject_becomes_a_zenoh_wildcard(self) -> None:
        keyexpr = ZenohEdgeBus.subscription_keyexpr("station.*.telemetry")

        assert "*" in keyexpr


class TestRequestReply:
    def _request(self) -> AgentMessage:
        return make_request(
            source="platform.gateway",
            target="station.node01",
            action="net.ping",
            payload={},
            trace_id=new_trace_id(),
            reply_to="station.node01",
        )

    async def test_reply_is_matched_by_causation_id(self, bus_with_session) -> None:
        bus, session = bus_with_session
        request = self._request()

        task = asyncio.create_task(bus.request_reply(SUBJECT, request, timeout_ms=400))
        await _wait_for(lambda: bool(session.gets))
        selector, on_reply, _kwargs = session.gets[0]

        on_reply(error_reply())  # 错误应答不能被当成成功
        on_reply(reply_of(_reply_bytes(request, {"pong": "other"}, causation_id="不匹配的 id")))
        await asyncio.sleep(0)
        assert task.done() is False, "causation_id 对不上的应答必须被丢弃"

        on_reply(reply_of(_reply_bytes(request, {"pong": True})))
        got = await task

        assert got.payload["pong"] is True
        assert KEYEXPR in selector

    async def test_request_timeout_becomes_deadline_exceeded(self, bus_with_session) -> None:
        bus, _session = bus_with_session

        with pytest.raises(DeadlineExceededError):
            await bus.request_reply(SUBJECT, self._request(), timeout_ms=60)

    async def test_query_failure_at_dispatch_is_an_edge_query_error(self) -> None:
        session = _FakeSession()

        def broken_get(*_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("no route")

        session.get = broken_get  # type: ignore[method-assign]
        bus = await _connected(session)

        with pytest.raises(EdgeQueryError):
            await bus.query(SUBJECT, event(), timeout_ms=50)
        await bus.close()

    async def test_query_collects_every_reply_within_the_window(self, bus_with_session) -> None:
        bus, session = bus_with_session
        message = event()

        task = asyncio.create_task(bus.query(SUBJECT, message, timeout_ms=400))
        await _wait_for(lambda: bool(session.gets))
        _selector, on_reply, _kwargs = session.gets[0]
        for index in range(3):
            on_reply(reply_of(_reply_bytes(message, {"i": index})))
        on_reply(error_reply())

        rows = await task

        assert [row.payload["i"] for row in rows] == [0, 1, 2]


class TestServe:
    async def test_handler_reply_lands_on_a_concrete_keyexpr(self, bus_with_session) -> None:
        bus, session = bus_with_session
        query = _FakeQuery(key_expr=KEYEXPR, payload=event().encode())

        subscription = await bus.serve(SUBJECT, _echo_handler)
        session.queryables[0].callback(query)
        await _wait_for(lambda: bool(query.replies))

        assert query.replies and query.replies[0][0] == KEYEXPR
        assert json.loads(query.replies[0][1])["payload"]["handled"] is True
        assert query.replies[0][2]["encoding"] == "application/json"
        await subscription._cancel()

    async def test_wildcard_queryable_answers_on_the_query_key(self, bus_with_session) -> None:
        bus, session = bus_with_session
        queried = "station/rg01/telemetry"
        message = event()
        query = _FakeQuery(key_expr=queried, payload=message.encode())

        subscription = await bus.serve("station.*.telemetry", _echo_handler)
        session.queryables[0].callback(query)
        await _wait_for(lambda: bool(query.replies))

        assert query.replies[0][0] == queried, "通配 queryable 的应答必须落到查询带来的具体 ke，回在通配上没人收"
        await subscription._cancel()

    async def test_wildcard_query_gets_an_error_reply_not_silence(self, bus_with_session) -> None:
        """查询本身就是通配时无法构造应答 ke：必须回错误，让对端立刻知道，而不是让它等到超时。"""
        bus, session = bus_with_session

        subscription = await bus.serve("station.*.telemetry", _echo_handler)
        query = _FakeQuery(key_expr="station/*/telemetry", payload=event().encode())
        session.queryables[0].callback(query)
        await _wait_for(lambda: bool(query.errors))

        assert query.replies == []
        assert json.loads(query.errors[0])["error"], "无法应答的原因要写进错误应答"
        await subscription._cancel()

    async def test_missing_payload_and_bad_contract_become_error_replies(self, bus_with_session) -> None:
        bus, session = bus_with_session

        subscription = await bus.serve(SUBJECT, _echo_handler)
        empty = _FakeQuery(key_expr=KEYEXPR, payload=None)
        broken = _FakeQuery(key_expr=KEYEXPR, payload=b"{not json")
        session.queryables[0].callback(empty)
        session.queryables[0].callback(broken)
        await _wait_for(lambda: bool(empty.errors) and bool(broken.errors))

        assert "缺少请求载荷" in json.loads(empty.errors[0])["error"]
        assert "契约解析失败" in json.loads(broken.errors[0])["error"]
        await subscription._cancel()

    async def test_handler_exception_is_returned_as_error_payload(self, bus_with_session) -> None:
        bus, session = bus_with_session

        async def raising(_message: AgentMessage) -> AgentMessage:
            raise RuntimeError("研判失败")

        subscription = await bus.serve(SUBJECT, raising)
        query = _FakeQuery(key_expr=KEYEXPR, payload=event().encode())
        session.queryables[0].callback(query)
        await _wait_for(lambda: bool(query.errors))

        assert json.loads(query.errors[0])["error"] == "研判失败"
        await subscription._cancel()


class TestEdgeBufferQueryable:
    async def test_gateway_can_read_the_buffer_without_consuming_it(self, bus_with_session) -> None:
        bus, session = bus_with_session
        message = event(payload={"v": 7})
        await bus.publish(SUBJECT, message)

        subscription = await bus.serve_edge_buffer()
        query = _FakeQuery(key_expr=subject_to_keyexpr(BUFFER_QUERY_SUBJECT), params=[("subject", SUBJECT), ("limit", "10")])
        session.queryables[0].callback(query)
        await _wait_for(lambda: bool(query.replies))

        assert len(query.replies) == 1, "一次查询一份完整应答：逐条 reply 在本版本会被吞掉"
        answer = json.loads(query.replies[0][1])
        assert answer["count"] == 1 and answer["pattern"] == SUBJECT
        assert answer["items"][0]["payload"]["payload"]["v"] == 7
        assert bus.buffer.pending == 1, "查询必须只读：查一次就把边缘缓冲清空是灾难"
        await subscription._cancel()

    def test_selector_round_trip_keeps_a_pattern_subject_intact(self) -> None:
        selector = selector_for(BUFFER_QUERY_SUBJECT, {"subject": "station.*.telemetry", "limit": 5})
        _ke, _, param_part = selector.partition("?")
        pairs = [(chunk.partition("=")[0], chunk.partition("=")[2]) for chunk in param_part.split(";")]

        parsed = selector_params(SimpleNamespace(parameters=pairs))

        assert parsed == {"subject": "station.*.telemetry", "limit": "5"}
        assert "?" not in _ke and "$" not in _ke, "ke 部分不能带 selector 保留字符"

    async def test_query_edge_buffer_unwraps_the_items_array(self, bus_with_session) -> None:
        bus, session = bus_with_session
        await bus.publish(SUBJECT, event())

        task = asyncio.create_task(bus.query_edge_buffer(SUBJECT, limit=5, timeout_ms=400))
        await _wait_for(lambda: bool(session.gets))
        _selector, on_reply, _kwargs = session.gets[0]
        on_reply(reply_of({"station": "edge01", "pattern": SUBJECT, "count": 1, "items": [{"seq": 1, "subject": SUBJECT, "size": 12}]}))
        on_reply(error_reply())

        rows = await task

        assert rows == [{"seq": 1, "subject": SUBJECT, "size": 12}]
        assert "limit=5" in session.gets[0][0]


class TestQosAndSettings:
    @pytest.mark.parametrize(
        ("priority", "expected"),
        [(1, "REAL_TIME"), (2, "INTERACTIVE_HIGH"), (3, "INTERACTIVE_LOW"), (4, "DATA_LOW"), (5, "DATA"), (9, "DATA"), (0, "DATA")],
    )
    def test_priority_mapping_covers_the_contract_and_falls_back(self, priority: int, expected: str) -> None:
        assert aegis_priority_to_zenoh_name(priority) == expected

    async def test_declared_priority_is_applied_at_publisher_declaration(self, bus_with_session) -> None:
        bus, session = bus_with_session
        session.matching = True

        await bus.publish(SUBJECT, event(priority=1))

        assert session.publishers[KEYEXPR].priority == bus._zenoh.Priority.REAL_TIME

    async def test_priority_cannot_be_hot_changed_after_declaration(self, bus_with_session) -> None:
        bus, session = bus_with_session
        session.matching = True
        await bus.publish(SUBJECT, event())

        with pytest.raises(ZenohOperationError):
            bus.set_publisher_priority(SUBJECT, 5)

        assert bus.set_publisher_priority("station.other", 1) == "REAL_TIME"

    def test_settings_from_env_parses_lists_booleans_and_numbers(self) -> None:
        settings = ZenohLinkSettings.from_env(
            {
                "AEGIS_ZENOH_MODE": "router",
                "AEGIS_ZENOH_LISTEN": "tcp/0.0.0.0:7447, ",
                "AEGIS_ZENOH_CONNECT": "tcp/10.0.0.5:7447",
                "AEGIS_ZENOH_SHM": "on",
                "AEGIS_ZENOH_BATCH_SIZE": "1024",
                "AEGIS_ZENOH_SCOUT": "false",
                "AEGIS_ZENOH_MAX_ITEMS": "5",
                "AEGIS_ZENOH_TTL_S": "2.5",
                "AEGIS_ZENOH_REPLAY_MS": "40",
            }
        )

        assert settings.mode == "router"
        assert settings.listen_endpoints == ("tcp/0.0.0.0:7447",)
        assert settings.connect_endpoints == ("tcp/10.0.0.5:7447",)
        assert settings.shared_memory is True and settings.multicast_scouting is False
        assert settings.batch_size == 1024 and settings.max_items == 5
        assert settings.ttl_seconds == 2.5 and settings.replay_interval_ms == 40

    def test_from_env_builds_a_bus_sharing_the_settings_sized_buffer(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AEGIS_ZENOH_MODE", "router")
        monkeypatch.setenv("AEGIS_ZENOH_MAX_ITEMS", "3")
        monkeypatch.setenv("AEGIS_ZENOH_TTL_S", "7")
        monkeypatch.setenv("AEGIS_ZENOH_SCOUT", "false")

        bus = ZenohEdgeBus.from_env()

        assert bus.buffer.snapshot()["max_items"] == 3
        assert bus.buffer.snapshot()["default_ttl_seconds"] == 7
        assert bus._mode == "router"
        assert bus._buffering is True
        asyncio.run(bus.close())

    def test_build_config_is_accepted_by_the_real_zenoh_runtime(self) -> None:
        """键名与取值类型只有真运行时认账：上一版写了两个本版本不存在的键，
        于是 from_env()（默认开组播）一 connect 就抛 unknown key。"""
        pytest.importorskip("zenoh")
        session = _FakeSession()
        bus = ZenohEdgeBus(
            session_factory=lambda _config: session,
            mode="peer",
            listen_endpoints=("tcp/0.0.0.0:7447",),
            multicast_scouting=True,
            multicast_loop=True,
        )

        config = bus.build_config()

        assert json.loads(config.get_json("mode")) == "peer"
        assert "tcp/0.0.0.0:7447" in config.get_json("listen")
        assert "loopback/127.0.0.1" in config.get_json("scouting/multicast")

        off = ZenohEdgeBus(session_factory=lambda _config: session, multicast_scouting=False).build_config()
        assert json.loads(off.get_json("scouting/multicast"))["autoconnect"] == []
        asyncio.run(bus.close())


class _FakeQuery:
    """zenoh Query 的替身：只实现本模块用到的四个成员。"""

    def __init__(self, *, key_expr: str, payload: bytes | None = None, params: list[tuple[str, Any]] | None = None) -> None:
        self.key_expr = key_expr
        self.payload = payload
        self.parameters = params or []
        self.replies: list[tuple[str, bytes, dict[str, Any]]] = []
        self.errors: list[bytes] = []
        self.dropped = False

    def reply(self, key_expr: str, payload: bytes, **kwargs: Any) -> None:
        self.replies.append((key_expr, bytes(payload), kwargs))

    def reply_err(self, payload: bytes) -> None:
        self.errors.append(bytes(payload))

    def drop(self) -> None:
        self.dropped = True


def _reply_bytes(request: AgentMessage, payload: dict[str, Any], *, causation_id: str | None = None) -> bytes:
    reply = make_event(source="station.node01", action="telemetry.reply", payload=payload, trace_id=request.trace_id)
    return reply.model_copy(update={"causation_id": causation_id or request.msg_id}).encode()


async def _echo_handler(message: AgentMessage) -> AgentMessage:
    return message.model_copy(update={"payload": {"handled": True, "echo": message.payload}, "causation_id": message.msg_id})


async def _wait_for(predicate, *, timeout: float = 2.0) -> None:
    """等到条件成立；到点不成立就报错，而不是让整场会话挂着。

    这里不用固定 sleep：整套用例跑在 150s+ 的会话里，20ms 足够被别的任务挤掉，
    于是 `session.gets[0]` 在 CI 上随机 IndexError（本地单跑永远发现不了）。
    """
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"等待条件超时（{timeout}s）")
        await asyncio.sleep(0.02)
