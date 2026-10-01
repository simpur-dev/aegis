"""平台装配容器：把总线、注册中心、网关、服务、链路、摄取组装成一个可启动可关停的整体。

`latency_report()` 是考核指标量测的统一出口——所有 SLA 判定基于运行时真实埋点，
不接受手工填写的数字。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
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
from aegis.domain.messages import TelemetryReading
from aegis.integrations import (
    AnalyticsRecorder,
    IntegrationState,
    StoreBundle,
    build_analytics,
    build_knowledge,
    build_store,
    start_store,
    stop_store,
)
from aegis.knowledge.provider import KnowledgeProvider
from aegis.observability.instrumentation import register_sla_budgets
from aegis.observability.metrics import MetricsExporter
from aegis.observability.tracer import Tracer
from aegis.pipeline.chain import ChainResult, HazardResponseChain
from aegis.services.delivery import ChannelAdapter, DeliveryDispatcher
from aegis.services.llm_gateway import LlmGateway, build_gateway_if_configured
from aegis.services.risk_engine import RiskEngine
from aegis.services.task_parser import TaskParser
from aegis.services.trigger_rules import RuleEngine
from aegis.services.warning_service import WarningService
from aegis.storage.store import StoreProtocol
from aegis.workflow.engine import WorkflowEngine
from aegis.workflow.services_bridge import build_workflow_services
from aegis.workflow.templates import register_builtin_templates

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
    store: StoreProtocol
    chain: HazardResponseChain
    ingest: IngestService
    dispatcher: DeliveryDispatcher
    exporter: MetricsExporter
    workflow: WorkflowEngine
    bundle: StoreBundle
    analytics: AnalyticsRecorder | None = None
    knowledge: KnowledgeProvider | None = None
    knowledge_state: IntegrationState | None = None
    simulator: HazardScenarioSimulator | None = None
    mock_agents: list[MockAgent] = field(default_factory=list)
    _tasks: list[asyncio.Task[None]] = field(default_factory=list)
    _stop: asyncio.Event = field(default_factory=asyncio.Event)
    _started: bool = False
    _store_state: IntegrationState | None = None

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
        # 可选子系统先接线再放行：持久层与分析旁路的可用性在 start 结束时就是已知事实，
        # 而不是第一次有人查指标时才暴露。二者都不阻断启动。
        self._store_state = await start_store(self.bundle, self.settings)
        if self.analytics is not None:
            await self.analytics.open()
        await self.transport.connect()
        await self.gateway.start()
        self.gateway.set_result_handler(self.chain.handle_agent_message)
        await self.registry.start_sweeper()

        if with_mock_agents:
            self.mock_agents = await start_mock_agents(self.transport, heartbeat_interval=0.5)

        await register_builtin_templates(self.workflow)

        if with_ingest_loop and self.ingest.sources:
            period = ingest_interval_seconds or self.settings.simulator_interval_seconds

            async def _ingest_forever() -> None:
                await self.ingest.run_loop(self._stop, interval_seconds=period)

            self._tasks.append(asyncio.create_task(_ingest_forever(), name="ingest-loop"))
        log.info("平台已启动", extra={"bus": self.transport.name, "mock_agents": len(self.mock_agents)})

    @property
    def stopping(self) -> bool:
        """关停信号：SSE 这类长连接据此立即收摊，否则优雅退出会被 keep-alive 窗口拖住。"""
        return self._stop.is_set()

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
        # 旁路最后关：分析缓冲要先把已受理的行排空，落库可以晚到，不能凭空消失。
        if self.analytics is not None:
            await self.analytics.close()
        await stop_store(self.bundle, grace_ms=float(self.settings.analytics_close_grace_ms))

    def add_source(self, source: DataSource) -> None:
        self.ingest.add_source(source)

    def integration_status(self) -> list[IntegrationState]:
        """可选子系统的当前事实：没启用、已启用、或启用了但降级，三者必须可区分。

        返回强类型事实而不是字典：序列化留给 API 边界，内部判定不靠 `object` 猜类型。
        """
        rows: list[IntegrationState] = [self._store_state if self._store_state is not None else self.bundle.state]
        if self.analytics is not None:
            rows.append(self.analytics.state())
        else:
            rows.append(IntegrationState(name="analytics", enabled=False, driver=self.settings.analytics_backend))
        rows.append(self.knowledge_state or IntegrationState(name="knowledge", enabled=False, driver="off"))
        return rows

    # ---------- 指标量测出口 ----------

    def latency_report(self) -> dict[str, object]:
        s = self.settings
        # 预算表只允许有一处定义（instrumentation.register_sla_budgets）。本方法此前自带一份副本，
        # 且把按"秒"记录的 ingest_end_to_end_seconds 乘了 1000 当毫秒预算，
        # 结果是 ≤5min 接入指标在任何时延下都不会被判违约。
        register_sla_budgets(self.tracer.ledger, s, self.exporter)

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
    llm_gateway: LlmGateway | None = None,
) -> PlatformContainer:
    cfg = settings or get_settings()
    gateway_llm = llm_gateway if llm_gateway is not None else build_gateway_if_configured(cfg)
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
    bundle = build_store(cfg)
    store = bundle.store
    analytics_sink, analytics_state = build_analytics(cfg)
    analytics = AnalyticsRecorder(analytics_sink, driver=analytics_state.driver, settings=cfg) if analytics_sink is not None else None
    knowledge, knowledge_state = build_knowledge(cfg, tracer)

    async def on_result(result: ChainResult) -> None:
        """单一落库点 + 分析旁路扇出：旁路未启用时这条链只有一步，语义与接入前完全一致。"""
        await store.record_chain(result)
        if analytics is not None:
            await analytics.record_chain(result)

    rule_engine = RuleEngine()
    dispatcher = DeliveryDispatcher(
        channels or default_channels(cfg),
        tracer,
        sla_reach_seconds=cfg.sla_reach_seconds,
    )
    warning_service = WarningService(
        tracer,
        settings=cfg,
        translator=gateway_llm.translate if gateway_llm is not None and gateway_llm.available else None,
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
        warning_service=warning_service,
        dispatcher=dispatcher,
        on_result=on_result,
        knowledge=knowledge,
    )
    simulator = HazardScenarioSimulator(seed=cfg.simulator_seed) if with_simulator else None

    async def on_readings(readings: Sequence[TelemetryReading]) -> None:
        if analytics is not None:
            await analytics.record_readings(readings)

    ingest = IngestService(
        gateway=gateway,
        store=store,
        tracer=tracer,
        settings=cfg,
        sources=[simulator] if simulator else [],
        on_readings=on_readings if analytics is not None else None,
    )

    workflow = WorkflowEngine(
        services=build_workflow_services(
            store=store,
            rule_engine=rule_engine,
            risk_engine=RiskEngine(rule_engine),
            warning_service=warning_service,
            dispatcher=dispatcher,
            gateway=gateway,
        ),
        tracer=tracer,
        settings=cfg,
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
        workflow=workflow,
        bundle=bundle,
        analytics=analytics,
        knowledge=knowledge,
        knowledge_state=knowledge_state,
        simulator=simulator,
    )

    # 进程启动即登记考核预算：违约判定不能等到第一次有人查指标才开始生效
    register_sla_budgets(tracer.ledger, cfg, container.exporter)

    return container
