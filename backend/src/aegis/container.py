"""平台装配容器：把总线、注册中心、网关、服务、链路、摄取组装成一个可启动可关停的整体。

`latency_report()` 是考核指标量测的统一出口——所有 SLA 判定基于运行时真实埋点，
不接受手工填写的数字。
"""

from __future__ import annotations

import asyncio
import logging
import time
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
from aegis.domain.messages import TelemetryReading, TriggerHit, utc_now
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
from aegis.services.assistant import AssistantActions, AssistantService
from aegis.services.delivery import (
    ChannelAdapter,
    DeliveryDispatcher,
    HttpChannelAdapter,
    normalize_gateway_target,
    parse_delivery_channels,
)
from aegis.services.llm_gateway import LlmGateway, build_gateway_if_configured
from aegis.services.risk_engine import RiskEngine
from aegis.services.semantic_parser import DisasterTextParser, EvidenceNote, ParsedDisaster
from aegis.services.task_parser import TaskParser
from aegis.services.trigger_rules import RuleEngine, rulebook_from_rows
from aegis.services.warning_service import WarningService
from aegis.storage.store import StoreProtocol, rule_library_port
from aegis.workflow.engine import WorkflowEngine
from aegis.workflow.outbound import OutboundCaller, OutboundPolicy, parse_host_list
from aegis.workflow.services_bridge import build_workflow_services
from aegis.workflow.templates import REPORT_REVIEW_TEMPLATE, register_builtin_templates, register_report_review_template

if TYPE_CHECKING:
    from aegis.retrieval.service import HybridRetrievalService

log = logging.getLogger("aegis.container")


def build_transport(settings: Settings) -> BusTransport:
    if settings.bus_backend == "nats":
        from aegis.bus.nats_bus import NatsBus  # 延迟导入：仅在启用时要求 nats 依赖

        return NatsBus(settings.nats_url, stream_prefix=settings.nats_stream_prefix)
    return InMemoryBus()


def default_channels(settings: Settings, *, client: Any | None = None) -> dict[Channel, ChannelAdapter]:
    """交付通道矩阵：mock 供演练与压测复现，http 才是现场触达。

    `delivery_mode=http` 而网关没配齐时是**构造期失败**，不静默回落 mock：
    把"网关没接上"读成"触达已达成"是这一型平台最贵的事故（架构铁律 7：指标必须实测、降级必须可见）。
    """
    if settings.delivery_mode == "mock":
        return {
            Channel.SMS: MockChannel(settings),
            Channel.BEIDOU: MockChannel(settings, latency_ms=900.0, name=Channel.BEIDOU),
            Channel.BROADCAST: MockChannel(settings, latency_ms=400.0, name=Channel.BROADCAST),
            Channel.WECHAT: MockChannel(settings, latency_ms=250.0, name=Channel.WECHAT),
        }
    return build_http_channels(settings, client=client)


def build_http_channels(settings: Settings, *, client: Any | None = None) -> dict[Channel, ChannelAdapter]:
    """按配置拼装真实通道。主机白名单为空即整条腿不可用（口径与 `workflow/outbound.py` 一致）。"""
    if client is None:
        raise ValueError("delivery_mode=http 需要注入 HTTP 客户端（见 build_delivery_client）")
    target = normalize_gateway_target(
        settings.delivery_http_base_url,
        allowed_hosts=parse_host_list(settings.delivery_http_allowed_hosts),
    )
    channels = parse_delivery_channels(settings.delivery_http_channels)
    return {
        channel: HttpChannelAdapter(
            channel,
            target.url,
            client,
            path=settings.delivery_http_path,
            timeout_ms=settings.delivery_http_timeout_ms,
        )
        for channel in channels
    }


def build_delivery_client(settings: Settings, *, client: Any | None = None) -> tuple[Any | None, bool]:
    """真实通道的 HTTP 客户端：凭据只进请求头、不跟随重定向。返回 (客户端, 是否本装配点自建)。"""
    if settings.delivery_mode != "http":
        return None, False
    if client is not None:
        return client, False
    import httpx  # 延迟导入：只有真实通道形态才要求 httpx

    headers = {"Authorization": f"Bearer {settings.delivery_http_token}"} if settings.delivery_http_token else {}
    return (
        httpx.AsyncClient(
            timeout=settings.delivery_http_timeout_ms / 1000,
            headers=headers,
            follow_redirects=False,
        ),
        True,
    )


