"""灾害响应五段的**唯一实现**：感知 → 研判 → 决策 → 执行 → 反馈。

架构决策（D7 编排归一 §4.6）：这里既有"段的判定"，也有"段的记账"（`stage_*_ms` 账本样本、
跨度属性、`StageResult`、降级留痕、D10 凭证转记）。谁按什么顺序调用这五段是编排器的职责，
而编排器现在只有一个：`pipeline/workflow_chain.py`（标准五段模板 + `chain_stage` 节点）。
退役前那份固定顺序实现调的也是**同一批方法**，所以"判定只有一份，账也只有一份"这件事
在退役前后都成立；分叉过一次的历史（触达段两处实现口径不一）正是 §4.6 列出的退役理由。

每一段都是"智能体优先、平台降级"：
- 智能体在线且契约合法 → 采纳其结论（mode=agent）；
- 智能体缺位/超时/产出不合契约 → 平台规则引擎与本地剧本兜底（mode=local），链路永不阻断。

这正是把智能体接入工作外包给队友的前提：平台不依赖任何智能体也能完成端到端预警，
真实智能体上线后按契约逐个替换降级路径（e2e 与 parity 用例覆盖两种形态）。
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

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
from aegis.observability import instrumentation
from aegis.observability.tracer import Tracer
from aegis.pipeline.board import ChainBoard
from aegis.pipeline.result import STAGE_ORDER, ChainResult, StageResult, model_receipt
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

#: 信封里的分歧格只有三种事实：False（两遍一致）、True（两遍不一致）、缺席（老版应答没带这一格）。
#: 把 None 折成 False 就是"没评估"冒充"评估过且一致"——§3.2 双盲最怕的正是这种读法。
_DISAGREEMENT_LABELS: dict[bool | None, str] = {True: "两遍不一致", False: "两遍一致", None: "分歧格未带"}


@dataclass(frozen=True, slots=True)
class _AssessOutcome:
    """研判段交回编排器的三件事：结论、谁答的、双盲走到哪一步。

    智能体在信封里带了 `double_blind` 与 `disagreement`（D10 的可选字段），此前链路只读结论：
    于是"双盲做了且分歧、由仲裁改判"与"根本没做双盲"在 `chain_runs` 里长同一个样，
    事后想查"这段时间模型自相矛盾的频率"就没有出处。这一段的事实必须由这一段落账。
    """

    verdict: RiskVerdict | None
    answered_by_agent: bool
    blind_status: str = ""
    disagreement: bool | None = None
    blind_mode: str = ""

    def note_suffix(self) -> str:
        """降级轨（本地定级）没有双盲这件事，不补半句；智能体那一程把状态串原样带上。

        `parallel` / `serial` 说的是这两遍是同时发的还是排队发的——它是这一段时延形状的事实，
        没有这格，"预触发没生效"与"预触发了但两腿一致"在台账上会长得一模一样。
        """
        if not self.answered_by_agent or not self.blind_status:
            return ""
        label = _DISAGREEMENT_LABELS[self.disagreement]
        head = f"双盲 {self.blind_mode}/{self.blind_status}" if self.blind_mode else f"双盲 {self.blind_status}"
        return f"；{head}（{label}）"


class SuggestionSink(Protocol):
    """反馈段把"待人工确认的阈值建议"交给谁——由装配点注入，内核不认识持久层。

    返回一句**状态**而不是布尔："没这张表"、"台账自己拒了"、"库里写不进去"是三件不同的事，
    压成一个 False 就等于把降级藏进返回值里（铁律 7：降级要看得见）。
    """

    async def record(
        self,
        *,
        trace_id: str,
        attribution: dict[str, Any],
        warning_id: str = "",
        region_code: str = "",
        hazard_type: str = "",
    ) -> str: ...


class HazardStages:
    """五段的执行体：每段自己写自己的账，交给编排器的就是那条 `StageResult`。"""

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
        suggestions: SuggestionSink | None = None,
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
        self._suggestions = suggestions

    @property
    def settings(self) -> Settings:
        return self._settings

    @property
    def tracer(self) -> Tracer:
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

    # ---------- 段的统一入口（编排器只有这一个调用方） ----------

    async def run_stage(self, name: str, board: ChainBoard) -> StageResult:
        """按段名跑一段并把 `StageResult` 记进盘面。

        链路的编排（DAG 的 `chain_stage` 节点经 `ChainStations`）只调这一个方法：
        "同一段在同一条链路上跑两次会不会长出两套账"这件事，因此结构上不可能发生。
        段名就是指标样本名的一部分（`stage_<name>_ms`），所以这里的名字与 `STAGE_ORDER` 一起构成契约。
        """
        result = board.result
        if name == "perceive":
            stage = await self.perceive(
                result, board.readings, board.region, board.trace, preset_hits=board.preset_hits, intake=board.intake
            )
        elif name == "assess":
            stage = await self.assess(
                result, result.hits, board.region, board.trace, preset_verdict=board.preset_verdict, reported=board.reported
            )
        elif name == "plan":
            stage = await self.plan(result, board.trace)
        elif name == "execute":
            stage = await self.execute(result, board.trace, started_at=board.started_at)
        elif name == "feedback":
            stage = await self.feedback(result, board.trace)
        else:
            raise ValueError(f"未知段名：{name}（可用：{', '.join(STAGE_ORDER)}）")
        result.stages.append(stage)
        return stage

    # ---------- ① 感知 ----------

    async def perceive(
        self,
        result: ChainResult,
        readings: list[TelemetryReading],
        region: str,
        trace: str,
        *,
        preset_hits: list[TriggerHit] | None = None,
        intake: str = "telemetry",
    ) -> StageResult:
        """命中（规则 ⊕ 智能体上报）写进 `result.hits`，返回感知段的账。"""
        reported = intake == "report"
        async with instrumentation.span("stage_perceive_ms", trace_id=trace) as span:
            with self._tracer.span("stage_perceive_ms", trace_id=trace) as sp:
                hits, agent_hit_count = await self._perceive(readings, region, trace, preset_hits=preset_hits)
            # 阶段结论写在账本计时之外：跨度不参与任何时延测量
            instrumentation.set_attributes(
                span,
                span_attrs={
                    "aegis.hits": len(hits),
                    "aegis.agent_hits": agent_hit_count,
                    "aegis.intake": intake,
                    "aegis.mode": "hybrid" if agent_hit_count else "local",
                },
            )
        result.hits = hits
        return StageResult(
            "perceive",
            "hybrid" if agent_hit_count else "local",
            True,
            sp.finished_ms or 0.0,
            ("上报文本判定：" if reported else "") + f"命中 {len(hits)} 条（智能体 {agent_hit_count} 条）",
        )

    async def _perceive(
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
        result: ChainResult,
        hits: list[TriggerHit],
        region: str,
        trace: str,
        *,
        preset_verdict: RiskVerdict | None = None,
        reported: bool = False,
    ) -> StageResult:
        """定级写进 `result.verdict`；None 就是研判未产出，编排器据此早退。"""
        before = len(result.degradations)
        async with instrumentation.span("stage_assess_ms", trace_id=trace) as span:
            with self._tracer.span("stage_assess_ms", trace_id=trace) as sp:
                outcome = await self._assess(hits, region, result, trace, preset_verdict=preset_verdict, reported=reported)
            verdict = outcome.verdict
            if verdict is None:
                instrumentation.mark_error(span, "研判未产出定级结论", code="assess_failed")
            elif len(result.degradations) > before:
                # 智能体缺位/超时/产出不合契约 → 平台规则引擎接管：降级，不是故障
                instrumentation.mark_degraded(span, result.degradations[-1])
            instrumentation.set_attributes(
                span,
                span_attrs={
                    "aegis.assessed_by": verdict.assessed_by if verdict else "",
                    "aegis.risk_level": int(verdict.risk_level) if verdict else 0,
                },
            )
        result.verdict = verdict
        return StageResult(
            "assess",
            "agent" if outcome.answered_by_agent else "local",
            verdict is not None,
            sp.finished_ms or 0.0,
            (f"等级 {int(verdict.risk_level)}" if verdict else "定级失败") + outcome.note_suffix(),
            model_info=result.stage_models.get("assess"),
        )

    async def _assess(
        self,
        hits: list[TriggerHit],
        region: str,
        result: ChainResult,
        trace: str,
        *,
        preset_verdict: RiskVerdict | None = None,
        reported: bool = False,
    ) -> _AssessOutcome:
        """结论与"这一段是谁答的、双盲走到哪一步"。降级路径的结论来自规则引擎或上报解析腿。"""
        local = preset_verdict or self._risk_engine.assess(hits, region_code=region)
        fallback_note = "采用上报解析定级" if preset_verdict is not None else "采用规则定级"
        if self._registry.pick(AgentType.ASSESS.value, "risk_assess") is None:
            return _AssessOutcome(local, False)
        request_payload: dict[str, Any] = {
            "region_code": region,
            "window": "1h",
            "candidate_hazards": sorted({h.hazard_type for h in hits}),
            "hits": [h.model_dump() for h in hits],
            "intake": "report" if reported else "telemetry",
        }
        # 规则轨的等级随请求交给智能体，只用作 §3.2 双盲的**预触发信号**（并行发两条腿要在看到
        # 模型结论之前就决定）；判定口径不变，结论的出处仍然只有模型那一份。规则轨没结论时
        # 干脆不送这一格——读不到就不预触发，比填一个数冒充"规则也说高危"诚实。
        if local is not None:
            request_payload["rule_level"] = int(local.risk_level)
        try:
            response = await self._gateway.dispatch(
                AgentType.ASSESS,
                Action.ASSESS_HAZARD,
                request_payload,
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
            return _AssessOutcome(local, False)

        if (verdict := self._parse_agent_assessment(response.payload, region, hits)) is not None:
            if (receipt := model_receipt(response)) is not None:
                result.stage_models["assess"] = receipt
            # 双盲是增强腿：它失败时主结论照用第一遍，但这件事必须落在台账上——
            # 不落的后果是"双盲没做成"被读成"双盲做成了且一致"（铁律 7：降级要看得见）。
            blind = str(response.payload.get("double_blind") or "")
            if blind.startswith("failed"):
                result.degradations.append(f"研判增强腿失败: {blind}（主结论取第一遍）")
            return _AssessOutcome(
                verdict,
                True,
                blind_status=blind,
                disagreement=response.disagreement,
                blind_mode=str(response.payload.get("double_blind_mode") or ""),
            )
        result.degradations.append(f"研判结论不合契约，{fallback_note}")
        return _AssessOutcome(local, False)

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
            # 缺席就是"没转人工"：这一格的语义是智能体自己有没有提出护栏，
            # 与 §3.2 那个 `disagreement` 的三态不同——那里"没评估"绝不能读成"评估过且一致"。
            human_review_suggested=bool(payload.get("human_review_suggested", False)),
        )

    # ---------- ③ 决策 ----------

    async def plan(self, result: ChainResult, trace: str) -> StageResult:
        verdict = result.verdict
        if verdict is None:
            raise ValueError("plan 段需要研判段的定级结论：编排器必须在研判早退后停手")
        before = len(result.degradations)
        async with instrumentation.span("stage_plan_ms", trace_id=trace) as span:
            with self._tracer.span("stage_plan_ms", trace_id=trace) as sp:
                units = await self._plan(verdict, result, trace)
            if not units:
                instrumentation.mark_error(span, "决策未产出任务单元", code="plan_empty")
            elif len(result.degradations) > before:
                instrumentation.mark_degraded(span, result.degradations[-1])
            instrumentation.set_attributes(span, span_attrs={"aegis.task_units": len(units)})
        result.task_units = units
        return StageResult(
            "plan",
            "agent" if any(u.created_by != "platform.task_parser" for u in units) else "local",
            bool(units),
            sp.finished_ms or 0.0,
            f"生成 {len(units)} 个任务单元",
            model_info=result.stage_models.get("plan"),
        )

    async def _plan(self, verdict: RiskVerdict, result: ChainResult, trace: str) -> list[StandardizedTaskUnit]:
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

    async def execute(self, result: ChainResult, trace: str, *, started_at: float) -> StageResult:
        """出文案、发预警、广播；产物写进 `result.warning`（None 表示发布失败）。

        `started_at` 必须是**链路起点**的 perf_counter 读数，由编排器传下来：
        `warning_generation_ms`（≤3min 那一项）量的就是"链路开始→预警就绪"，
        在生成步里现取会恒为 0。
        """
        verdict = result.verdict
        if verdict is None:
            raise ValueError("execute 段需要研判段的定级结论：编排器必须在研判早退后停手")
        before = len(result.degradations)
        async with instrumentation.span("stage_execute_ms", trace_id=trace) as span:
            with self._tracer.span("stage_execute_ms", trace_id=trace) as sp:
                warning, used_agent = await self._execute(verdict, result, trace, started_at=started_at)
            if warning is None:
                instrumentation.mark_error(span, "预警发布失败", code="execute_failed")
            elif len(result.degradations) > before:
                instrumentation.mark_degraded(span, result.degradations[-1])
            instrumentation.set_attributes(
                span, span_attrs={"aegis.warning_id": warning.warning_id if warning else "", "aegis.used_agent": used_agent}
            )
        result.warning = warning
        return StageResult(
            "execute",
            "agent" if used_agent else "local",
            warning is not None,
            sp.finished_ms or 0.0,
            warning.warning_id if warning else "预警发布失败",
            model_info=result.stage_models.get("execute"),
        )

    async def _execute(
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

    async def feedback(self, result: ChainResult, trace: str) -> StageResult:
        """回执汇总 → 派发给反馈智能体归因 → 状态进 ops 流。

        §3.5 的归因、阈值建议、教训写记忆都发生在 FeedbackAgent 内环里；这一段负责把它**叫起来**
        并把采纳结果记进台账。此前只广播不派发，于是智能体在册却收不到请求，那三件事在真栈上
        发生 0 次。
        """
        async with instrumentation.span("stage_feedback_ms", trace_id=trace) as span:
            with self._tracer.span("stage_feedback_ms", trace_id=trace) as sp:
                attribution, suggestion_ledger = await self._feedback(result.warning, trace, result)
            instrumentation.set_attributes(span, span_attrs={"aegis.reach_expected": result.warning is not None})
        return StageResult(
            "feedback",
            "agent" if attribution is not None else "local",
            True,
            sp.finished_ms or 0.0,
            # 归因结论要点进台账：`chain_runs.stages` 是事后唯一还能查到的地方
            self._feedback_note(attribution, suggestion_ledger),
            model_info=result.stage_models.get("feedback"),
        )

    @staticmethod
    def _feedback_note(attribution: dict[str, Any] | None, suggestion_ledger: str = "none") -> str:
        if attribution is None:
            return "触达回执已归档"
        head = (
            f"触达回执已归档｜效果分 {attribution['effect_score']:.3f}"
            f"｜归因腿 {attribution['decided_by']}｜建议 {attribution['suggestion_status']}"
        )
        if attribution["suggestion_status"] != "pending_human_review":
            return head
        # 建议"挂着待谁确认"必须能说出口：只在 ops 瞬时指标里过一遍，值班员下次看见它就没了
        return f"{head}｜建议入账 {suggestion_ledger}"

    async def _feedback(self, record: WarningRecord | None, trace: str, result: ChainResult) -> tuple[dict[str, Any] | None, str]:
        """归因结论 + 一句"这条建议入账了没有"。第二个返回值进段注，不进取证载荷。"""
        if record is None:
            return None, "none"
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
        return attribution, await self._record_suggestion(attribution, record, trace)

    async def _record_suggestion(self, attribution: dict[str, Any] | None, record: WarningRecord, trace: str) -> str:
        """把"待人工确认"的阈值建议交给台账；没这条腿、或台账自己不收，都要能说出是哪一种。

        放在发布事件**之后**：入账失败不许拖走已经归好因的 ops 事件——那条是 §3.5 的凭据，
        而这一笔只是让它可被消解。`attribution is None`（降级轨没有归因）也算"没有建议可入账"，
        不是错误。
        """
        if attribution is None or attribution["suggestion_status"] != "pending_human_review":
            return "none"
        if self._suggestions is None:
            return "skipped_no_sink"
        return await self._suggestions.record(
            trace_id=trace,
            attribution=attribution,
            warning_id=record.warning_id,
            region_code=record.region_codes[0] if record.region_codes else "",
            hazard_type=record.hazard_type,
        )

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
