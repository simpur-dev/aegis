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

from aegis.agents.agentkit import AgentKit
from aegis.agents.mock import MockAgent, start_mock_agents
from aegis.agents.runtime import build_agent_runtime, resolve_context_face
from aegis.bus import subjects
from aegis.bus.gateway import AgentGateway
from aegis.bus.registry import AgentRegistry
from aegis.bus.transport import BusTransport
from aegis.config import Settings, get_settings
from aegis.connectors.base import DataSource, IngestService
from aegis.connectors.mqtt import MqttSource
from aegis.connectors.simulator import HazardScenarioSimulator
from aegis.connectors.weather_api import WeatherApiSource
from aegis.contracts import ContractRegistry
from aegis.domain.chain import ChainResult
from aegis.domain.enums import Channel
from aegis.domain.messages import AgentMessage, TelemetryReading
from aegis.integrations import (
    AnalyticsRecorder,
    AssembledEvidence,
    AssembledScenarios,
    IntegrationState,
    StoreBundle,
    build_analytics,
    build_delivery_client,
    build_knowledge,
    build_mcp,
    build_memory,
    build_mqtt,
    build_retrieval,
    build_store,
    build_transport,
    build_weather,
    close_knowledge,
    default_channels,
    sandbox_runner,
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
from aegis.pipeline.assess_input import AssessEvidenceBuilder
from aegis.pipeline.publisher import WarningPublisher
from aegis.pipeline.report_intake import ReportIntake
from aegis.pipeline.stages import HazardStages
from aegis.pipeline.workflow_chain import ChainStations, WorkflowChain
from aegis.services.alerting import AlertService
from aegis.services.alerting.notifier import Notifier
from aegis.services.alerting.webhook import NotificationWebhook, WebhookPolicy
from aegis.services.assistant import Action, ActionDomain, ActionRegistry, AssistantService
from aegis.services.assistant.actions.action_status import ActionStatus
from aegis.services.assistant.actions.control import DrillStart, ReportSubmit
from aegis.services.assistant.actions.device_control import DeviceControl
from aegis.services.assistant.actions.notify import NotifyGroup, NotifyLead
from aegis.services.assistant.actions.query import (
    AgentList,
    ChainExplain,
    ChainList,
    MetricReport,
    PlanAsk,
    StationList,
    TaskGet,
    TaskList,
    WarningExplain,
    WarningList,
)
from aegis.services.assistant.actions.rule_control import (
    RuleConfirmSuggestion,
    RuleRejectSuggestion,
    RuleUpdate,
)
from aegis.services.assistant.actions.task_control import (
    TaskAck,
    TaskAssign,
    TaskClaim,
    TaskClose,
    TaskTransfer,
)
from aegis.services.assistant.actions.warning_control import (
    WarningChangeLevel,
    WarningPublish,
    WarningResend,
    WarningRevoke,
    WarningSilence,
)
from aegis.services.assistant.actions.workflow_control import (
    WorkflowAbort,
    WorkflowDecide,
    WorkflowStart,
)
from aegis.services.collab.tasks import CollabService
from aegis.services.interpret.semantic_parser import DisasterTextParser
from aegis.services.llm.llm_gateway import LlmGateway, build_gateway_if_configured
from aegis.services.memory.label_flywheel import LabelFlywheel
from aegis.services.memory.memory import MemoryService
from aegis.services.operator_auth import OperatorAuthService
from aegis.services.operators import OperatorService
from aegis.services.response.delivery import ChannelAdapter, DeliveryDispatcher
from aegis.services.response.task_parser import TaskParser
from aegis.services.response.warning_service import WarningService
from aegis.services.rules.risk_engine import RiskEngine
from aegis.services.rules.threshold_suggestions import ThresholdSuggestionService
from aegis.services.rules.trigger_rules import RuleEngine, rulebook_from_rows
from aegis.storage.store import (
    PlatformStore,
    StoreProtocol,
    alert_ledger_port,
    control_action_port,
    handling_task_port,
    notification_port,
    operator_key_port,
    operator_ledger_port,
    rule_library_port,
)
from aegis.workflow.drafts import DraftService, LineageLocks
from aegis.workflow.engine import WorkflowEngine
from aegis.workflow.outbound import OutboundCaller, OutboundPolicy
from aegis.workflow.publishing import PublishingService
from aegis.workflow.services_bridge import build_workflow_services
from aegis.workflow.templates import (
    register_builtin_templates,
    register_report_review_template,
    register_standard_chain_template,
)

if TYPE_CHECKING:
    from aegis.retrieval.service import HybridRetrievalService

log = logging.getLogger("aegis.container")


@dataclass
class PlatformContainer:
    settings: Settings
    transport: BusTransport
    registry: AgentRegistry
    contracts: ContractRegistry
    tracer: Tracer
    gateway: AgentGateway
    store: StoreProtocol
    #: 五段链路入口（D7 §4.6 收口后唯一一条编排路径：标准五段模板 + `chain_stage` 节点）。
    #: 名字沿用 `chain` 是因为调用点要的是那个形状，不是因为它还归 Python 写顺序。
    chain: WorkflowChain
    ingest: IngestService
    dispatcher: DeliveryDispatcher
    exporter: MetricsExporter
    workflow: WorkflowEngine
    #: 草稿与发布共享同一条 lineage 锁：发布要先比对 revision/哈希再写版本，两件事必须串行。
    drafts: DraftService
    publishing: PublishingService
    bundle: StoreBundle
    rule_engine: RuleEngine
    #: 人工上报这条腿的入口（P4 起独立成 `pipeline/report_intake.py`）。
    #: 刻意做成必填字段并【不留 `container.submit_report` 委托方法】：两处都能调的入口
    #: 迟早会长出第二套口径，而"助手页上报"与"表单上报"必须同源。
    report_intake: ReportIntake
    #: 人工上报这条腿的账：受理数、阈值命中所得数、转人工核签数。
    #: 计数放在容器上而不是散在路由里，`latency_report()` 才能一次给全（指标必须可追溯到样本）。
    #: 同一个 dict 引用也交给 `report_intake` 去加，装配点不另建第二本账。
    report_stats: dict[str, int]
    #: 研判证据包的唯一构造面（§4.7）：研判段用它拼 `AssessRequest.evidence`，
    #: 证据读面用它回同样形状——两处若各拼一套，"推"和"拉"迟早给出不同的事实。
    assess_evidence: AssessEvidenceBuilder | None = None
    analytics: AnalyticsRecorder | None = None
    knowledge: KnowledgeProvider | None = None
    knowledge_state: IntegrationState | None = None
    retrieval: HybridRetrievalService | None = None
    retrieval_state: IntegrationState | None = None
    #: 共享记忆门面（§4.3）：assess 写研判、feedback 写教训、下一次同区域 assess 读回来。
    #: 它的 `backend` 决定这句话是"存进了库"还是"只活到这个进程结束"，所以必须对外可见。
    memory: MemoryService | None = None
    memory_state: IntegrationState | None = None
    #: MCP 工具层（§4.2）：server 与它的装配事实。没装可选腿/没开时 server=None 而状态行照出，
    #: 这样"平台有没有对外暴露工具面"是一个能查的事实，不是要人去翻依赖清单。
    #: `mcp_runtime` 是同一个装配的调用/失败台账引用：状态行每次从这里重读，
    #: 不然 detail 里那两个计数会永远停在装配时刻的 0（把"这一小时没人调"当成实测结论）。
    mcp_server: Any | None = None
    mcp_state: IntegrationState | None = None
    mcp_runtime: Any | None = None
    llm_gateway: LlmGateway | None = None
    mqtt: MqttSource | None = None
    mqtt_state: IntegrationState | None = None
    weather: WeatherApiSource | None = None
    weather_state: IntegrationState | None = None
    outbound: OutboundCaller | None = None
    simulator: HazardScenarioSimulator | None = None
    parser: DisasterTextParser | None = None
    assistant: AssistantService | None = None
    #: 席位台账：写动作的前置。内存形态没有这张表，服务自己报 unavailable（端点 503），不假装能写。
    operators: OperatorService | None = None
    #: 按人令牌台账（P4 强认证，设计 §14.1）：签发/校验/锁定。内存形态没有这张表，
    #: `available` 为假时端点如实 503——令牌重启即失等于"钥匙还有效但门锁忘了"，比没有钥匙更危险。
    operator_auth: OperatorAuthService | None = None
    #: 外部通知通道（P4，设计 §14.2）：白名单放行的 HTTP webhook。关着时通知只走站内。
    notification_webhook: NotificationWebhook | None = None
    #: 告警台账：订阅 `platform.alert.>` 的去重落账与处置面。内存/持久两套都实现，
    #: `persistent` 如实外露——内存形态重启即失。
    alerts: AlertService | None = None
    collab: CollabService | None = None
    notifier: Notifier | None = None
    #: 告警总线订阅句柄：停服时单独取消（不放进 `_tasks`，它不是循环任务）。
    _alert_sub: Any | None = None
    #: 数据飞轮（D9）：人工核签 → `warning_truth_labels` 的投递面；存储没有标注面时如实"未沉淀"
    labels: LabelFlywheel | None = None
    #: 阈值建议闭环（§3.5 后半截）：入账 → 人工确认 → `trigger_rules` 新版本 → 引擎装载。
    #: 装配点自建，所以它总在；`available` 为假时端点如实 503，不假装收下过建议。
    suggestions: ThresholdSuggestionService | None = None
    delivery_client: Any | None = None
    _owns_delivery_client: bool = False
    #: 规则库装配事实（批次 C2）：读不到 / 有坏行都要能在外侧看到，而不是默默用另一版阈值
    rulebook_error: str | None = None
    rulebook_rejected: list[str] = field(default_factory=list)
    mock_agents: list[MockAgent] = field(default_factory=list)
    real_agents: list[AgentKit] = field(default_factory=list)
    #: 智能体运行时的实际形态（含互斥压制的事实），启动日志与状态面都要能看见
    agent_runtime_state: str = ""
    #: 真智能体佐证取数当前走的门面（§4.2 阶段 2）：`mcp` / `rest` / "" = 还没起真运行时。
    #: 单独一个字段而不是塞进 `agent_runtime_state` 那句话：这一件事决定了"佐证从哪来"，会被单独问到。
    agent_context_face: str = ""
    _tasks: list[asyncio.Task[None]] = field(default_factory=list)
    _stop: asyncio.Event = field(default_factory=asyncio.Event)
    _started: bool = False
    _store_state: IntegrationState | None = None
    #: 工作流持久态恢复失败的原因（恢复了多少见 `workflow.instance_stats` 的 recovered_*）。
    workflow_recovery_error: str | None = None
    _retrieval_warm_error: str | None = None
    _knowledge_schema_error: str | None = None
    _knowledge_close_error: str | None = None
    _mqtt_start_error: str | None = None

    async def _backfill_read_model(self) -> None:
        """把最近 N 条预警/任务灌进读视图；失败只记日志，读写路径仍可走库查询。"""
        backfill = getattr(self.store, "backfill_read_model", None)
        if backfill is None:
            return
        try:
            report = await backfill(limit=self.settings.read_backfill_limit)
        except Exception as exc:
            log.warning("读视图回填失败，问答改走库查询", extra={"error": f"{type(exc).__name__}: {exc}"})
            return
        log.info("读视图回填完成", extra=report)

    async def _recover_control_actions(self) -> None:
        """启动时收尾上一次进程留下的中间态。

        `executing` 超过 10 分钟没更新 = 执行它的进程没了，结果未知：标记失败而不是让它永远卡着；
        `proposed` 过了有效期直接置 `expired`，否则用户点确认会得到一句语焉不详的冲突。
        失败只记日志：台账坏了不该阻断启动（读动作与其余平台功能照常）。
        """
        recover = getattr(self.store, "recover_stuck_actions", None)
        expire = getattr(self.store, "expire_actions", None)
        if recover is None and expire is None:
            return
        try:
            failed = 0 if recover is None else await recover()
            expired = 0 if expire is None else await expire()
        except Exception as exc:
            log.warning("动作台账恢复失败", extra={"error": f"{type(exc).__name__}: {exc}"})
            return
        if failed or expired:
            log.warning("动作台账启动收尾", extra={"failed": failed, "expired": expired})

    def _build_ext_agents(self) -> list[Any]:
        """构造外部交付模块（`agents_ext`）的智能体内环。

        返回空列表表示"外部实现此刻不可用"（可选 extra 没装 / 导入失败），
        由调用方如实退回自建内环——不让一个可选依赖决定整条链路能不能跑。
        """
        try:
            from aegis.agents_ext.adapter import build_ext_agents

            return build_ext_agents(self.transport)
        except Exception as exc:
            log.warning(
                "外部智能体实现不可用，退回自建内环",
                extra={"error": f"{type(exc).__name__}: {exc}"[:200]},
            )
            return []

    async def _start_alerting(self) -> None:
        """订阅既有预警广播，把"已发出的预警"变成"值班员桌上的卡"。

        这是**第二个订阅者**，与 `api/routers/stream.py` 的推帧互不依赖：
        推帧失败不影响落账，落账失败也不影响大屏。订阅不上只记一行事实、不阻断启动——
        此时前端 `GET /alerts` 仍可对账，只是没有实时卡片。
        """
        if self.alerts is None or not self.alerts.available:
            return
        try:
            self._alert_sub = await self.transport.subscribe("platform.alert.>", self._on_alert_message)
        except Exception as exc:
            log.warning(
                "告警订阅失败，实时卡片不可用（/alerts 仍可对账）",
                extra={"error": f"{type(exc).__name__}: {exc}"},
            )
            return
        self._tasks.append(asyncio.create_task(self._escalate_forever(), name="alert-escalate"))

    async def _on_alert_message(self, message: AgentMessage) -> None:
        if self.alerts is None:
            return
        try:
            record, created = await self.alerts.raise_from_broadcast(message.payload)
        except Exception as exc:
            log.warning(
                "告警落账失败",
                extra={"warning_id": message.payload.get("warning_id"), "error": f"{type(exc).__name__}: {exc}"},
            )
            return
        if created and record is not None:
            log.info(
                "告警已生成",
                extra={
                    "alert_id": record["alert_id"],
                    "warning_id": record["warning_id"],
                    "level": record["level"],
                    "drill": record["drill"],
                },
            )

    async def _escalate_forever(self) -> None:
        """超时未确认就升级。多副本靠 `UPDATE ... RETURNING` 天然幂等，不选主、不加锁。"""
        interval = max(5.0, float(self.settings.alert_escalation_interval_seconds))
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
                return
            except TimeoutError:
                pass
            if self.alerts is None:
                return
            try:
                escalated = await self.alerts.escalate_due()
            except Exception as exc:
                log.warning("告警升级扫描失败", extra={"error": f"{type(exc).__name__}: {exc}"})
                continue
            if escalated:
                log.warning(
                    "告警超时未确认，已升级",
                    extra={"count": len(escalated), "alert_ids": [row["alert_id"] for row in escalated]},
                )

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
        await self._recover_control_actions()
        await self._backfill_read_model()
        # 阈值版本在启动期就位：第一条预警用的必须是"当前生效那一版"，而不是代码里的种子
        await self._load_rulebook()
        # 定义与实例先接回内存，再注册内置模板：模板注册按名字幂等，
        # 反过来的话每次重启都会把持久化模板再追加一版。
        await self._restore_workflow_state()
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
        await self._start_alerting()
        self.gateway.set_result_handler(self.chain.handle_agent_message)
        await self.registry.start_sweeper()

        runtime = self.settings.agent_runtime
        if runtime == "real":
            # 真智能体与 mock 互斥：两副同挂 agent_in 会抢答同一批请求，"谁应答"变成随机数。
            # 取数门面（§4.2 阶段 2）：本进程装出工具面才谈得上走 MCP，`mcp_url` 就是这个判据本身。
            mcp_url = (
                f"http://127.0.0.1:{self.settings.http_port}{self.settings.mcp_mount_path.rstrip('/')}/"
                if self.mcp_server is not None
                else ""
            )
            self.agent_context_face = resolve_context_face(self.settings.agent_context_face, mcp_url=mcp_url)
            # 走哪一套实现（builtin/ext）：默认 builtin 不变；ext 是外部交付模块经适配层走同一契约。
            if self.settings.agent_impl == "ext":
                self.real_agents = self._build_ext_agents()
                if self.real_agents:
                    log.info(
                        "智能体实现走外部交付模块（agents_ext，经适配层走同一契约）",
                        extra={"impl": "ext"},
                    )
                else:
                    # 依赖没装 → 如实退回自建内链：不能让"没装可选 extra"变成整条链路不可用。
                    log.warning(
                        "AEGIS_AGENT_IMPL=ext 但外部实现不可用（依赖未安装或导入失败），退回自建内环",
                        extra={"impl": "builtin"},
                    )
                    self.real_agents = build_agent_runtime(
                        self.transport,
                        rule_engine=self.rule_engine,
                        base_url=f"http://127.0.0.1:{self.settings.http_port}",
                        llm=self.llm_gateway,
                        parser=self.parser,
                        contracts=self.contracts,
                        service_token=self.settings.service_token,
                        memory=self.memory,
                        mcp_url=mcp_url,
                        context_face=self.agent_context_face,
                    )
            else:
                self.real_agents = build_agent_runtime(
                    self.transport,
                    rule_engine=self.rule_engine,
                    base_url=f"http://127.0.0.1:{self.settings.http_port}",
                    llm=self.llm_gateway,
                    parser=self.parser,
                    contracts=self.contracts,
                    service_token=self.settings.service_token,
                    memory=self.memory,
                    mcp_url=mcp_url,
                    context_face=self.agent_context_face,
                )
            log.info(
                f"真智能体佐证取数走 {self.agent_context_face} 门面",
                extra={"agent_context_face": self.agent_context_face, "mcp_url": mcp_url or None},
            )
            for agent in self.real_agents:
                await agent.start(heartbeat_interval=0.5)
            self.agent_runtime_state = "real"
            if with_mock_agents:
                self.agent_runtime_state = "real（mock 按互斥压制，未启动）"
                log.info("agent_runtime=real：mock 替身不启动，一个订阅面只留一副", extra={"bus": self.transport.name})
        elif runtime == "mock" and with_mock_agents:
            self.mock_agents = await start_mock_agents(self.transport, heartbeat_interval=0.5)
            self.agent_runtime_state = "mock"
        else:
            self.agent_runtime_state = "off" if runtime == "off" else "mock（未请求启动）"

        if self.settings.env == "prod" and not self.real_agents and not self.mock_agents:
            # 生产默认不拉 mock，真运行时要显式开启；剩下的形态是"智能体全在进程外"。
            # 0 在线不是启动错误，但在外部接入完成前每条协同事务都会 no_agent 降级——
            # 这条路必须在启动日志里说破，而不是等第一条预警卡住时才由值班员反推。
            log.warning(
                "生产环境本进程未启动智能体，等待外部智能体注册；在此之前协同事务将按 no_agent 降级",
                extra={"agent_runtime": self.agent_runtime_state, "bus": self.transport.name},
            )

        await register_builtin_templates(self.workflow)
        # 核签流程与灾种剧本分开注册：低置信度上报要能开出一张真人可签的工单
        await register_report_review_template(self.workflow)
        # 标准五段模板：D7 退役 chain 的对照物。预算取配置里的 LLM 段事务预算，
        # 两边同源——节点超时若小于段预算，就会出现在链路还在等智能体时节点先被判死。
        await register_standard_chain_template(self.workflow, agent_txn_ms=int(self.settings.sla_agent_txn_ms))

        if with_ingest_loop and self.ingest.sources:
            period = ingest_interval_seconds or self.settings.simulator_interval_seconds

            async def _ingest_forever() -> None:
                await self.ingest.run_loop(self._stop, interval_seconds=period)

            self._tasks.append(asyncio.create_task(_ingest_forever(), name="ingest-loop"))
        log.info(
            "平台已启动",
            extra={
                "bus": self.transport.name,
                "mock_agents": len(self.mock_agents),
                "real_agents": len(self.real_agents),
                "agent_runtime": self.agent_runtime_state,
            },
        )

    async def _restore_workflow_state(self) -> None:
        """从持久层接回定义与未终态实例；失败只记事实，不阻断启动。

        内存后端没有可恢复的东西；`start_store` 已把连接失败写进 store 行，
        这里不重复报同一条故障。真连上了但读恢复失败（例如迁移没跑、快照坏）
        则留下 `workflow_recovery_error`，让它出现在 `/readyz` 的 chain 块里。
        """
        durability = self.bundle.durability
        if durability is None or not getattr(durability, "connected", False):
            return
        try:
            await self.workflow.restore_persisted_state()
        except Exception as exc:
            self.workflow_recovery_error = f"{type(exc).__name__}: {str(exc)[:300]}"
            log.warning("工作流持久态恢复失败，实例面按空账本启动", extra={"error": self.workflow_recovery_error})

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
        if self._alert_sub is not None:
            # 先退订再关总线：反过来会在已关闭的连接上调 cancel（NATS 会抛 ConnectionClosed）
            await self._alert_sub.cancel()
            self._alert_sub = None
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        for agent in self.mock_agents:
            await agent.stop()
        self.mock_agents = []
        for kit in self.real_agents:
            await kit.stop()
        self.real_agents = []
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
        if self.memory is not None:
            memory_state = self.memory_state or IntegrationState(name="memory", enabled=True, driver=self.memory.backend)
            memory_defects: dict[str, object] = {}
            if self.memory.stats["嵌入失败"]:
                # 嵌入腿在但算不出向量：这一档记忆从此只能按时间找，不能按语义找
                memory_defects["embedding_errors"] = self.memory.stats["嵌入失败"]
            if self.memory.evicted:
                # 进程内后端满了丢最旧：重启归零之前先归零了，这行必须读得到
                memory_defects["evicted"] = self.memory.evicted
            if memory_defects:
                memory_state = replace(memory_state, detail={**memory_state.detail, **memory_defects})
            rows.append(memory_state)
        # MCP 工具面这一行必须在"没开/没装"时也出现：外部要能分辨"平台没有工具面"与"有但没开"，
        # 而智能体取数走的是 MCP 还是 REST 兼容面，是验收时会被问到的事实。
        if self.mcp_state is None:
            mcp_state = IntegrationState(name="mcp", enabled=False, driver="disabled", detail={"reason": "装配点未提供 mcp 状态"})
        else:
            mcp_state = self.mcp_state
        # 取数门面当前走哪张（§4.2 阶段 2）：工具面在不在是一件事，智能体是否真的经它取数是另一件事。
        # 只看前一件会把"面开着但取数还在 REST 兼容面上"读成切换完成，所以这一格在"没开/没装"时也要出现。
        face_note = {"agent_context_face": self.agent_context_face or "未起真运行时"}
        if self.mcp_runtime is not None:
            # 调用数/失败数/成因是运行期事实，装配快照会把它们永远说成 0 与空串——与 mqtt 腿同一口径每次重读
            mcp_state = replace(mcp_state, detail={**mcp_state.detail, **self.mcp_runtime.as_dict(), **face_note})
        else:
            mcp_state = replace(mcp_state, detail={**mcp_state.detail, **face_note})
        rows.append(mcp_state)
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
            # 网关 v2 用量：LLM 成本/时延按 agent×model 归因的出口（§4.1.4/§4.8.3）。
            # 未配 LLM 时如实给 configured=False，而不是空表读成"零成本"。
            "llm": {
                "configured": self.llm_gateway is not None,
                **(self.llm_gateway.usage_snapshot() if self.llm_gateway is not None else {"by_agent_model": [], "total_calls": 0}),
            },
            "reports": dict(self.report_stats),
            # 触达口径必须自带身份：mock 通道的 1.2s 与真实网关的送达时间是两回事，
            # 分开报才不至于让"≤20min 触达"这项在演练数字上被读成达成（铁律 7）。
            "delivery": {"mode": s.delivery_mode, "channels": self.dispatcher.channel_status()},
            # 阈值版本的出处：指标 1"识别 ≥5 类触发条件"的证据要能落到具体版本与标定状态
            "rulebook": {**self.rule_engine.describe(), "error": self.rulebook_error, "rejected": self.rulebook_rejected[:5]},
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
    tracer = Tracer()
    gateway_llm = llm_gateway if llm_gateway is not None else build_gateway_if_configured(cfg, tracer=tracer)
    bus = transport or build_transport(cfg)
    registry = AgentRegistry(cfg)
    contracts = ContractRegistry(contracts_dir or cfg.contracts_dir)
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
    memory, memory_state = build_memory(store=bundle.store, retrieval=retrieval, knowledge=knowledge, settings=cfg)
    # MCP 工具层要在检索/知识/记忆都装配完之后再建：它只是把这些既有端口换个协议面暴露出去。
    # tracer 也要一起给：§4.2 那句"调用留 trace"靠的是账本，不是靠工具面的调用计数。
    mcp = build_mcp(
        settings=cfg,
        store=bundle.store,
        retrieval=retrieval,
        knowledge=knowledge,
        memory=memory,
        tracer=tracer,
    )
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
        # 处置空间/任务与读视图在同一落库点建，不靠定时任务补：链路一落地就能被认领。
        await collab.ensure_from_chain(result)
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
    # 协同处置台账：内存与持久两套同语义，跨重启差异如实外露。
    # 任务状态迁移广播走注入的回调（`services/` 不得 import aegis.bus），与站内通知同一取向。
    collab = CollabService(
        handling_task_port(store),
        persistent=not isinstance(store, PlatformStore),
        publish=gateway.publish_to,
        subject=subjects.ops("handling", "task"),
    )
    # 发布语义只有一份：主链路、工作流画布、助手动作共用同一个 Publisher。
    publisher = WarningPublisher(dispatcher=dispatcher, gateway=gateway)
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
    # 阈值建议闭环要在链路之前建好：反馈段拿它当入账口，端点拿它当唯一读写面。
    suggestions = ThresholdSuggestionService(store=store, rule_engine=rule_engine)
    # 证据包与 dev 环境同源：窗口取配置，存储不可达时 builder 自己降级成"只有 board 读数"。
    assess_evidence = AssessEvidenceBuilder(store, window_minutes=cfg.assess_window_minutes)
    # D7 §4.6 第 4 步：段的判定与记账在 `HazardStages`，段的先后由标准五段模板的边决定。
    # 这里刻意不再另建一个"固定顺序编排器"——两条路径并存的那段日子，parity 用例是唯一的护栏，
    # 而护栏不该是常驻成本（退役过程见 REPORT 的 D7 第 4 步一节）。
    stages = HazardStages(
        gateway=gateway,
        registry=registry,
        contracts=contracts,
        tracer=tracer,
        settings=cfg,
        rule_engine=rule_engine,
        risk_engine=RiskEngine(rule_engine),
        task_parser=TaskParser(contracts, settings=cfg),
        warning_service=warning_service,
        publisher=publisher,
        knowledge=knowledge,
        retrieval=retrieval,
        suggestions=suggestions,
        assess_evidence=assess_evidence,
    )
    # 段的执行口先于引擎：引擎的服务里要摆这个可调用，而它只依赖段与盘面，不依赖引擎本身。
    stations = ChainStations(stages)
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

    # 持久层实现延迟导入：内存后端部署不必把 asyncpg/pgvector 的依赖树拖进装配路径。
    from aegis.persistence.workflow_store import build_workflow_persistence

    workflow = WorkflowEngine(
        # 定义写穿 + 实例检查点：内存后端返回 None，引擎按纯内存运行（P1-5）。
        persistence=build_workflow_persistence(bundle.durability),
        services=build_workflow_services(
            store=store,
            rule_engine=rule_engine,
            risk_engine=RiskEngine(rule_engine),
            warning_service=warning_service,
            publisher=publisher,
            gateway=gateway,
            outbound=outbound,
            # 案例驱动推演的注入位：知识腿没装配就留 None，节点侧显式降级而不是回退到 ±1 启发式
            scenario_provider=AssembledScenarios(knowledge=knowledge, settings=cfg) if knowledge is not None else None,
            # §4.4：推演沙盒接进 DAG 节点。LLM 角色档由 `simulation_roles_llm` 决定，默认规则档。
            sandbox_runner=sandbox_runner(cfg, gateway_llm),
            run_chain_stage=stations,
        ),
        tracer=tracer,
        settings=cfg,
    )
    # 链路入口就是这张图。`chain` 这个名字与它的四个方法（process / process_many /
    # handle_agent_message / warnings）都不改——调用点（`/api/v1/drill/run`、人工上报腿、
    # 演练与报表脚本、总线订阅）要的只是这个形状；改的是"段的先后由谁决定"。
    chain = WorkflowChain(stages, workflow, stations=stations, on_result=on_result)
    lineage_locks = LineageLocks()
    drafts = DraftService(workflow.repository, locks=lineage_locks)
    publishing = PublishingService(
        workflow.repository,
        drafts,
        locks=lineage_locks,
        validate_definition=workflow.validate_definition,
    )
    # 人工上报这本账只有一个出处：容器读它出报表，受理腿写它记样本，两边拿的是同一个 dict。
    report_stats = {"submitted": 0, "measured_by_rule": 0, "review_required": 0, "reviews_opened": 0}
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
        drafts=drafts,
        publishing=publishing,
        bundle=bundle,
        rule_engine=rule_engine,
        report_intake=ReportIntake(parser=parser, chain=chain, tracer=tracer, workflow=workflow, report_stats=report_stats),
        report_stats=report_stats,
        assess_evidence=assess_evidence,
        analytics=analytics,
        knowledge=knowledge,
        knowledge_state=knowledge_state,
        retrieval=retrieval,
        retrieval_state=retrieval_state,
        memory=memory,
        memory_state=memory_state,
        mcp_server=mcp.server,
        mcp_state=mcp.state,
        mcp_runtime=mcp.runtime,
        mqtt=mqtt_source,
        mqtt_state=mqtt_state,
        weather=weather_source,
        weather_state=weather_state,
        outbound=outbound,
        labels=LabelFlywheel(store),
        suggestions=suggestions,
        simulator=simulator,
        parser=parser,
        llm_gateway=gateway_llm,
        delivery_client=resolved_delivery_client,
        _owns_delivery_client=owns_delivery_client,
    )

    # 进程启动即登记考核预算：违约判定不能等到第一次有人查指标才开始生效
    register_sla_budgets(tracer.ledger, cfg, container.exporter)

    container.operators = OperatorService(operator_ledger_port(store))
    container.collab = collab
    # P4 强认证：令牌台账与席位台账同一套"只有持久层有"的口径（内存形态端点 503，不假装能签发）。
    container.operator_auth = OperatorAuthService(
        operator_key_port(store),
        container.operators,
        enabled=cfg.operator_key_auth_enabled,
        max_failed_attempts=cfg.operator_key_max_failed_attempts,
        lock_minutes=cfg.operator_key_lock_minutes,
    )
    # P4 外部通知：白名单为空即整条通道关闭，通知照旧只走站内。
    container.notification_webhook = NotificationWebhook(WebhookPolicy.from_settings(cfg))
    notifier = Notifier(
        notification_port(store),
        publish=gateway.publish_to,
        subject=subjects.ops("assistant", "notify"),
        operators=container.operators,
        webhook=container.notification_webhook,
    )
    container.notifier = notifier

    if cfg.alerts_enabled:
        container.alerts = AlertService(
            alert_ledger_port(store),
            # 内存与持久两套台账语义一致，差别只有一条：重启后还在不在。这一点必须外露。
            persistent=not isinstance(store, PlatformStore),
            escalation_minutes=cfg.alert_escalation_minutes,
        )

    if cfg.assistant_enabled:
        container.assistant = AssistantService(
            registry=_build_assistant_registry(
                container,
                store=store,
                registry=registry,
                gateway=gateway,
                knowledge=knowledge,
                simulator=simulator,
                ingest=ingest,
                settings=cfg,
                publisher=publisher,
                collab=collab,
                notifier=notifier,
            ),
            llm=gateway_llm,
            settings=cfg,
            tracer=tracer,
            # 动作台账：写动作的"待确认 → 执行"落这里。内存形态也能跑，但重启即失（能力面如实标注）
            control_actions=control_action_port(store),
            control_actions_persistent=not isinstance(store, PlatformStore),
            # P4 二次认证（设计 §14.1.3）：同一席位再出示一次令牌。与双人复核正交。
            operator_auth=container.operator_auth,
        )
    return container