class AssembledEvidence:
    """把知识层 + 检索层折成解析器认识的 `EvidenceNote`（装配点专属翻译，内核只认 Protocol）。

    `typical_level` 只取案例自带的 `applies_to_levels` 中最不利（数字最小）的一档；
    检索文档没有等级就不给等级——编一个等级出来，等于把"历史案例"降级成"我们的猜测"。
    """

    def __init__(self, *, knowledge: KnowledgeProvider | None, retrieval: HybridRetrievalService | None, settings: Settings) -> None:
        self._knowledge = knowledge
        self._retrieval = retrieval
        self._settings = settings

    async def corroborate(
        self,
        *,
        query: str,
        hazard_type: str | None,
        region_code: str | None,
        limit: int,
    ) -> list[EvidenceNote]:
        notes: list[EvidenceNote] = []
        if self._knowledge is not None:
            try:
                matches = await self._knowledge.recall(
                    query,
                    hazard_type=hazard_type,
                    region_code=region_code,
                    limit=limit,
                    budget_ms=self._settings.knowledge_recall_budget_ms,
                )
            except Exception as exc:
                log.warning("解析佐证：知识腿降级", extra={"err": type(exc).__name__})
                matches = []
            for match in matches:
                brief = match.planning_brief()
                notes.append(
                    EvidenceNote(
                        source=f"case:{match.case_id}",
                        text=str(brief.get("title") or match.case_id)[:200],
                        hazard_type=next(iter(brief.get("hazard_types") or ()), hazard_type),
                        typical_level=_worst_level(brief.get("applies_to_levels")),
                        refs=(match.case_id,),
                    )
                )
        if self._retrieval is not None:
            from aegis.retrieval.service import RetrievalQuery  # 延迟导入：装配点才碰检索 I/O 面

            try:
                outcome = await self._retrieval.retrieve(
                    RetrievalQuery(
                        text=query,
                        k=limit,
                        hazard_type=hazard_type,
                        region_code=region_code,
                        budget_ms=self._settings.retrieval_budget_ms,
                    )
                )
            except Exception as exc:
                log.warning("解析佐证：检索腿降级", extra={"err": type(exc).__name__})
                outcome = None
            if outcome is not None:
                for doc in outcome.docs:
                    reference = doc.as_reference()
                    notes.append(
                        EvidenceNote(
                            source=f"doc:{doc.doc_id}",
                            text=str(reference.get("text") or reference.get("title") or "")[:200],
                            hazard_type=reference.get("hazard_type"),
                            typical_level=None,  # 检索块没有等级口径，就不臆造
                            refs=(doc.doc_id,),
                        )
                    )
        return notes


def _worst_level(raw: object) -> int | None:
    levels = [int(item) for item in raw if isinstance(item, (int, float)) and 1 <= int(item) <= 5] if isinstance(raw, list) else []
    return min(levels) if levels else None


