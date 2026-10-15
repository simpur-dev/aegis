"""灾害响应五段的**唯一实现**：感知 → 研判 → 决策 → 执行 → 反馈。

架构决策（D7 编排归一 §4.6）：这里只有"段"，没有"顺序"。谁按什么顺序调用这五段是编排器的职责
——`pipeline/chain.py` 的固定顺序与 `workflow/` 的 DAG 节点都不得自带第二份 agent 优先/降级判定，
否则两处口径迟早分叉（触达段就分叉过一次，见 §4.6 的退役理由）。

每一段都是"智能体优先、平台降级"：
- 智能体在线且契约合法 → 采纳其结论（mode=agent）；
- 智能体缺位/超时/产出不合契约 → 平台规则引擎与本地剧本兜底（mode=local），链路永不阻断。

这正是把智能体接入工作外包给队友的前提：平台不依赖任何智能体也能完成端到端预警，
真实智能体上线后按契约逐个替换降级路径（e2e 与 parity 用例覆盖两种形态）。
"""

from __future__ import annotations

import logging
from collections import deque
from typing import TYPE_CHECKING, Any

from aegis.bus import subjects
from aegis.bus.gateway import AgentGateway, ContractRegistry
from aegis.bus.registry import AgentRegistry
from aegis.config import Settings, get_settings
from aegis.domain.enums import Action, AgentType, HazardType, RiskLevel
from aegis.domain.messages import (
    AgentMessage,
    StandardizedTaskUnit,
    TelemetryReading,
    TriggerHit,
    WarningRecord,
    make_event,
    now_iso,
)
from aegis.errors import AegisError
from aegis.knowledge.provider import CaseMatch, KnowledgeProvider
from aegis.observability.tracer import Tracer
from aegis.pipeline.result import ChainResult, model_receipt
from aegis.services.delivery import AllChannelsFailedError, DeliveryDispatcher
from aegis.services.risk_engine import RiskEngine, RiskVerdict
from aegis.services.task_parser import TaskParser
from aegis.services.text_guard import invented_numbers
from aegis.services.trigger_rules import RuleEngine
from aegis.services.warning_service import WarningService

if TYPE_CHECKING:
    # 只在类型面出现：段实现运行时不 import 检索层（装配层守住"内核不依赖持久/检索实现"这条边界）。
    from aegis.retrieval.service import HybridRetrievalService

log = logging.getLogger("aegis.pipeline")

# 参考案例条数：3 条足够支撑"凭什么这么判"的可解释性，再多会把规划 payload 撑成噪声
_CASE_RECALL_LIMIT = 3
# 注入的检索上下文块数：与案例召回分开计数，块越长下游研判读的越多但预算也吃得更紧
_CONTEXT_DOC_LIMIT = 5


