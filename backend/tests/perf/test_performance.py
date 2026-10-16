"""性能与并发回归基准。

原则：优先断言"相对关系"（并发是否真的并发、是否串行），绝对阈值给足 CI 机器余量，
这样既能在算法退化成串行时立刻报警，又不会因机器抖动而随机红灯。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

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
from aegis.services.rules.trigger_rules import RuleEngine

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
    """摄取不许在事件循环里同步等。量的是**最长一次 tick 停顿**，并且仪表先自校准。

    上一版断的是"三次 `ingest_once` 期间 ticker 至少走到 3"，那实际是拿步长（10ms）去量
    "这三次一共花了多久"：快机器上三次摄取不到 10ms 就跑完，ticker 一步没走 ⇒ CI 的 Linux
    runner 必红；本机 Windows 时钟粒度约 15.6ms、摄取又慢，反而常绿。按机器速度成败的断言
    抓不到阻塞，只会挑出更快的机器。
    """

    @staticmethod
    async def _gaps_while(body: Callable[[], Awaitable[None]], *, step_s: float = 0.01) -> tuple[list[float], float]:
        """跑 ticker 边执行 body，返回（相邻 tick 的间隔序列，body 总耗时）。"""
        gaps: list[float] = []

        async def ticker() -> None:
            last = time.perf_counter()
            while True:
                await asyncio.sleep(step_s)
                now = time.perf_counter()
                gaps.append(now - last)
                last = now

        task = asyncio.create_task(ticker())
        started = time.perf_counter()
        try:
            # 先让 ticker 真跑出一步：`create_task` 只是排期，body 若是同步阻塞的，
            # 它连起点都没设上，量出来的就是「阻塞没发生」而不是「阻塞多久」。
            while not gaps:
                await asyncio.sleep(step_s)
            await body()
            # 冲刷一步再停表：body 若是同步阻塞的，ticker 那个待完成的 sleep 只有等 body 让出循环
            # 之后才会落地——不等这一步，度量到的就是空序列，而空序列看着像『没红』。
            await asyncio.sleep(step_s * 3)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return gaps, time.perf_counter() - started

    async def test_仪表先自校准_同步阻塞必须被测出来(self) -> None:
        """这条不测产品，测的是"这副仪表能不能看见阻塞"。

        两段都是 0.3 秒，区别只在让不让出循环：`await asyncio.sleep` 期间 ticker 照常走，
        `time.sleep` 期间它一步都动不了。两种都测出来，才说明下面那条"没红"是结论而不是运气
        （校验器先被校验——否则它只是一个恰好会绿的装饰）。
        """

        async def yielding() -> None:
            await asyncio.sleep(0.3)

        async def blocking() -> None:
            time.sleep(0.3)

        free, _ = await self._gaps_while(yielding)
        stuck, _ = await self._gaps_while(blocking)
        assert max(free) < 0.1, f"挂起等待期间也测出了大间隔（{max(free) * 1000:.0f}ms），这副仪表不可信"
        assert max(stuck) >= 0.25, f"同步 sleep(0.3) 都没被测出来（最大间隔 {max(stuck) * 1000:.0f}ms），仪表失灵"

    async def test_摄取期间循环没被占住(self, settings: Settings) -> None:
        container = create_container(settings, with_simulator=True)
        await container.start()
        try:
            rounds = 0

            async def body() -> None:
                nonlocal rounds
                deadline = time.perf_counter() + 0.15
                while True:
                    await container.ingest.ingest_once()
                    rounds += 1
                    if time.perf_counter() >= deadline and rounds >= 3:
                        break

            gaps, elapsed = await self._gaps_while(body)
            assert rounds >= 3, f"观察窗内只做了 {rounds} 轮摄取，窗口不成立——停顿判定就没有底子"
            assert len(gaps) >= 5, (
                f"观察窗内 ticker 只走了 {len(gaps)} 步（{rounds} 轮摄取、共 {elapsed * 1000:.0f}ms），样本不足以判有无阻塞"
            )
            worst = max(gaps)
            assert worst < 0.25, f"摄取期间事件循环最长停顿 {worst * 1000:.0f}ms（步长 10ms，超 25 倍即有人在循环里同步等）"
        finally:
            await container.shutdown()