class AssembledScenarios:
    """把知识层的案例命中折成推演节点要的键（装配点翻译；内核只认 `ScenarioProvider` 协议）。

    刻意不捕异常：召回失败要作为 `situation_simulate` 的降级事实外显（节点侧写 `degraded`），
    在这里圆成"空案例"就等于把图谱挂了说成"没有相似案例"。
    图谱侧只回节点摘要（`case is None`）时，`planning_brief()` 就只有命中证据——
    推演节点拿不到 `estimated_delay_hours` 就不输出该要素，谁也不替案例补数。
    """

    def __init__(self, *, knowledge: KnowledgeProvider, settings: Settings) -> None:
        self._knowledge = knowledge
        self._settings = settings

    async def recall_scenarios(
        self,
        *,
        hazard_type: str | None = None,
        region_code: str | None = None,
        risk_level: int | None = None,
        limit: int = 3,
    ) -> list[dict[str, Any]]:
        query = " ".join(
            part
            for part in (
                hazard_type or "",
                region_code or "",
                "" if risk_level is None else f"{int(risk_level)}级",
            )
            if part
        )
        matches = await self._knowledge.recall(
            query or "山地灾害",
            hazard_type=hazard_type,
            region_code=region_code,
            limit=int(limit),
            budget_ms=self._settings.knowledge_recall_budget_ms,
        )
        return [brief for brief in (match.planning_brief() for match in matches) if brief.get("case_id")]


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
    rule_engine: RuleEngine
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
    parser: DisasterTextParser | None = None
    assistant: AssistantService | None = None
    delivery_client: Any | None = None
    _owns_delivery_client: bool = False
    #: 人工上报这条腿的账：受理数、阈值命中所得数、转人工核签数。
    #: 计数放在容器上而不是散在路由里，`latency_report()` 才能一次给全（指标必须可追溯到样本）。
    report_stats: dict[str, int] = field(
        default_factory=lambda: {"submitted": 0, "measured_by_rule": 0, "review_required": 0, "reviews_opened": 0}
    )
    #: 规则库装配事实（批次 C2）：读不到 / 有坏行都要能在外侧看到，而不是默默用另一版阈值
    rulebook_error: str | None = None
    rulebook_rejected: list[str] = field(default_factory=list)
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
        # 阈值版本在启动期就位：第一条预警用的必须是"当前生效那一版"，而不是代码里的种子
        await self._load_rulebook()
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
        # 核签流程与灾种剧本分开注册：低置信度上报要能开出一张真人可签的工单
        await register_report_review_template(self.workflow)

        if with_ingest_loop and self.ingest.sources:
            period = ingest_interval_seconds or self.settings.simulator_interval_seconds

            async def _ingest_forever() -> None:
                await self.ingest.run_loop(self._stop, interval_seconds=period)

            self._tasks.append(asyncio.create_task(_ingest_forever(), name="ingest-loop"))
        log.info("平台已启动", extra={"bus": self.transport.name, "mock_agents": len(self.mock_agents)})

    async def _load_rulebook(self) -> None:
        """从规则库装配当前生效的阈值集（批次 C2）。

        三种情况要能分开：存储后端不提供规则库（内存读视图）、读库失败、读成功但有坏行。
        任何一种都**沿用内置种子并留下一行事实**——静默换版比读失败危险得多：
        预警等级会跟着变，而报表上什么都看不出来。
        """
        port = rule_library_port(self.store)
        if port is None:
            return
        try:
            rows = await port.trigger_rules(status="active", limit=200)
        except Exception as exc:
            self.rulebook_error = f"规则库读取失败: {type(exc).__name__}: {str(exc)[:180]}"
            log.warning("规则库读取失败，沿用内置种子阈值", extra={"error": self.rulebook_error})
            return
        rules, rejected = rulebook_from_rows(rows)
        self.rulebook_rejected = rejected
        if not rules:
            self.rulebook_error = f"库里没有可装载的 active 规则（返回 {len(rows)} 行），沿用内置种子阈值"
            log.warning(self.rulebook_error)
            return
        self.rule_engine.reload(rules, source="postgres")

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
        if self._owns_delivery_client and self.delivery_client is not None:
            # 只关装配点自建的客户端；测试注入的那个由注入方负责（与气象拉取腿同一口径）
            await self.delivery_client.aclose()
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
        # 触达通道：mock 与 http 是两种不同的事实，不能都读成"能触达"。
        # 状态面只给脱敏后的网关地址（scheme://host）与各通道发送/失败计数，凭据不出现在这里。
        rows.append(
            IntegrationState(
                name="delivery",
                enabled=self.settings.delivery_mode == "http",
                driver=self.settings.delivery_mode,
                detail={"channels": self.dispatcher.channel_status()},
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
            "reports": dict(self.report_stats),
            # 触达口径必须自带身份：mock 通道的 1.2s 与真实网关的送达时间是两回事，
            # 分开报才不至于让"≤20min 触达"这项在演练数字上被读成达成（铁律 7）。
            "delivery": {"mode": s.delivery_mode, "channels": self.dispatcher.channel_status()},
            # 阈值版本的出处：指标 1"识别 ≥5 类触发条件"的证据要能落到具体版本与标定状态
            "rulebook": {**self.rule_engine.describe(), "error": self.rulebook_error, "rejected": self.rulebook_rejected[:5]},
            "store": self.store.snapshot(),
        }

    # ---------- 人工上报（第四条接入腿，架构文档 §6.2 场景二） ----------

    async def submit_report(
        self,
        *,
        note: str,
        region_code: str,
        reporter: str = "web",
        hazard_hint: str | None = None,
        location: tuple[float, float] | None = None,
    ) -> dict[str, Any]:
        """群防群治上报 → 三路融合解析 → **同一条链路**。

        这里刻意不复制一条"上报专用预警链"：解析只负责把文本变成触发命中与定级建议，
        之后的研判、决策、预警生成、靶向触达、降级留痕与时延量测全部走 `HazardResponseChain`。

        低置信度（等级不是阈值命中所得）如实外显 `human_review_required` 并计入 `report_stats`；
        核签动作在指挥员一侧（工作流实例的 `human_review` 节点），平台不自动起实例。
        """
        if self.parser is None:
            raise RuntimeError("解析服务未装配（report parser）")
        # 时长用 perf_counter，不用 utc_now()：时钟被同步一步，`max(…, 0.0)` 就把负数
        # 夹成一个看似合理的 0 秒，"人工上报接入 ≤5min" 这条会静默变成永绿。
        started = time.perf_counter()
        parsed = await self.parser.parse(note, region_code=region_code, hazard_hint=hazard_hint)
        region = parsed.region_code or region_code
        self.report_stats["submitted"] += 1
        if parsed.measured_by_rule:
            self.report_stats["measured_by_rule"] += 1
        if parsed.needs_review:
            self.report_stats["review_required"] += 1

        hits = list(parsed.hits)
        verdict = parsed.to_verdict()
        if verdict is not None and not hits:
            # 没有阈值命中也要带证据进链路：rule_id 明写 R-REPORT-*，
            # 让下游一眼看出这一条的等级来自申报值或建议值，不是测量值。
            hits = [
                TriggerHit(
                    rule_id=f"R-REPORT-{parsed.decided_by.upper()}",
                    hazard_type=parsed.hazard_type.value,
                    region_code=region,
                    evidence_refs=[f"reporter={reporter}", f"note={parsed.text[:120]}"],
                    score=max(0.05, min(1.0, parsed.confidence)),
                )
            ]
        if location is not None and verdict is not None:
            lon, lat = location
            # 坐标只作证据，不参与判据：它进 evidence_refs，不进任何一条阈值。
            hits = [
                *hits,
                TriggerHit(
                    rule_id="R-REPORT-LOC",
                    hazard_type=parsed.hazard_type.value,
                    region_code=region,
                    evidence_refs=[f"loc={round(lon, 6)},{round(lat, 6)}", f"reporter={reporter}"],
                    score=1.0,
                ),
            ]
        if verdict is not None and not verdict.hits:
            # 定级结论必须带上它自己的证据，否则状态面上的 `risk.evidence_refs` 是空的，
            # 事后没人能回答"这条等级凭什么"——R-REPORT-* 前缀就是"非阈值测量"的标记。
            verdict.hits = list(hits)
        result = await self.chain.process(
            [],
            region_code=region,
            preset_hits=hits,
            preset_verdict=verdict,
            intake="report",
        )
        intake_seconds = max(time.perf_counter() - started, 0.0)
        self.tracer.record(
            "report_intake_seconds",
            intake_seconds,
            trace_id=result.trace_id,
            outcome="ok" if result.ok else "degraded",
        )
        review = (
            await self._open_report_review(parsed=parsed, region=region, reporter=reporter, location=location, result=result)
            if parsed.needs_review
            else None
        )
        return {
            "parse": parsed.as_dict(),
            "human_review_required": parsed.needs_review,
            "review": review,
            "intake_seconds": round(intake_seconds, 3),
            "chain": result.as_dict(),
        }

    async def _open_report_review(
        self,
        *,
        parsed: ParsedDisaster,
        region: str,
        reporter: str,
        location: tuple[float, float] | None,
        result: ChainResult,
    ) -> dict[str, Any] | None:
        """低置信度上报开一张人工核签工单（架构文档 §6.2 场景二的后半段）。

        开单不拦发布：预警照发（漏报的代价高于误报，且判定过程已在 `degradations` 里留痕），
        核签判的是"这条结论是否按现状生效"。签完的结果进工作流实例台账，供阈值标定回看。
        工单在接入时延量测点**之后**才开：≤5min 这项判的是"上报到链路完成"，
        把等人签字的时间算进去就等于用人的节奏污染机器的口径。
        """
        definition = self.workflow.latest_definition(REPORT_REVIEW_TEMPLATE["name"])
        if definition is None or definition.status != "active":
            log.warning("核签流程未注册：低置信度上报只留下 human_review_required 标志")
            return None
        detail = await self.workflow.start(
            definition.workflow_id,
            trace_id=result.trace_id,
            payload={
                "region_code": region,
                "reporter": reporter,
                "note": parsed.text[:500],
                "hazard_type": parsed.hazard_type.value,
                "risk_level": None if parsed.risk_level is None else int(parsed.risk_level),
                "confidence": parsed.confidence,
                "decided_by": parsed.decided_by,
                "why_review": list(parsed.degradations),
                "warning_id": result.warning.warning_id if result.warning else None,
                "event_id": result.event_id,
                "location": None if location is None else [location[0], location[1]],
            },
        )
        self.report_stats["reviews_opened"] += 1
        return {
            "workflow_id": definition.workflow_id,
            "instance_id": detail.get("instance_id"),
            "status": detail.get("status"),
            "pending_node": next(
                (str(node.get("node_id")) for node in detail.get("nodes", []) if node.get("state") == "awaiting_human"),
                None,
            ),
            "options": ["approve", "adjust", "reject"],
            "decision_endpoint": "/api/v1/workflow/instances/{instance_id}/nodes/{node_id}/decision",
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
    delivery_client: object | None = None,
) -> PlatformContainer:
    """装配一个平台容器。

    `with_simulator=None`（默认）跟随配置面 `simulator_enabled`——那个旋钮在 .env.example 里
    本来就承诺过的语义；显式传 True/False 是测试与演练的覆盖口。
    `weather_client` 只给测试注入 httpx 传输用，生产留空。
    `outbound_client` 同理，注入的是工作流节点外呼（`api_call`/`device_control`）的传输；
    白名单为空时这条腿整体不接入节点，外呼会响亮失败而不是发出请求。
    `delivery_client` 是真实触达通道（`delivery_mode=http`）的传输注入口：测试注入假网关，
    生产留空由装配点自建并负责关闭。注入 `channels` 时整段自建逻辑跳过（不建也不关连接池）。
    """
    cfg = settings or get_settings()
    gateway_llm = llm_gateway if llm_gateway is not None else build_gateway_if_configured(cfg)
    bus = transport or build_transport(cfg)
    registry = AgentRegistry(cfg)
    contracts = ContractRegistry(contracts_dir or cfg.contracts_dir)
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
    # 注入 channels 时完全不建 HTTP 客户端：既不建就没有"容器关了但连接池还在"的尾巴。
    resolved_delivery_client, owns_delivery_client = (None, False) if channels else build_delivery_client(cfg, client=delivery_client)
    dispatcher = DeliveryDispatcher(
        channels or default_channels(cfg, client=resolved_delivery_client),
        tracer,
        sla_reach_seconds=cfg.sla_reach_seconds,
    )
    parser = DisasterTextParser(
        rule_engine=rule_engine,
        risk_engine=RiskEngine(rule_engine),
        evidence=AssembledEvidence(knowledge=knowledge, retrieval=retrieval, settings=cfg) if cfg.report_evidence_leg else None,
        llm=gateway_llm if cfg.report_llm_leg else None,
        review_confidence=cfg.report_review_confidence,
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
            # 案例驱动推演的注入位：知识腿没装配就留 None，节点侧显式降级而不是回退到 ±1 启发式
            scenario_provider=AssembledScenarios(knowledge=knowledge, settings=cfg) if knowledge is not None else None,
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
        rule_engine=rule_engine,
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
        parser=parser,
        delivery_client=resolved_delivery_client,
        _owns_delivery_client=owns_delivery_client,
    )

    # 进程启动即登记考核预算：违约判定不能等到第一次有人查指标才开始生效
    register_sla_budgets(tracer.ledger, cfg, container.exporter)

    if cfg.assistant_enabled:
        container.assistant = AssistantService(
            actions=_assistant_actions(
                container,
                store=store,
                registry=registry,
                gateway=gateway,
                knowledge=knowledge,
                simulator=simulator,
                ingest=ingest,
                settings=cfg,
            ),
            parser=parser,
            llm=gateway_llm,
            settings=cfg,
            tracer=tracer,
        )
    return container


def _assistant_actions(
    container: PlatformContainer,
    *,
    store: StoreProtocol,
    registry: AgentRegistry,
    gateway: AgentGateway,
    knowledge: KnowledgeProvider | None,
    simulator: HazardScenarioSimulator | None,
    ingest: IngestService,
    settings: Settings,
) -> AssistantActions:
    """助手动作到平台既有服务的桥——全部是薄翻译，没有第二条实现。

    读侧直接取内存读视图，演练复用 `IngestService`，上报复用 `PlatformContainer.submit_report`。
    所以助手嘴上说的等级、数字与大屏上的必然同源（架构铁律 4）。
    """

    async def list_warnings(*, limit: int = 10, region_code: str | None = None) -> list[dict[str, Any]]:
        rows = store.warnings.list(limit=int(limit), region_code=region_code or None)
        return [row.model_dump() for row in rows]

    async def get_warning(warning_id: str) -> dict[str, Any] | None:
        record = store.warnings.get(warning_id)
        return None if record is None else record.model_dump()

    async def list_chains(*, limit: int = 8) -> list[dict[str, Any]]:
        return [row.as_dict() for row in store.chains.latest(int(limit))]

    async def get_chain(trace_id: str) -> dict[str, Any] | None:
        wanted = str(trace_id)
        for row in store.chains.snapshot():
            if wanted in (row.trace_id, row.event_id):
                return row.as_dict()
        return None

    async def get_task(task_unit_id: str) -> dict[str, Any] | None:
        unit = store.tasks.get(task_unit_id)
        return None if unit is None else unit.model_dump()

    async def list_stations(*, region_code: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        rows = await store.list_stations(region_code=region_code or None, limit=int(limit))
        return [dict(row) for row in rows]

    async def list_agents() -> dict[str, Any]:
        return {"online": registry.online_count, "items": registry.snapshot(), "gateway_counters": dict(gateway.counters)}

    def latency_report() -> dict[str, Any]:
        return container.latency_report()

    async def recall_plan(
        *,
        query: str = "",
        hazard_type: str | None = None,
        region_code: str | None = None,
        limit: int = 3,
    ) -> list[dict[str, Any]]:
        if knowledge is None:
            return []
        matches = await knowledge.recall(
            query,
            hazard_type=hazard_type,
            region_code=region_code,
            limit=int(limit),
            budget_ms=settings.knowledge_recall_budget_ms,
        )
        return [match.planning_brief() for match in matches]

    async def run_drill(*, scenario: str = "surge", ticks: int = 2, region_code: str | None = None) -> dict[str, Any]:
        """对话里的演练入口：轮次上限压在 5，超出由 HTTP 层的 1..50 口径另行约束。"""
        if simulator is None:
            raise RuntimeError("模拟器未启用（AEGIS_SIMULATOR_ENABLED=false），演练腿不可用")
        simulator.scenario = "normal" if scenario == "normal" else "surge"
        rounds = [(await ingest.ingest_once()).as_dict() for _ in range(max(1, min(int(ticks), 5)))]
        grouped: dict[str, list[TelemetryReading]] = {}
        for reading in store.telemetry.query(region_code=region_code or None, limit=1_000):
            grouped.setdefault(reading.region_code, []).append(reading)
        results = await container.chain.process_many(grouped)
        return {"ingest": rounds, "regions": len(grouped), "chains": [row.as_dict() for row in results]}

    async def submit_report(
        *,
        note: str,
        region_code: str,
        reporter: str = "web-assistant",
        hazard_hint: str | None = None,
    ) -> dict[str, Any]:
        return await container.submit_report(note=note, region_code=region_code, reporter=reporter, hazard_hint=hazard_hint)

    return AssistantActions(
        list_warnings=list_warnings,
        get_warning=get_warning,
        list_chains=list_chains,
        get_chain=get_chain,
        get_task=get_task,
        list_stations=list_stations,
        list_agents=list_agents,
        latency_report=latency_report,
        recall_plan=recall_plan,
        run_drill=run_drill,
        submit_report=submit_report,
    )