class HazardStages:
    """五段的执行体。方法返回"这一段的产物"，把 mode/latency/note 的记账留给编排器——

    段的产物与段的记账分开，才能让固定顺序编排与 DAG 节点复用同一段实现而不会各记各的账。
    """

    def __init__(
        self,
        *,
        gateway: AgentGateway,
        registry: AgentRegistry,
        contracts: ContractRegistry,
        tracer: Tracer | None = None,
        settings: Settings | None = None,
        rule_engine: RuleEngine | None = None,
        risk_engine: RiskEngine | None = None,
        task_parser: TaskParser | None = None,
        warning_service: WarningService | None = None,
        dispatcher: DeliveryDispatcher | None = None,
        agent_hit_window: int = 512,
        knowledge: KnowledgeProvider | None = None,
        retrieval: HybridRetrievalService | None = None,
    ) -> None:
        self._gateway = gateway
        self._registry = registry
        self._contracts = contracts
        self._tracer = tracer or Tracer()
        self._settings = settings or get_settings()
        self._rule_engine = rule_engine or RuleEngine()
        self._risk_engine = risk_engine or RiskEngine(self._rule_engine)
        self._task_parser = task_parser or TaskParser(contracts, settings=self._settings)
        self._warning_service = warning_service or WarningService(self._tracer, settings=self._settings)
        self._dispatcher = dispatcher
        self._agent_hits: deque[TriggerHit] = deque(maxlen=agent_hit_window)
        self._warnings: dict[str, WarningRecord] = {}
        self._knowledge = knowledge
        self._retrieval = retrieval

    @property
    def settings(self) -> Settings:
        return self._settings

    @property
    def tracer(self) -> Tracer:
        """编排器共用同一只计时器：段的账本样本名由编排器写，时钟必须由段这边交出。"""
        return self._tracer

    @property
    def warnings(self) -> dict[str, WarningRecord]:
        return dict(self._warnings)

    @property
    def agent_txn_ms(self) -> int:
        """LLM 段事务的预算（§5 A 案）：与段预算同一档，不用两个数互相打脸。"""
        return int(self._settings.sla_agent_txn_ms)

    @property
    def agent_txn_ttl_ms(self) -> int:
        # TTL 必须宽于预算：默认 30s 的 TTL 会在投递前就把 60s 事务判成"超期丢弃"，
        # 等于让一个传输层默认值偷偷改小业务预算。留 5s 给排队与回程。
        return int(self._settings.sla_agent_txn_ms) + 5_000

    # ---------- 上行事件消费（网关 result handler） ----------

    async def handle_agent_message(self, message: AgentMessage) -> None:
        if message.action == Action.PERCEIVE_TRIGGER_HIT.value:
            try:
                self._agent_hits.append(TriggerHit.model_validate(message.payload))
            except Exception as exc:
                log.warning("智能体触发命中载荷非法", extra={"error": str(exc), "msg_id": message.msg_id})
        elif message.action == Action.FEEDBACK_STATUS.value:
            log.info("收到反馈", extra={"warning_id": message.payload.get("warning_id")})

    def _drain_agent_hits(self, region_code: str) -> list[TriggerHit]:
        picked = [h for h in self._agent_hits if h.region_code == region_code]
        for h in picked:
            self._agent_hits.remove(h)
        return picked

    # ---------- ① 感知 ----------

    async def perceive(
        self,
        readings: list[TelemetryReading],
        region: str,
        trace: str,
        *,
        preset_hits: list[TriggerHit] | None = None,
    ) -> tuple[list[TriggerHit], int]:
        # 上报腿的命中是 `services/semantic_parser` 用同一张规则表算出来的，这里不重算阈值。
        local_hits = list(preset_hits) if preset_hits is not None else self._rule_engine.evaluate(readings, region_code=region).hits
        agent_hits = self._drain_agent_hits(region)
        merged = list(local_hits)
        seen = {h.rule_id for h in merged}
        merged.extend(h for h in agent_hits if h.rule_id not in seen)
        for _hit in agent_hits:
            self._tracer.record("perceive_agent_hit_ms", 0.0, trace_id=trace, outcome="agent")
        return merged, len(agent_hits)

    # ---------- ② 研判 ----------

    async def assess(
        self,
        hits: list[TriggerHit],
        region: str,
        result: ChainResult,
        trace: str,
        *,
        preset_verdict: RiskVerdict | None = None,
        reported: bool = False,
    ) -> tuple[RiskVerdict | None, bool]:
        """返回 (结论, 是否由总线智能体应答)。降级路径的结论来自规则引擎或上报解析腿。"""
        local = preset_verdict or self._risk_engine.assess(hits, region_code=region)
        fallback_note = "采用上报解析定级" if preset_verdict is not None else "采用规则定级"
        if self._registry.pick(AgentType.ASSESS.value, "risk_assess") is None:
            return local, False
        try:
            response = await self._gateway.dispatch(
                AgentType.ASSESS,
                Action.ASSESS_HAZARD,
                {
                    "region_code": region,
                    "window": "1h",
                    "candidate_hazards": sorted({h.hazard_type for h in hits}),
                    "hits": [h.model_dump() for h in hits],
                    "intake": "report" if reported else "telemetry",
                },
                trace_id=trace,
                capability="risk_assess",
                # 事务预算 = LLM 档（§5 A 案）：与 `stage_*_ms` 同档同钟；
                # 不再用 `llm_timeout_seconds`（单次 HTTP 超时），也不蹭 ≤10s 调度档。
                deadline_ms=self.agent_txn_ms,
                ttl_ms=self.agent_txn_ttl_ms,
                priority=1,
            )
        except AegisError as exc:
            result.degradations.append(f"研判降级: {exc.message}")
            return local, False

        if (verdict := self._parse_agent_assessment(response.payload, region, hits)) is not None:
            if (receipt := model_receipt(response)) is not None:
                result.stage_models["assess"] = receipt
            # 双盲是增强腿：它失败时主结论照用第一遍，但这件事必须落在台账上——
            # 不落的后果是"双盲没做成"被读成"双盲做成了且一致"（铁律 7：降级要看得见）。
            blind = str(response.payload.get("double_blind") or "")
            if blind.startswith("failed"):
                result.degradations.append(f"研判增强腿失败: {blind}（主结论取第一遍）")
            return verdict, True
        result.degradations.append(f"研判结论不合契约，{fallback_note}")
        return local, False

    @staticmethod
    def _parse_agent_assessment(payload: dict[str, Any], region: str, hits: list[TriggerHit]) -> RiskVerdict | None:
        try:
            level = RiskLevel(int(payload["risk_level"]))
            hazard = HazardType(str(payload["hazard_type"]))
            confidence = float(payload["confidence"])
        except (KeyError, ValueError, TypeError):
            return None
        if not 0.0 <= confidence <= 1.0:
            return None
        return RiskVerdict(
            hazard_type=hazard,
            region_code=region,
            risk_level=level,
            confidence=confidence,
            rationale=str(payload.get("rationale", ""))[:1000] or "智能体结论",
            hits=hits,
            assessed_by=str(payload.get("assessed_by", "assess.agent")),
        )

    # ---------- ③ 决策 ----------

    async def plan(self, verdict: RiskVerdict, result: ChainResult, trace: str) -> list[StandardizedTaskUnit]:
        # 召回放在取本地剧本之前：无论最终由智能体还是本地剧本产出任务单元，
        # "参考过哪些历史案例"都是这条链路的证据，不该只在智能体在线时才存在。
        references = await self._recall_cases(verdict, result)
        local = self._task_parser.parse(verdict, event_id=result.event_id)
        if self._registry.pick(AgentType.PLAN.value, "task_decompose") is None:
            return local
        # 检索块只在智能体在场时取：本地剧本不消费正文块，为它付一次 CPU 推理不值。
        context = await self._retrieve_context(verdict, result)
        try:
            response = await self._gateway.dispatch(
                AgentType.PLAN,
                Action.PLAN_STU,
                {
                    "event_id": result.event_id,
                    "risk": verdict.as_payload(),
                    "objective": "按灾种处置剧本生成标准化任务单元",
                    "constraints": {"sla_warning_gen_seconds": self._settings.sla_warning_gen_seconds},
                    "reference_cases": references,
                    "retrieved_context": context,
                },
                trace_id=trace,
                capability="task_decompose",
                deadline_ms=self.agent_txn_ms,
                ttl_ms=self.agent_txn_ttl_ms,
                priority=1,
            )
        except AegisError as exc:
            result.degradations.append(f"决策降级: {exc.message}")
            return local

        raw_units = response.payload.get("task_units")
        if not isinstance(raw_units, list):
            result.degradations.append("决策产出不含 task_units，采用本地剧本")
            return local
        payloads = [u for u in raw_units if isinstance(u, dict)]
        if errors := self._contracts.stu_errors(payloads):
            result.degradations.append(f"STU 校验失败 {len(errors)} 项，采用本地剧本")
            return local
        try:
            units = [StandardizedTaskUnit.model_validate(p) for p in payloads]
        except Exception as exc:
            result.degradations.append(f"STU 解析失败: {exc}，采用本地剧本")
            return local
        if units and (receipt := model_receipt(response)) is not None:
            # 只有真采纳了智能体产的任务单元才记凭证：回落本地剧本那一程没有模型参与，
            # 记了就等于把本地结果算到某个模型头上。
            result.stage_models["plan"] = receipt
        return units or local

    async def _recall_cases(self, verdict: RiskVerdict, result: ChainResult) -> list[dict[str, Any]]:
        """规划前的案例召回：LLM 与图谱都不进这条路径的必需依赖，失败只记降级、绝不阻断预警。

        预算来自 knowledge_recall_budget_ms：宁可少给几条参考案例，也不能让一次知识检索
        把"≤3min 预警生成"的窗口吃掉。命中切片由 `CaseMatch.planning_brief()` 给出，
        与案例知识 API 共用同一份映射（段实现不自己抄一遍字段）。
        """
        if self._knowledge is None:
            return []
        query = self._planning_query(verdict)
        try:
            matches: list[CaseMatch] = await self._knowledge.recall(
                query,
                hazard_type=verdict.hazard_type.value,
                region_code=verdict.region_code,
                limit=_CASE_RECALL_LIMIT,
                budget_ms=self._settings.knowledge_recall_budget_ms,
            )
        except Exception as exc:  # 知识层是增强腿：任何异常都降级，不外抛到预警路径
            log.warning("案例召回降级", extra={"trace_id": result.trace_id, "err": type(exc).__name__})
            result.degradations.append(f"案例召回降级: {type(exc).__name__}")
            return []

        result.reference_cases = [match.case_id for match in matches]
        return [match.planning_brief() for match in matches]

    @staticmethod
    def _planning_query(verdict: RiskVerdict) -> str:
        """案例召回与检索上下文共用同一句查询串：两处各拼一遍迟早漂成两个口径。"""
        return f"{verdict.hazard_type.cn} {verdict.region_code} {verdict.rationale}"

    async def _retrieve_context(self, verdict: RiskVerdict, result: ChainResult) -> list[dict[str, Any]]:
        """规划前的混合检索上下文：dense + 词法 + RRF，LLM 一次都不进这条回路。

        与案例召回的分工写在字段上就清楚了：`reference_cases` 是结构化的历史处置方案，
        `context_docs` 是按语义排好序的上下文块（当前是 pgvector 上的历史任务单元 + 案例语料）。
        预算来自 retrieval_budget_ms；密集腿因缺少连接而被"跳过"时不记降级——那是装配状态，
        已由 `/api/v1/integrations` 报出，写进每一条预警只会淹没真正的故障。
        """
        if self._retrieval is None:
            return []
        from aegis.retrieval.service import RetrievalQuery  # 延迟导入：段实现不因此依赖检索层的 I/O 面

        try:
            outcome = await self._retrieval.retrieve(
                RetrievalQuery(
                    text=self._planning_query(verdict),
                    k=_CONTEXT_DOC_LIMIT,
                    trace_id=result.trace_id,
                    budget_ms=self._settings.retrieval_budget_ms,
                )
            )
        except Exception as exc:  # 检索是增强腿：任何异常都降级，不外抛到预警路径
            log.warning("检索上下文降级", extra={"trace_id": result.trace_id, "err": type(exc).__name__})
            result.degradations.append(f"检索上下文降级: {type(exc).__name__}")
            return []

        for item in outcome.degradations:
            if not item.is_skipped:
                result.degradations.append(f"检索腿降级: {item.leg}/{item.reason}"[:160])
        result.context_docs = [doc.doc_id for doc in outcome.docs]
        return [doc.as_reference() for doc in outcome.docs]

    # ---------- ④ 执行 ----------

    async def execute(
        self,
        verdict: RiskVerdict,
        result: ChainResult,
        trace: str,
        *,
        started_at: float | None = None,
    ) -> tuple[WarningRecord | None, bool]:
        """出文案与发预警是两件事：智能体只产文案，发送与回执永远属于平台交付腿。

        升级前这里是这么写的：注册了 execute 智能体，就把它 ack 里的 `channel_results`
        直接当触达证据（`_parse_ack`），没给就替每条通道造一条 `status="delivered"` 的回执。
        那等于"谁来答这一跳，谁就能自己宣布送达"——≤20min 触达指标与 `warning_reach_ms`
        会跟着一起变成编的。现在：智能体的自报通道结果一律不采信（只留一行可见事实），
        触达记录只能来自 `DeliveryDispatcher.dispatch()` 的真回执。
        """
        draft = await self._warning_service.generate(verdict, event_id=result.event_id, trace_id=trace, started_at=started_at)
        record = draft.record
        used_agent = False
        if self._registry.pick(AgentType.EXECUTE.value, "warn_publish") is not None:
            try:
                response = await self._gateway.dispatch(
                    AgentType.EXECUTE,
                    Action.EXECUTE_WARN,
                    {
                        "warning_id": record.warning_id,
                        "level": int(record.risk_level),
                        "regions": record.region_codes,
                        "audiences": record.audiences,
                        "content": {"zh": record.body_zh, "bo": record.body_bo},
                        "channels": record.channels,
                        # 文案里的数字必须有出处可查：把预警事实的引用交给智能体，
                        # 回收时再拿同一批事实做防幻觉闸（§3.4 护栏）。
                        "content_refs": self._copy_facts(record, verdict),
                    },
                    trace_id=trace,
                    capability="warn_publish",
                    deadline_ms=self.agent_txn_ms,
                    ttl_ms=self.agent_txn_ttl_ms,
                    priority=1,
                )
                used_agent = True
                pre_adopt = record
                record = self._adopt_agent_copy(record, response.payload, result, verdict=verdict)
                if record != pre_adopt and (receipt := model_receipt(response)) is not None:
                    # 只有智能体写的文字真的进了这条预警才记凭证：文案全被数字闸拦回、保留平台模板的那一程，
                    # 记上模型名字等于把模板产出的东西算到某个模型头上（与规划段同一条判据）。
                    result.stage_models["execute"] = receipt
            except AegisError as exc:
                result.degradations.append(f"执行智能体失败，文案回落平台模板: {exc.message}")
        dispatched = await self._dispatch(record, result)
        if dispatched is None:
            return None, used_agent
        await self._broadcast(dispatched, trace)
        self._warnings[dispatched.warning_id] = dispatched
        return dispatched, used_agent

    @staticmethod
    def _copy_facts(record: WarningRecord, verdict: RiskVerdict) -> str:
        """给文案防幻觉闸用的事实池：平台已产出的预警正文 + 定级依据与证据引用。

        只做拼接、不改数值——数字口径的唯一真源仍是 record 与 verdict，智能体不能往里加东西。
        """
        refs = [f"{hit.rule_id}={hit.score}" for hit in verdict.hits]
        return " ".join(
            [
                record.title_zh,
                record.body_zh,
                record.body_bo or "",
                " ".join(record.region_codes),
                str(int(record.risk_level)),
                verdict.rationale,
                f"confidence={verdict.confidence}",
                *refs,
            ]
        )

    @staticmethod
    def _adopt_agent_copy(record: WarningRecord, payload: dict[str, Any], result: ChainResult, *, verdict: RiskVerdict) -> WarningRecord:
        """采纳智能体的文案，但采纳的是【文字】不是送达：数字必须都在事实池里，否则整段弃用。"""
        claims = payload.get("channel_results")
        if isinstance(claims, list) and claims:
            result.degradations.append(f"执行智能体自报 {len(claims)} 条通道结果未采信（无 dispatcher 凭证）：触达证据只认平台交付腿回执")
        facts = HazardStages._copy_facts(record, verdict)
        update: dict[str, Any] = {}
        for slot in ("title_zh", "body_zh", "body_bo"):
            value = payload.get(slot)
            if not isinstance(value, str) or not value.strip():
                continue
            invented = sorted(invented_numbers(value, facts))
            if invented:
                result.degradations.append(f"执行智能体文案含预警事实之外的数字 {invented}，该字段弃用，保留平台模板")
                continue
            update[slot] = value
        copies = payload.get("audience_copies")
        if isinstance(copies, dict):
            kept: dict[str, str] = {}
            dropped: list[str] = []
            for role, text in copies.items():
                if not isinstance(role, str) or not isinstance(text, str) or not text.strip():
                    continue
                bad = sorted(invented_numbers(text, facts))
                if bad:
                    dropped.append(role)
                else:
                    kept[role.strip()] = text.strip()
            if dropped:
                result.degradations.append(f"执行智能体受众分支 {sorted(dropped)} 含预警事实之外的数字，不入库")
            if kept:
                update["audience_copies"] = kept
        if not update:
            return record
        adopted = record.model_copy(update=update)
        result.degradations.append("预警文案取自执行智能体（送达记录仍由平台交付腿产生）")
        return adopted

    async def _dispatch(self, record: WarningRecord, result: ChainResult) -> WarningRecord | None:
        if self._dispatcher is None:
            record.released_at = now_iso()
            return record
        try:
            return await self._dispatcher.dispatch(record)
        except AllChannelsFailedError as exc:
            result.errors.append(f"全部通道失败: {exc.message}")
            return None

    async def _broadcast(self, record: WarningRecord, trace: str) -> None:
        event = make_event(
            source="platform.pipeline",
            action=Action.EXECUTE_ACK.value,
            trace_id=trace,
            target=f"platform.broadcast_{int(record.risk_level)}",
            payload={
                "warning_id": record.warning_id,
                "risk_level": int(record.risk_level),
                "regions": record.region_codes,
                "title": record.title_zh,
            },
        )
        await self._gateway.publish_to(subjects.alert(int(record.risk_level), record.region_codes[0]), event)

    # ---------- ⑤ 反馈 ----------

    async def feedback(self, record: WarningRecord | None, trace: str, result: ChainResult) -> dict[str, Any] | None:
        """回执汇总 → 派发给反馈智能体归因 → 状态进 ops 流。返回被采纳的归因（未采纳给 `None`）。

        §3.5 的归因、阈值建议、教训写记忆都发生在 FeedbackAgent 内环里。此前这一段只广播不派发，
        于是智能体在册却永远等不到请求，那三件事在真栈上发生 0 次——本方法补上派发与消费。
        """
        if record is None:
            return None
        deliveries = record.deliveries
        delivered = sum(1 for d in deliveries if d.status in ("delivered", "retried"))
        failed = sum(1 for d in deliveries if d.status == "failed")
        reach_stats = {
            "channels": len(deliveries),
            "total": len(deliveries),
            "delivered": delivered,
            "failed": failed,
            "pending": max(0, len(deliveries) - delivered - failed),
            "reach_seconds": record.reach_seconds(),
        }
        response_state = "pending" if delivered else "unknown"
        attribution: dict[str, Any] | None = None
        if self._registry.pick(AgentType.FEEDBACK.value, "reach_track") is not None:
            try:
                response = await self._gateway.dispatch(
                    AgentType.FEEDBACK,
                    Action.FEEDBACK_ATTRIBUTE,
                    {
                        "warning_id": record.warning_id,
                        "region_code": record.region_codes[0] if record.region_codes else "",
                        "reach_stats": reach_stats,
                        "response_state": response_state,
                    },
                    trace_id=trace,
                    capability="reach_track",
                    deadline_ms=self.agent_txn_ms,
                    ttl_ms=self.agent_txn_ttl_ms,
                )
            except AegisError as exc:
                # 反馈段排在预警已发布之后：它失败只能塌自己这一腿，不许把已经发出去的预警拖回降级
                result.degradations.append(f"反馈归因降级: {exc.message}")
            else:
                parsed = self._parse_agent_feedback(response.payload)
                if parsed is None:
                    result.degradations.append("反馈结论不合契约，按平台回执归档")
                else:
                    attribution = parsed
                    # 凭证只跟着真正由模型产出的归因走：`decided_by=local` 时那段文字是规则摘要，
                    # 记到模型头上与"规划回落本地剧本不记凭证"是同一条判据。
                    if parsed["decided_by"] == "agent" and (receipt := model_receipt(response)) is not None:
                        result.stage_models["feedback"] = receipt
        payload: dict[str, Any] = {
            "warning_id": record.warning_id,
            "reach_stats": reach_stats,
            "response_state": response_state,
            "observed_at": now_iso(),
        }
        if attribution is not None:
            payload.update(attribution)
        event = make_event(
            source="platform.feedback",
            action=Action.FEEDBACK_STATUS.value,
            trace_id=trace,
            target="platform.feedback_sink",
            payload=payload,
        )
        await self._gateway.publish_to(subjects.ops("feedback", "status"), event)
        return attribution

    @staticmethod
    def _parse_agent_feedback(payload: dict[str, Any]) -> dict[str, Any] | None:
        """只收契约内的字段；效果分不是 0—1 的数就整份弃用（宁可没有归因，不要编一个）。"""
        try:
            score = float(payload["effect_score"])
            text = str(payload["attribution"])
        except (KeyError, TypeError, ValueError):
            return None
        if not 0.0 <= score <= 1.0 or not text:
            return None
        return {
            "effect_score": round(score, 3),
            "attribution": text[:500],
            "unreached_audiences": [str(item) for item in (payload.get("unreached_audiences") or [])][:16],
            "lag_stage": str(payload.get("lag_stage", ""))[:120],
            "threshold_suggestion": str(payload.get("threshold_suggestion", ""))[:500],
            "suggestion_status": str(payload.get("suggestion_status", "none")),
            "decided_by": str(payload.get("decided_by", "local")),
        }
