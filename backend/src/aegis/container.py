"""平台装配容器：把总线、注册中心、网关、服务、链路、摄取组装成一个可启动可关停的整体。

`latency_report()` 是考核指标量测的统一出口——所有 SLA 判定基于运行时真实埋点，
不接受手工填写的数字。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from aegis.agents.mock import MockAgent, start_mock_agents
from aegis.bus.gateway import AgentGateway, ContractRegistry
from aegis.bus.inmemory import InMemoryBus
from aegis.bus.registry import AgentRegistry
from aegis.bus.transport import BusTransport
from aegis.config import Settings, get_settings
from aegis.connectors.base import DataSource, IngestService
from aegis.connectors.mqtt import MqttSource
from aegis.connectors.simulator import HazardScenarioSimulator
from aegis.connectors.weather_api import WeatherApiSource
from aegis.domain.enums import Channel
from aegis.domain.messages import TelemetryReading
from aegis.integrations import (
    AnalyticsRecorder,
    IntegrationState,
    StoreBundle,
    build_analytics,
    build_knowledge,
    build_mqtt,
    build_retrieval,
    build_store,
    build_weather,
    close_knowledge,
    start_store,
    stop_store,
    warm_knowledge,
    warm_retrieval,
)
from aegis.knowledge.provider import KnowledgeProvider
from aegis.observability.instrumentation import register_sla_budgets
from aegis.observability.metrics import MetricsExporter
from aegis.observability.telemetry import telemetry_status
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
from aegis.workflow.outbound import OutboundCaller, OutboundPolicy
from aegis.workflow.services_bridge import build_workflow_services
from aegis.workflow.templates import register_builtin_templates

if TYPE_CHECKING:
    from aegis.retrieval.service import HybridRetrievalService

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
    retrieval: HybridRetrievalService | None = None
    retrieval_state: IntegrationState | None = None
    mqtt: MqttSource | None = None
    mqtt_state: IntegrationState | None = None
    weather: WeatherApiSource | None = None
    weather_state: IntegrationState | None = None
    outbound: OutboundCaller | None = None
    simulator: HazardScenarioSimulator | None = None
    mock_agents: list[MockAgent] = field(default_factory=list)
    _tasks: list[asyncio.Task[None]] = field(default_factory=list)
    _stop: asyncio.Event = field(default_factory=asyncio.Event)
    _started: bool = False
    _store_state: IntegrationState | None = None
    _retrieval_warm_error: str | None = None
    _knowledge_schema_error: str | None = None
    _knowledge_close_error: str | None = None
    _mqtt_start_error: str | None = None

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
        if self.retrieval is not None:
            # 装载 568MB 权重要几秒：放在启动期，别让第一条预警替后续所有请求付这笔钱
            self._retrieval_warm_error = await warm_retrieval(self.retrieval)
        # 图谱索引初始化：纯内存装配不实现这件事，这里对它是空转；配了图谱却是坏图时，
        # 结果作为 knowledge 这条腿的降级事实出现在 /api/v1/integrations 上。
        self._knowledge_schema_error = await warm_knowledge(self.knowledge)
        if self.mqtt is not None:
            # 订阅腿起不来不阻断平台启动（broker 可能在站端那边还没起来），
            # 但失败必须成为 /api/v1/integrations 上的一行事实，而不是只进日志。
            try:
                await self.mqtt.start()
            except Exception as exc:
                self._mqtt_start_error = f"{type(exc).__name__}: {exc}"
                log.warning("MQTT 订阅腿启动失败，平台继续运行", extra={"error": self._mqtt_start_error})
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
        if self.mqtt is not None:
            # 订阅腿停在摄取循环之后：摄取任务已取消，没有人再取缓冲，继续收只会攒成一堆过期读数
            await self.mqtt.stop()
        if self.weather is not None:
            # 只关连接器自己建的 httpx 客户端；注入进来的（测试）由注入方负责
            await self.weather.aclose()
        if self.outbound is not None:
            await self.outbound.aclose()
        await self.registry.stop_sweeper()
        # 图谱腿自己揣着 Neo4j 驱动与 LLM 客户端，此前没人关；关不掉只留一行原因，不打断后面的停服步骤
        self._knowledge_close_error = await close_knowledge(self.knowledge)
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
        knowledge_state = self.knowledge_state or IntegrationState(name="knowledge", enabled=False, driver="off")
        knowledge_defects: dict[str, object] = {}
        if self._knowledge_schema_error is not None:
            # 图谱索引没建成的 knowledge 腿是"启用了但图谱那侧永远召不回"，只写日志外部读不到
            knowledge_defects["schema_error"] = self._knowledge_schema_error
        if self._knowledge_close_error is not None:
            # 停服期间图谱连接没关成：排障时要能区分"服务停了"与"服务停了但连接还开着"
            knowledge_defects["close_error"] = self._knowledge_close_error
        if knowledge_defects:
            knowledge_state = replace(knowledge_state, detail={**knowledge_state.detail, **knowledge_defects})
        rows.append(knowledge_state)
        retrieval_state = self.retrieval_state or IntegrationState(name="retrieval", enabled=False, driver="off")
        retrieval_detail: dict[str, object] = dict(retrieval_state.detail)
        # 外部索引的形态在启动期才成立（建表 + 灌数），装配时快照会得到"未建表 / 0 条"，
        # 那等于在状态面上把一条已经工作的腿读成没工作。
        if self.retrieval is not None and self.retrieval.index is not None:
            retrieval_detail["index"] = self.retrieval.index.status()
        if self._retrieval_warm_error is not None:
            # 权重在位但装载失败：装配事实是"启用了但这条腿跑不起来"，只写日志外部看不见
            retrieval_detail["warm_error"] = self._retrieval_warm_error
        if retrieval_detail != retrieval_state.detail:
            retrieval_state = replace(retrieval_state, detail=retrieval_detail)
        rows.append(retrieval_state)
        # MQTT 腿把运行期计数一并带出来：broker 是否连着、收了多少条、被拒/溢出各多少，
        # 这些正是"站端在发但平台没数"时唯一能区分故障位置的证据。
        mqtt_state = self.mqtt_state or IntegrationState(name="mqtt", enabled=False, driver="off")
        if self.mqtt is not None:
            detail: dict[str, object] = {**mqtt_state.detail, **self.mqtt.status()}
            if self._mqtt_start_error is not None:
                detail["start_error"] = self._mqtt_start_error
            mqtt_state = replace(mqtt_state, detail=detail)
        rows.append(mqtt_state)
        # 拉取腿同理：只配了 base_url 不代表采到数，"几轮、多少条、失败几次"才是事实。
        weather_state = self.weather_state or IntegrationState(name="weather", enabled=False, driver="off")
        if self.weather is not None:
            weather_state = replace(weather_state, detail={**weather_state.detail, **self.weather.status()})
        rows.append(weather_state)
        # 工作流外呼腿（`api_call` / `device_control` 的出口）：白名单为空时这条腿整条不接入。
        # 画布上摆了这两类节点却没人配白名单，运维要的是一行"没开"加上"为什么没开"，
        # 而不是等节点报 NodeError 之后再猜是哪一环。拒发/失败两个计数就是那两环的现场证据。
        if self.outbound is None:
            rows.append(IntegrationState(name="outbound", enabled=False, driver="off"))
        else:
            outbound_detail = {key: value for key, value in self.outbound.status().items() if key != "enabled"}
            rows.append(
                IntegrationState(
                    name="outbound",
                    enabled=self.outbound.enabled,
                    driver="http" if self.outbound.enabled else "off",
                    detail=outbound_detail,
                )
            )
        # 链路追踪不是"装了 OTel 就有证据"：没装配 provider 时跨度只落在本地，
        # 第三方在 Jaeger 里查不到任何东西。这一行让"上报中/仅本地"可判别。
        status = telemetry_status()
        rows.append(
            IntegrationState(
                name="tracing",
                enabled=bool(status["exporting"]),
                driver="otlp" if status["exporting"] else "local",
                detail=dict(status),
            )
        )
        return rows

    # ---------- 指标量测出口 ----------

    def latency_report(self) -> dict[str, Any]:  # JSON 形状的报表载体：字段异构，交给 API 边界序列化
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
    with_simulator: bool | None = None,
    llm_gateway: LlmGateway | None = None,
    weather_client: object | None = None,
    outbound_client: object | None = None,
) -> PlatformContainer:
    """装配一个平台容器。

    `with_simulator=None`（默认）跟随配置面 `simulator_enabled`——那个旋钮在 .env.example 里
    本来就承诺过的语义；显式传 True/False 是测试与演练的覆盖口。
    `weather_client` 只给测试注入 httpx 传输用，生产留空。
    `outbound_client` 同理，注入的是工作流节点外呼（`api_call`/`device_control`）的传输；
    白名单为空时这条腿整体不接入节点，外呼会响亮失败而不是发出请求。
    """
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
    retrieval, retrieval_state = build_retrieval(cfg, tracer, store=bundle.durability)
    mqtt_source, mqtt_state = build_mqtt(cfg)
    weather_source, weather_state = build_weather(cfg, client=weather_client)
    outbound = OutboundCaller(OutboundPolicy.from_settings(cfg), client=outbound_client)
    if not outbound.enabled:
        # 不是错误，但必须说清楚：画布上摆了 api_call/device_control 却没人配白名单时，
        # 运维要的是一句"这条腿没开"，而不是节点失败之后再猜为什么。
        log.info("工作流外呼未启用：AEGIS_WORKFLOW_HTTP_ALLOWED_HOSTS 为空，api_call/device_control 按缺少依赖服务失败")

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
        retrieval=retrieval,
    )
    use_simulator = cfg.simulator_enabled if with_simulator is None else with_simulator
    simulator = HazardScenarioSimulator(seed=cfg.simulator_seed) if use_simulator else None

    async def on_readings(readings: Sequence[TelemetryReading]) -> None:
        if analytics is not None:
            await analytics.record_readings(readings)

    ingest = IngestService(
        gateway=gateway,
        store=store,
        tracer=tracer,
        settings=cfg,
        sources=[source for source in (simulator, mqtt_source, weather_source) if source is not None],
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
            outbound=outbound,
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
        retrieval=retrieval,
        retrieval_state=retrieval_state,
        mqtt=mqtt_source,
        mqtt_state=mqtt_state,
        weather=weather_source,
        weather_state=weather_state,
        outbound=outbound,
        simulator=simulator,
    )

    # 进程启动即登记考核预算：违约判定不能等到第一次有人查指标才开始生效
    register_sla_budgets(tracer.ledger, cfg, container.exporter)

    return container
