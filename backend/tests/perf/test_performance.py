"""性能与并发回归基准。

原则：优先断言"相对关系"（并发是否真的并发、是否串行），绝对阈值给足 CI 机器余量，
这样既能在算法退化成串行时立刻报警，又不会因机器抖动而随机红灯。
"""

from __future__ import annotations

import asyncio
import time

from aegis.agents.mock import MockAgent
from aegis.bus import subjects
from aegis.bus.inmemory import InMemoryBus
from aegis.bus.transport import encode
from aegis.config import Settings
from aegis.connectors.base import DataSource
from aegis.connectors.simulator import HazardScenarioSimulator
from aegis.container import create_container
from aegis.domain.enums import Action, AgentType
from aegis.domain.messages import TelemetryReading, make_event, new_trace_id, utc_now
from aegis.services.trigger_rules import RuleEngine

TRACE = "trc_" + "8" * 16


class _BatchSource(DataSource):
    """一次性回放固定读数，用于摄取吞吐基准。"""

    name = "bulk_replay"

    def __init__(self, name: str, readings: list[TelemetryReading]) -> None:
        self.name = name
        self._readings = list(readings)
        self._taken = False

    async def collect(self) -> list[TelemetryReading]:
        if self._taken:
            return []
        self._taken = True
        return self._readings


def _grouped(readings):
    grouped: dict[str, list] = {}
    for reading in readings:
        grouped.setdefault(reading.region_code, []).append(reading)
    return grouped


class TestCodecThroughput:
    def test_orjson_backend_is_active(self) -> None:
        """性能选型依赖 orjson：若退化为标准库 json，本测试应立即失败。"""
        module = getattr(encode, "__globals__", {})
        assert "orjson" in module, "消息编解码未使用 orjson，编码路径退化"

    def test_encode_decode_throughput(self) -> None:
        message = make_event(
            source="perceive.t01", action=Action.PERCEIVE_ANOMALY.value, payload={"v": 1, "text": "泥石流"}, trace_id=TRACE
        )
        round_trips = 20_000
        started = time.perf_counter()
        for _ in range(round_trips):
            type(message).decode(encode(message))
        elapsed = time.perf_counter() - started
        rate = round_trips / elapsed
        assert rate > 15_000, f"消息编解码吞吐仅 {rate:.0f}/s，低于阈值"


class TestRuleEngineThroughput:
    def test_evaluate_scale(self) -> None:
        engine = RuleEngine()
        simulator = HazardScenarioSimulator(scenario="surge", seed=5)
        rounds = 200
        started = time.perf_counter()
        hits = 0
        for _index in range(rounds):
            readings = simulator.collect_at(utc_now())
            hits += len(engine.evaluate(readings, now=utc_now()).hits)
        elapsed = time.perf_counter() - started
        assert hits > 0
        per_round = elapsed / rounds
        assert per_round < 0.02, f"单轮全站点规则评估 {per_round * 1000:.1f}ms 超预算"


class TestBusFanOut:
    async def test_publish_throughput(self, bus: InMemoryBus) -> None:
        received = 0

        async def sink(message) -> None:
            nonlocal received
            received += 1

        await bus.subscribe(subjects.agent_out("perceive"), sink)
        message = make_event(source="perceive.t01", action=Action.PERCEIVE_ANOMALY.value, payload={}, trace_id=TRACE)
        total = 20_000
        started = time.perf_counter()
        for _ in range(total):
            await bus.publish(subjects.agent_out("perceive"), message)
        await bus.idle(timeout=30.0)
        elapsed = time.perf_counter() - started
        assert received == total
        assert total / elapsed > 8_000, f"总线投递吞吐 {total / elapsed:.0f}/s 低于阈值"

    async def test_queue_group_parallelises(self, bus: InMemoryBus) -> None:
        """4 个消费者队列组：单条耗时工作应被并行摊薄，而不是串行叠加。"""
        work_ms = 60.0
        counter = {"done": 0}

        async def worker(message) -> None:
            await asyncio.sleep(work_ms / 1000)
            counter["done"] += 1

        for _ in range(4):
            await bus.subscribe(subjects.agent_in("assess"), worker, queue="pool")

        message = make_event(source="platform.gateway", target="assess.t01", action=Action.ASSESS_HAZARD.value, payload={}, trace_id=TRACE)
        jobs = 8
        started = time.perf_counter()
        for _ in range(jobs):
            await bus.publish(subjects.agent_in("assess"), message)
        await bus.idle(timeout=30.0)
        elapsed = time.perf_counter() - started

        serial = jobs * work_ms / 1000
        assert counter["done"] == jobs
        assert elapsed < serial / 2, f"队列组未并行：耗时 {elapsed:.2f}s（串行基线 {serial:.2f}s）"


