"""平台装配容器：把总线、注册中心、网关、服务、链路、摄取组装成一个可启动可关停的整体。

`latency_report()` 是考核指标量测的统一出口——所有 SLA 判定基于运行时真实埋点，
不接受手工填写的数字。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from aegis.agents.mock import MockAgent, start_mock_agents
from aegis.bus.gateway import AgentGateway, ContractRegistry
from aegis.bus.inmemory import InMemoryBus
from aegis.bus.registry import AgentRegistry
from aegis.bus.transport import BusTransport
from aegis.config import Settings, get_settings
from aegis.connectors.base import DataSource, IngestService
from aegis.connectors.simulator import HazardScenarioSimulator
from aegis.domain.enums import Channel
from aegis.observability.metrics import MetricsExporter
from aegis.observability.tracer import Tracer
from aegis.pipeline.chain import HazardResponseChain
from aegis.services.delivery import ChannelAdapter, DeliveryDispatcher
from aegis.services.risk_engine import RiskEngine
from aegis.services.task_parser import TaskParser
from aegis.services.trigger_rules import RuleEngine
from aegis.services.warning_service import WarningService
from aegis.storage.store import PlatformStore

log = logging.getLogger("aegis.container")


def build_transport(settings: Settings) -> BusTransport:
    if settings.bus_backend == "nats":
        from aegis.bus.nats_bus import NatsBus  # 延迟导入：仅在启用时要求 nats 依赖

        return NatsBus(settings.nats_url, stream_prefix=settings.nats_stream_prefix)
    return InMemoryBus()


def default_channels(settings: Settings) -> dict[Channel, ChannelAdapter]:
    """默认 mock 通道矩阵：演练与压测可复现；真实网关通过 delivery_mode=http 接入。"""
    if settings.delivery_mode == "mock":
        return {
            Channel.SMS: MockChannel(settings),
            Channel.BEIDOU: MockChannel(settings, latency_ms=900.0, name=Channel.BEIDOU),
            Channel.BROADCAST: MockChannel(settings, latency_ms=400.0, name=Channel.BROADCAST),
            Channel.WECHAT: MockChannel(settings, latency_ms=250.0, name=Channel.WECHAT),
        }

    raise NotImplementedError("http 通道需注入 httpx 客户端，见 M4 现场联调批次")


class MockChannel:
    def __init__(self, settings: Settings, *, name: Channel = Channel.SMS, latency_ms: float = 150.0) -> None:
        from aegis.services.delivery import MockChannelAdapter

        self._inner = MockChannelAdapter(
            name=name,
            latency_ms=latency_ms,
            fail_rate=0.0,
            receipt_latency_ms=300.0,
            seed=settings.simulator_seed,
        )
        self.name = name

    async def send(self, record, audiences):
        return await self._inner.send(record, audiences)


@dataclass
class PlatformContainer:
    settings: Settings
    transport: BusTransport
    registry: AgentRegistry
    contracts: ContractRegistry
    tracer: Tracer
    gateway: AgentGateway
    store: PlatformStore
    chain: HazardResponseChain
    ingest: IngestService
    dispatcher: DeliveryDispatcher
    exporter: MetricsExporter
    simulator: HazardScenarioSimulator | None = None
    mock_agents: list[MockAgent] = field(default_factory=list)
    _tasks: list[asyncio.Task[None]] = field(default_factory=list)
    _stop: asyncio.Event = field(default_factory=asyncio.Event)
    _started: bool = False

    async def start(
        self,
        *,
        with_mock_agents: bool = False,
        with_ingest_loop: bool = False,
        ingest_interval_seconds: float | None = None,
    ) -> None:
        if self._started:
            log.debug("容器已启动，忽略重复调用")
            return
        self._started = True
        await self.transport.connect()
        await self.gateway.start()
        self.gateway.set_result_handler(self.chain.handle_agent_message)
        await self.registry.start_sweeper()

        if with_mock_agents:
            self.mock_agents = await start_mock_agents(self.transport, heartbeat_interval=0.5)

        if with_ingest_loop and self.ingest.sources:
            period = ingest_interval_seconds or self.settings.simulator_interval_seconds

            async def _ingest_forever() -> None:
                await self.ingest.run_loop(self._stop, interval_seconds=period)

            self._tasks.append(asyncio.create_task(_ingest_forever(), name="ingest-loop"))
        log.info("平台已启动", extra={"bus": self.transport.name, "mock_agents": len(self.mock_agents)})

    async def shutdown(self) -> None:
        self._stop.set()
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        for agent in self.mock_agents:
            await agent.stop()
        self.mock_agents = []
        await self.registry.stop_sweeper()
        await self.gateway.close()
        await self.transport.close()

    def add_source(self, source: DataSource) -> None:
        self.ingest.add_source(source)

    # ---------- 指标量测出口 ----------

    def latency_report(self) -> dict[str, object]:
        s = self.settings
        budgets_ms = {
            "sync_agent_to_gateway_ms": s.sla_sync_ms,
            "collab_txn": s.sla_reschedule_ms,
            "stage_plan_ms": s.sla_schedule_ms,
            "stage_assess_ms": s.sla_schedule_ms,
            "warning_reach_ms": s.sla_reach_seconds * 1000,
            "warning_generation_ms": s.sla_warning_gen_seconds * 1000,
            "ingest_end_to_end_seconds": s.sla_ingest_seconds * 1000,
        }
        for name, budget in budgets_ms.items():
            self.tracer.ledger.set_budget(name, budget)

        metrics = {name: self.tracer.ledger.stats(name).as_dict() for name in self.tracer.ledger.names()}
        violations = {name: stats["breaches"] for name, stats in metrics.items() if isinstance(stats, dict) and stats.get("breaches")}
        rate = self.gateway.success_rate()
        return {
            "sla_thresholds": {
                "sync_ms": s.sla_sync_ms,
                "schedule_ms": s.sla_schedule_ms,
                "reschedule_ms": s.sla_reschedule_ms,
                "ingest_seconds": s.sla_ingest_seconds,
                "warning_gen_seconds": s.sla_warning_gen_seconds,
                "reach_seconds": s.sla_reach_seconds,
                "collaboration_success_rate": s.sla_collaboration_success_rate,
            },
            "metrics": metrics,
            "collaboration": {
                "transactions": len(self.gateway.transactions),
                "success_rate": rate,
                "target": s.sla_collaboration_success_rate,
                "pass": rate is not None and rate >= s.sla_collaboration_success_rate,
            },
            "violations": violations,
            "gateway_counters": dict(self.gateway.counters),
            "store": self.store.snapshot(),
        }


def create_container(
    settings: Settings | None = None,
    *,
    transport: BusTransport | None = None,
    contracts_dir=None,
    channels: dict[Channel, ChannelAdapter] | None = None,
    with_simulator: bool = True,
) -> PlatformContainer:
    cfg = settings or get_settings()
    bus = transport or build_transport(cfg)
    registry = AgentRegistry(cfg)
    contracts = ContractRegistry(contracts_dir)
    tracer = Tracer()
    gateway = AgentGateway(
        bus,
        registry,
        contracts,
        tracer,
        allow_unregistered_action=cfg.gateway_allow_unregistered_action,
        default_deadline_ms=cfg.default_request_deadline_ms,
    )
    store = PlatformStore()
    rule_engine = RuleEngine()
    dispatcher = DeliveryDispatcher(
        channels or default_channels(cfg),
        tracer,
        sla_reach_seconds=cfg.sla_reach_seconds,
    )
    chain = HazardResponseChain(
        gateway=gateway,
        registry=registry,
        contracts=contracts,
        tracer=tracer,
        settings=cfg,
        rule_engine=rule_engine,
        risk_engine=RiskEngine(rule_engine),
        task_parser=TaskParser(contracts, settings=cfg),
        warning_service=WarningService(tracer, settings=cfg),
        dispatcher=dispatcher,
        on_result=store.record_chain,
    )
    simulator = HazardScenarioSimulator(seed=cfg.simulator_seed) if with_simulator else None
    ingest = IngestService(
        gateway=gateway,
        store=store,
        tracer=tracer,
        settings=cfg,
        sources=[simulator] if simulator else [],
    )
    container = PlatformContainer(
        settings=cfg,
        transport=bus,
        registry=registry,
        contracts=contracts,
        tracer=tracer,
        gateway=gateway,
        store=store,
        chain=chain,
        ingest=ingest,
        dispatcher=dispatcher,
        exporter=MetricsExporter(gateway, tracer),
        simulator=simulator,
    )

    return container