def _build_assistant_registry(
    container: PlatformContainer,
    *,
    store: StoreProtocol,
    registry: AgentRegistry,
    gateway: AgentGateway,
    knowledge: KnowledgeProvider | None,
    simulator: HazardScenarioSimulator | None,
    ingest: IngestService,
    settings: Settings,
    publisher: WarningPublisher,
    collab: CollabService,
    notifier: Notifier,
) -> ActionRegistry:
    """把平台既有服务装配成助手动作：每个动作只拿到它需要的那一个依赖。

    读侧直接取内存读视图，演练复用 `IngestService`，上报复用 `ReportIntake.submit`。
    所以助手嘴上说的等级、数字与大屏上的必然同源（架构铁律 4）。
    """

    async def recall_plan(query: str = "", hazard_type: str | None = None, limit: int = 3) -> list[dict[str, Any]]:
        if knowledge is None:
            return []
        matches = await knowledge.recall(
            query,
            hazard_type=hazard_type,
            limit=int(limit),
            budget_ms=settings.knowledge_recall_budget_ms,
        )
        return [match.planning_brief() for match in matches]

    async def run_drill(*, scenario: str = "surge", ticks: int = 2, region_code: str | None = None) -> dict[str, Any]:
        """对话里的演练入口：轮次上限压在 5；模拟器缺席由动作自己报不可用。"""
        if simulator is None:
            raise RuntimeError("模拟器未启用（AEGIS_SIMULATOR_ENABLED=false），演练腿不可用")
        simulator.scenario = "normal" if scenario == "normal" else "surge"
        rounds = [(await ingest.ingest_once()).as_dict() for _ in range(max(1, min(int(ticks), 5)))]
        grouped: dict[str, list[TelemetryReading]] = {}
        for reading in store.telemetry.query(region_code=region_code or None, limit=1_000):
            grouped.setdefault(reading.region_code, []).append(reading)
        results = await container.chain.process_many(grouped)
        return {"ingest": rounds, "regions": len(grouped), "chains": [row.as_dict() for row in results]}

    actions: list[Action] = [
        WarningList(store.warnings),
        WarningExplain(store.warnings, store.chains),
        ChainList(store.chains),
        ChainExplain(store.chains),
        TaskList(store.tasks),
        TaskGet(store.tasks),
        StationList(store),
        AgentList(registry, gateway),
        MetricReport(container.latency_report),
        PlanAsk(recall_plan if knowledge is not None else None),
        DrillStart(run_drill, available=simulator is not None),
        ReportSubmit(container.report_intake),
        WarningPublish(store.warnings, publisher),
        WarningRevoke(store.warnings, publisher),
        WarningChangeLevel(store.warnings, publisher),
        WarningSilence(store.warnings, publisher, container.alerts),
        WarningResend(store.warnings, publisher),
        TaskClaim(collab),
        TaskAck(collab),
        TaskAssign(collab),
        TaskTransfer(collab),
        TaskClose(collab),
        NotifyLead(notifier),
        NotifyGroup(notifier),
        ActionStatus(control_action_port(store)),
        WorkflowStart(container.workflow),
        WorkflowDecide(container.workflow),
        WorkflowAbort(container.workflow),
        RuleConfirmSuggestion(container.suggestions),
        RuleRejectSuggestion(container.suggestions),
        RuleUpdate(container.suggestions),
        DeviceControl(container.outbound),
    ]
    action_registry = ActionRegistry(
        actions,
        domains={
            ActionDomain.QUERY,
            ActionDomain.WARNING,
            ActionDomain.TASK,
            ActionDomain.DRILL,
            ActionDomain.NOTIFY,
            ActionDomain.WORKFLOW,
            ActionDomain.RULE,
            ActionDomain.DEVICE,
        },
    )
    action_registry.assert_consistent()
    return action_registry