class TestCollaborationConcurrency:
    async def test_parallel_transactions(self, settings: Settings) -> None:
        container = create_container(settings, with_simulator=False)
        await container.start()
        try:
            agents = [
                MockAgent(
                    transport=container.transport,
                    agent_type=AgentType.ASSESS,
                    agent_id=f"assess.load{index:02d}",
                    capabilities=["risk_assess"],
                    latency_ms=40.0,
                    max_concurrency=8,
                    seed=index,
                )
                for index in range(4)
            ]
            for agent in agents:
                await agent.start(heartbeat_interval=0.05)
            await asyncio.sleep(0.1)
            await container.transport.idle()

            jobs = 32
            started = time.perf_counter()
            await asyncio.gather(
                *(
                    container.gateway.dispatch(
                        AgentType.ASSESS,
                        Action.ASSESS_HAZARD,
                        {"region_code": "540121", "hits": []},
                        trace_id=new_trace_id(),
                        capability="risk_assess",
                    )
                    for _ in range(jobs)
                )
            )
            elapsed = time.perf_counter() - started

            assert container.gateway.success_rate() == 1.0
            serial = jobs * 40 / 1000
            assert elapsed < serial / 2, f"协同事务未并发：{elapsed:.2f}s（串行基线 {serial:.2f}s）"
            # 负载均衡：4 个实例都应接到任务
            assert min(agent.handled for agent in agents) >= 1
            for agent in agents:
                await agent.stop()
        finally:
            await container.shutdown()


class TestChainConcurrency:
    async def test_regions_processed_concurrently(self, settings: Settings) -> None:
        container = create_container(settings, with_simulator=False)
        await container.start()
        try:
            # 单区域耗时基线
            readings = HazardScenarioSimulator(scenario="surge", seed=11).collect_at(utc_now())
            grouped = _grouped(readings)
            started = time.perf_counter()
            await container.chain.process_many(grouped)
            single = time.perf_counter() - started

            # 复制到 12 个区域，若真并发则总耗时应远小于 12 倍单区域耗时
            many: dict[str, list] = {}
            for offset in range(12):
                for region, rows in grouped.items():
                    clone = [r.model_copy(update={"region_code": f"{offset:02d}{region}", "station_id": f"RG-{offset}"}) for r in rows]
                    many.setdefault(f"{offset:02d}{region}", []).extend(clone)

            started = time.perf_counter()
            results = await container.chain.process_many(many)
            scaled = time.perf_counter() - started

            assert len(results) == len(many)
            assert all(r.ok for r in results)
            assert scaled < single * len(many) / 2, f"多区域链路近似串行（单区域 {single:.3f}s，12 区域 {scaled:.3f}s）"
        finally:
            await container.shutdown()


class TestIngestThroughput:
    async def test_bulk_ingest(self, settings: Settings) -> None:
        container = create_container(settings, with_simulator=False)
        await container.start()
        try:
            simulator = HazardScenarioSimulator(scenario="surge", seed=7)
            simulator.suspect_rate = 0.0
            readings = [r for _ in range(300) for r in simulator.collect_at(utc_now())]
            assert len(readings) >= 4_000

            container.ingest.remove_source("sim_field_nodes")
            container.ingest.add_source(_BatchSource("bulk_replay", readings))

            started = time.perf_counter()
            report = await container.ingest.ingest_once()
            elapsed = time.perf_counter() - started

            assert report.readings == len(readings)
            assert report.published == report.readings
            assert report.ok
            assert elapsed < 8.0, f"{len(readings)} 条读数摄取耗时 {elapsed:.2f}s 超预算"
            assert container.store.telemetry.size == len(readings)
        finally:
            await container.shutdown()


class TestRegistryScale:
    def test_sweep_is_linear(self, settings: Settings) -> None:
        from aegis.bus.registry import AgentRegistry
        from aegis.domain.messages import CapabilityRegistration

        registry = AgentRegistry(settings)
        for index in range(500):
            registry.register(
                CapabilityRegistration(
                    agent_id=f"assess.a{index:03d}",
                    agent_type="assess",
                    capabilities=["risk_assess"],
                )
            )
        started = time.perf_counter()
        registry.sweep()
        elapsed = time.perf_counter() - started
        assert elapsed < 0.25, f"500 实例健康巡检耗时 {elapsed * 1000:.1f}ms 超预算"


class TestNoBlockingInEventLoop:
    async def test_ingest_does_not_stall_bus(self, settings: Settings) -> None:
        """摄取与总线并发推进：若摄取阻塞事件循环，心跳计数会明显落后。"""
        container = create_container(settings, with_simulator=True)
        await container.start()
        try:
            ticks = 0

            async def ticker() -> None:
                nonlocal ticks
                while True:
                    await asyncio.sleep(0.01)
                    ticks += 1

            task = asyncio.create_task(ticker())
            try:
                for _ in range(3):
                    await container.ingest.ingest_once()
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            assert ticks >= 3, f"事件循环被阻塞（心跳计数 {ticks}）"
        finally:
            await container.shutdown()
