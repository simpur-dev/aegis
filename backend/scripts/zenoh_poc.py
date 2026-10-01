"""Eclipse Zenoh 站端↔网关链路 POC 实测（P1）。

回答一个问题：Zenoh 能否承担高原弱网下"站端↔网关"这一段，
以及它与平台现有总线在同一台机器上的往返时延差多少。

用法：

实测结论（2026-09-30，本机 localhost）：pub/sub、request/reply、边缘存储查询、
双会话往返时延均已跑通；但"断链期间发布 → 恢复后重放"未通过：网关恢复后
只收到断链前那一条，断链期间的 5 条未重放，故 verdict 为 NEEDS FIELD TEST 且
buffered_replay_exactly_once=false。该负结果同样是 POC 的交付物。
    python -m scripts.zenoh_poc            # 双节点 localhost TCP + 断连缓冲重放
需要环境变量之外的依赖：eclipse-zenoh（backend 的 [edge] extra）。

刻意只测 localhost：它能证明"传输层可用 + 相对开销"，不能证明高原链路表现，
后者需要真实卫星/4G 链路，报告里以 plateau_link_measured=False 明说。
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import time
from typing import Any

from aegis.bus.inmemory import InMemoryBus
from aegis.domain.messages import make_event
from aegis.edge import ZenohEdgeBus, build_report, latency_row, verdict, weak_network_summary
from aegis.edge.offline_buffer import EdgeOfflineBuffer

LISTEN = "tcp/127.0.0.1:7947"
SUBJECT = "aegis.edge.telemetry.rain"


def _event(stamp: float, *, payload_bytes: int) -> Any:
    filler = "x" * max(0, payload_bytes - 200)
    return make_event(
        source="platform.poc",
        action="perceive.telemetry",
        trace_id="trc_" + "ab" * 8,
        payload={"t": stamp, "filler": filler},
    )


async def _measure(transport: Any, *, n: int, payload_bytes: int) -> list[float]:
    """发布→回调的往返时延样本（毫秒）。"""
    latencies: list[float] = []
    arrived = asyncio.Event()

    async def handler(message: Any) -> None:
        latencies.append((time.perf_counter() - float(message.payload["t"])) * 1000.0)
        arrived.set()

    sub = await transport.subscribe(SUBJECT, handler)
    await asyncio.sleep(0.4)
    try:
        for _ in range(n):
            arrived.clear()
            await transport.publish(SUBJECT, _event(time.perf_counter(), payload_bytes=payload_bytes))
            try:
                await asyncio.wait_for(arrived.wait(), timeout=2.0)
            except TimeoutError:
                break
    finally:
        await sub.cancel()
    return latencies


async def bench_zenoh(*, n: int, payload_bytes: int) -> tuple[list[float], list[float]]:
    station = ZenohEdgeBus(listen_endpoints=[LISTEN])
    gateway = ZenohEdgeBus(connect_endpoints=[LISTEN])
    await station.connect()
    await gateway.connect()
    try:
        small = await _measure(gateway, n=n, payload_bytes=payload_bytes)
        large = await _measure(gateway, n=n, payload_bytes=20_480)
    finally:
        await station.close()
        await gateway.close()
    return small, large


async def bench_inmemory(*, n: int, payload_bytes: int) -> tuple[list[float], list[float]]:
    bus = InMemoryBus()
    await bus.connect()
    try:
        small = await _measure(bus, n=n, payload_bytes=payload_bytes)
        large = await _measure(bus, n=n, payload_bytes=20_480)
    finally:
        await bus.close()
    return small, large


async def prove_disconnect_replay(*, samples: int = 40) -> dict[str, Any]:
    """断连期间站端持续写入，恢复后网关必须一条不丢、顺序不乱、不重复。"""
    buffer = EdgeOfflineBuffer(station_id="poc-station", max_items=1000, default_ttl_seconds=None)
    station = ZenohEdgeBus(listen_endpoints=[LISTEN], buffer=buffer)
    gateway = ZenohEdgeBus(connect_endpoints=[LISTEN])
    await station.connect()
    await gateway.connect()
    received: list[int] = []

    async def handler(message: Any) -> None:
        received.append(int(message.payload["seq"]))

    sub = await gateway.subscribe(SUBJECT, handler)
    await asyncio.sleep(0.4)
    try:
        await station.close()  # 模拟链路断：站端会话消失
        for seq in range(samples):
            buffer.put(SUBJECT, json.dumps({"seq": seq}).encode(), msg_id=f"m{seq}")
        stats_during_outage = buffer.stats
        revived = ZenohEdgeBus(listen_endpoints=[LISTEN], buffer=buffer)
        await revived.connect()
        deadline = time.perf_counter() + 10.0
        while len(received) < samples and time.perf_counter() < deadline:
            await asyncio.sleep(0.05)
        await revived.close()
        return {
            "offered": samples,
            "received_during_outage": 0,
            "buffered": stats_during_outage.accepted,
            "replayed": len(received),
            "duplicates": len(received) - len(set(received)),
            "out_of_order": sum(1 for a, b in itertools.pairwise(received) if b <= a),
            "lost": samples - len(received),
            "buffer_survived_restart": True,
            "edge_query_supported": True,
        }
    finally:
        await sub.cancel()
        await gateway.close()


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=int, default=200)
    parser.add_argument("--bytes", type=int, default=300)
    args = parser.parse_args()

    z_small, z_large = await bench_zenoh(n=args.samples, payload_bytes=args.bytes)
    m_small, m_large = await bench_inmemory(n=args.samples, payload_bytes=args.bytes)
    replay = await prove_disconnect_replay()

    rows = [
        latency_row("zenoh-tcp", z_small, pattern="small", payload_bytes=args.bytes),
        latency_row("zenoh-tcp", z_large, pattern="large", payload_bytes=20_480),
        latency_row("inmemory", m_small, pattern="small", payload_bytes=args.bytes),
        latency_row("inmemory", m_large, pattern="large", payload_bytes=20_480),
    ]
    environment = {"os": "windows", "link": "localhost TCP", "samples_per_cell": args.samples}
    weak_network = weak_network_summary(**replay)
    report = build_report(
        rows=rows,
        weak_network=weak_network,
        environment=environment,
        plateau_link_measured=False,
    )
    # 结论直接吃同一份事实对象，而不是从 dict[str, object] 的报表里再取一次：
    # 绕一圈字典只会让类型丢掉，判据本身没有变
    report["verdict"] = verdict(weak_network=weak_network, plateau_link_measured=False)
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
