"""真实 eclipse-zenoh 运行时的在线验证（默认跳过）。

本机起法（不需要 router，两个 peer 会话经 TCP 直连即可）：
    pip install eclipse-zenoh          # 本机实测 1.10.1
    AEGIS_TEST_ZENOH=1 pytest tests/integration/test_edge_zenoh_live.py

只验"替身 session 给不了"的四件事：
1. 真 `zenoh.open(config)` 接受 `build_config()` 产出的键名与取值类型
   （上一版写了两个本版本不存在的键，替身永远发现不了，而这条路径正是 from_env 的默认路径）；
2. 两端 publish/subscribe 真的互通，JSON 载荷在两端编解码一致；
3. queryable 的原生请求-响应跨会话可用（应答按 causation_id 认领）；
4. 边缘缓冲的 store/query 能被对端查到，且查询只读、不消费。
"""

from __future__ import annotations

import asyncio
import os

import pytest

from aegis.domain.messages import AgentMessage, make_event, new_trace_id
from aegis.edge.offline_buffer import EdgeOfflineBuffer
from aegis.edge.zenoh_transport import ZenohEdgeBus

try:
    import zenoh as _zenoh
except ImportError:  # pragma: no cover - 未装 edge extra
    _zenoh = None

pytestmark = [
    pytest.mark.skipif(not os.getenv("AEGIS_TEST_ZENOH"), reason="未设置 AEGIS_TEST_ZENOH，跳过真实 zenoh 运行时测试"),
    pytest.mark.skipif(_zenoh is None, reason="eclipse-zenoh 运行时未安装"),
    pytest.mark.slow,
]

ENDPOINT = "tcp/127.0.0.1:7457"
SUBJECT = "station.telemetry"
BUFFERED_SUBJECT = "station.buffered"
DEADLINE = 15.0


def _station() -> ZenohEdgeBus:
    return ZenohEdgeBus(
        mode="peer",
        listen_endpoints=(ENDPOINT,),
        multicast_scouting=False,
        buffer=EdgeOfflineBuffer(max_items=64, max_bytes=1 << 20),
    )


def _gateway() -> ZenohEdgeBus:
    return ZenohEdgeBus(mode="peer", connect_endpoints=(ENDPOINT,), multicast_scouting=False)


def _message(payload: dict[str, object]) -> AgentMessage:
    return make_event(source="station.node01", action="telemetry.report", payload=payload, trace_id=new_trace_id())


async def _until(predicate, *, timeout: float = DEADLINE, what: str = "条件") -> None:
    """zenoh 的匹配建立与投递都是异步的：轮询到成立为止，而不是睡一个"大概够"的时长。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"{timeout}s 内{what}未成立")


class TestLiveZenohLink:
    async def test_two_peer_sessions_exchange_messages_over_tcp(self) -> None:
        station, gateway = _station(), _gateway()
        received: list[AgentMessage] = []

        async def _handler(message: AgentMessage) -> None:
            received.append(message)

        try:
            await station.connect()
            await gateway.connect()
            subscription = await gateway.subscribe(SUBJECT, _handler)

            # 声明与匹配建立之间有一拍：这期间发的消息会先进边缘缓冲，复链判定后自动补发
            await station.publish(SUBJECT, _message({"rain_10min": 41.5, "station": "RG-540121-01"}))
            await _until(lambda: len(received) >= 1, what="跨会话投递")

            assert received[0].payload["rain_10min"] == 41.5
            assert received[0].action == "telemetry.report"
            assert received[0].source == "station.node01"
            await subscription._cancel()
        finally:
            await gateway.close()
            await station.close()

    async def test_native_query_reply_round_trip(self) -> None:
        station, gateway = _station(), _gateway()
        request = _message({"op": "ping"})

        async def _answer(message: AgentMessage) -> AgentMessage:
            return message.model_copy(update={"payload": {"op": "pong", "echo": message.payload}, "causation_id": message.msg_id})

        try:
            await station.connect()
            await gateway.connect()
            served = await station.serve(SUBJECT, _answer)

            reply = await gateway.request_reply(SUBJECT, request, timeout_ms=5_000)

            assert reply.payload["op"] == "pong"
            assert reply.causation_id == request.msg_id
            await served._cancel()
        finally:
            await gateway.close()
            await station.close()

    async def test_edge_buffer_is_queryable_from_the_other_side(self) -> None:
        """没人订阅的 subject 必须留在边缘，并能被对端原样查到——弱网期网关要先看见"攒了什么"。

        重放节拍被调到 30s（本用例里手动触发一次），否则"链路是通的"会让后台循环立刻把这条补发掉，
        就看不到边缘存留了。这也正是真实弱网里的差别：链路与匹配是两件事。
        """
        station = ZenohEdgeBus(mode="peer", listen_endpoints=(ENDPOINT,), multicast_scouting=False, replay_interval_s=30.0)
        gateway = _gateway()
        received: list[AgentMessage] = []

        async def _handler(message: AgentMessage) -> None:
            received.append(message)

        try:
            await station.connect()
            await gateway.connect()
            served = await station.serve_edge_buffer()

            await station.publish(BUFFERED_SUBJECT, _message({"v": 7}))
            await _until(lambda: station.buffer.pending == 1, what="消息进入边缘缓冲")

            rows = await gateway.query_edge_buffer(BUFFERED_SUBJECT, limit=10, timeout_ms=2_000)

            assert rows, f"对端查不到边缘内容: {station.snapshot()}"
            stored = [row for row in rows if isinstance(row.get("payload"), dict) and row["payload"].get("payload", {}).get("v") == 7]
            assert stored, rows
            assert stored[0]["seq"] >= 1 and stored[0]["size"] > 0, "查到的每条都要带序号与字节数，网关才能判断攒了多少、多久之前的"
            assert station.buffer.pending == 1, "查询必须只读：查一次就清空边缘缓冲是灾难"

            # 网关随后补上订阅，边缘把这批按序补发——这就是"先看再收"的完整闭环
            subscription = await gateway.subscribe(BUFFERED_SUBJECT, _handler)
            await asyncio.sleep(1.0)
            assert await station._worker.call(station._replay_once) == 1
            await _until(lambda: len(received) >= 1, what="补发的消息送达")
            assert received[0].payload["v"] == 7
            assert station.buffer.pending == 0

            await subscription._cancel()
            await served._cancel()
        finally:
            await gateway.close()
            await station.close()

    async def test_replayed_batch_arrives_in_submission_order(self) -> None:
        """先断后连再补发：跨会话重放必须保持提交顺序（单测里用替身证过，这里在真链路上复核）。"""
        station, gateway = _station(), _gateway()
        received: list[AgentMessage] = []

        async def _handler(message: AgentMessage) -> None:
            received.append(message)

        try:
            await station.connect()
            await gateway.connect()
            served = await gateway.subscribe(SUBJECT, _handler)
            indexes = list(range(5))
            for index in indexes:
                await station.publish(SUBJECT, _message({"i": index}))

            await _until(lambda: len(received) >= len(indexes), what="五条消息全部送达")

            got = [message.payload["i"] for message in received[: len(indexes)]]
            assert got == indexes, f"顺序被打乱：{got}"
            await served._cancel()
        finally:
            await gateway.close()
            await station.close()
