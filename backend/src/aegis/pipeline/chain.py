"""灾害响应链路：感知 → 研判 → 决策 → 执行 → 反馈 的平台侧编排器（M1 固定流水线）。

架构决策：每一段都是"智能体优先、平台降级"。
- 智能体在线且契约合法 → 采纳其结论（mode=agent）；
- 智能体缺位/超时/产出不合契约 → 平台规则引擎与本地剧本兜底（mode=local），链路永不阻断。

这正是把智能体接入工作外包给队友的前提：平台不依赖任何智能体也能完成端到端预警，
真实智能体上线后按契约逐个替换降级路径（`scripts/demo_e2e.py` 与一致性测试覆盖两种形态）。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
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
    new_event_id,
    new_trace_id,
    now_iso,
)
from aegis.errors import AegisError
from aegis.knowledge.provider import CaseMatch, KnowledgeProvider
from aegis.observability import instrumentation
from aegis.observability.tracer import Tracer
from aegis.services.delivery import AllChannelsFailedError, DeliveryDispatcher
from aegis.services.risk_engine import RiskEngine, RiskVerdict
from aegis.services.task_parser import TaskParser
from aegis.services.text_guard import invented_numbers
from aegis.services.trigger_rules import RuleEngine
from aegis.services.warning_service import WarningService

if TYPE_CHECKING:
    # 只在类型面出现：链路运行时不 import 检索层（装配层守住"内核不依赖持久/检索实现"这条边界）。
    from aegis.retrieval.service import HybridRetrievalService

log = logging.getLogger("aegis.pipeline")

_STAGE_ORDER = ("perceive", "assess", "plan", "execute", "feedback")
# 参考案例条数：3 条足够支撑"凭什么这么判"的可解释性，再多会把规划 payload 撑成噪声
_CASE_RECALL_LIMIT = 3
# 注入的检索上下文块数：与案例召回分开计数，块越长下游研判读的越多但预算也吃得更紧
_CONTEXT_DOC_LIMIT = 5


@dataclass(slots=True)
class StageResult:
    name: str
    mode: str  # agent | local | hybrid | skipped
    ok: bool
    latency_ms: float
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "mode": self.mode, "ok": self.ok, "latency_ms": round(self.latency_ms, 3), "note": self.note}


@dataclass(slots=True)
class ChainResult:
    trace_id: str
    event_id: str
    stages: list[StageResult] = field(default_factory=list)
    hits: list[TriggerHit] = field(default_factory=list)
    verdict: RiskVerdict | None = None
    task_units: list[StandardizedTaskUnit] = field(default_factory=list)
    warning: WarningRecord | None = None
    errors: list[str] = field(default_factory=list)
    degradations: list[str] = field(default_factory=list)
    # 本次规划参考过的历史案例 id：可解释性证据，不是"提示词装饰"——报告里要能回答"凭什么这么判"。
    reference_cases: list[str] = field(default_factory=list)
    # 本次规划注入的检索上下文块 id：与案例召回分开记账，因为两者的语料面不同（案例库 vs 历史任务单元）
    context_docs: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """链路成功 = 各段执行成功且无降级错误。无触发时同样成功（未告警是正确结果）。"""
        return all(s.ok for s in self.stages) and not self.errors

    @property
    def acted(self) -> bool:
        return self.verdict is not None

    def stage(self, name: str) -> StageResult | None:
        return next((s for s in self.stages if s.name == name), None)

    def as_dict(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "event_id": self.event_id,
            "ok": self.ok,
            "acted": self.acted,
            "stages": [s.as_dict() for s in self.stages],
            "risk": self.verdict.as_payload() if self.verdict else None,
            "task_units": [u.task_unit_id for u in self.task_units],
            "warning_id": self.warning.warning_id if self.warning else None,
            "errors": self.errors,
            "degradations": self.degradations,
            "reference_cases": self.reference_cases,
            "context_docs": self.context_docs,
        }


class HazardResponseChain:
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
        on_result: Callable[[ChainResult], Awaitable[None]] | None = None,
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
        self._on_result = on_result
        self._knowledge = knowledge
        self._retrieval = retrieval

    async def _finish(self, result: ChainResult) -> ChainResult:
        if self._on_result is not None:
            await self._on_result(result)
        return result

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

    # ---------- 主链路 ----------

    async def process(
        self,
        readings: list[TelemetryReading],
        *,
        trace_id: str | None = None,
        event_id: str | None = None,
        region_code: str | None = None,
        preset_hits: list[TriggerHit] | None = None,
        preset_verdict: RiskVerdict | None = None,
        intake: str = "telemetry",
    ) -> ChainResult:
        """一次灾害事件（感知→研判→决策→执行→反馈）= 一条可第三方引用的链路。

        `preset_hits`/`preset_verdict` 是人工上报腿的入口（架构文档 §6.2 场景二）：灾情文本经三路
        融合解析后已经产出触发命中与定级建议，链路从感知段之后**照常走同一条**——同一套量测、
        同一条 trace、同一份降级留痕，而不是平行实现第二条预警链。`intake` 只改感知段的说明文字。

        根跨度 `hazard_chain` 只承载链路结构（父子关系、降级/故障状态），不另立计时口径：
        各段时延仍由 `_run_chain` 里的 `self._tracer.span(...)` 落账，SLA 判定继续以时延账本为准。
        根跨度在活动上下文里激活，因此其中的阶段跨度与网关协同事务跨度自动成为它的子节点，
        整棵树的 traceID 由契约 trace_id 确定性映射而来（见 observability/instrumentation.py）。
        """
        trace = trace_id or new_trace_id()
        async with instrumentation.span(
            "hazard_chain",
            trace_id=trace,
            kind="server",
            span_attrs={
                "aegis.region_code": region_code or (readings[0].region_code if readings else "UNKNOWN"),
                "aegis.readings": len(readings),
                "aegis.intake": intake,
            },
        ) as root:
            result = await self._run_chain(
                readings,
                trace_id=trace,
                event_id=event_id,
                region_code=region_code,
                preset_hits=preset_hits,
                preset_verdict=preset_verdict,
                intake=intake,
            )
            # 事件 ID 在链路内部才生成，回填到根跨度上，取证时可用 event_id 或 trace_id 双向检索
            instrumentation.set_attributes(
                root,
                span_attrs={
                    "aegis.event_id": result.event_id,
                    "aegis.chain.ok": result.ok,
                    "aegis.chain.stages": len(result.stages),
                    "aegis.chain.degradations": len(result.degradations),
                    "aegis.chain.errors": len(result.errors),
                },
            )
            if result.errors:
                instrumentation.mark_error(root, "；".join(result.errors)[:500], code="chain_errors")
            elif result.degradations:
                instrumentation.mark_degraded(root, "；".join(result.degradations)[:500])
            return result

    async def _run_chain(
        self,
        readings: list[TelemetryReading],
        *,
        trace_id: str | None = None,
        event_id: str | None = None,
        region_code: str | None = None,
        preset_hits: list[TriggerHit] | None = None,
        preset_verdict: RiskVerdict | None = None,
        intake: str = "telemetry",
    ) -> ChainResult:
        trace = trace_id or new_trace_id()
        result = ChainResult(trace_id=trace, event_id=event_id or new_event_id())
        region = region_code or (readings[0].region_code if readings else "UNKNOWN")
        reported = intake == "report"
        #: 链路起点。执行段生成预警时按"链路开始→产物就绪"量 ≤3min，
        #: 所以这个数必须从这里传下去，而不是在生成步里现取（那样恒为 0）。
        #: 用 perf_counter 而不是 monotonic：后者在 Windows 上步进 15.6 ms，
        #: 毫秒级的段会把 0.3 ms 量成 0，而 `warning_generation_ms` 的判定就建在这个数上。
        started_at = time.perf_counter()

        async with instrumentation.span("stage_perceive_ms", trace_id=trace) as perceive_span:
            with self._tracer.span("stage_perceive_ms", trace_id=trace) as sp:
                hits, agent_hit_count = await self._perceive(readings, region, trace, preset_hits=preset_hits)
            # 阶段结论写在账本计时之外：跨度不参与任何时延测量
            instrumentation.set_attributes(
                perceive_span,
                span_attrs={
                    "aegis.hits": len(hits),
                    "aegis.agent_hits": agent_hit_count,
                    "aegis.intake": intake,
                    "aegis.mode": "hybrid" if agent_hit_count else "local",
                },
            )
        result.stages.append(
            StageResult(
                "perceive",
                "hybrid" if agent_hit_count else "local",
                True,
                sp.finished_ms or 0.0,
                ("上报文本判定：" if reported else "") + f"命中 {len(hits)} 条（智能体 {agent_hit_count} 条）",
            )
        )
        result.hits = hits

        if not hits:
            result.stages.extend([StageResult(n, "skipped", True, 0.0, "无触发条件命中") for n in _STAGE_ORDER[1:]])
            return await self._finish(result)

        assess_degradations = len(result.degradations)
        async with instrumentation.span("stage_assess_ms", trace_id=trace) as assess_span:
            with self._tracer.span("stage_assess_ms", trace_id=trace) as sp:
                verdict, answered_by_agent = await self._assess(
                    hits, region, result, trace, preset_verdict=preset_verdict, reported=reported
                )
            if verdict is None:
                instrumentation.mark_error(assess_span, "研判未产出定级结论", code="assess_failed")
            elif len(result.degradations) > assess_degradations:
                # 智能体缺位/超时/产出不合契约 → 平台规则引擎接管：降级，不是故障
                instrumentation.mark_degraded(assess_span, result.degradations[-1])
            instrumentation.set_attributes(
                assess_span,
                span_attrs={
                    "aegis.assessed_by": verdict.assessed_by if verdict else "",
                    "aegis.risk_level": int(verdict.risk_level) if verdict else 0,
                },
            )
        result.stages.append(
            StageResult(
                "assess",
                "agent" if answered_by_agent else "local",
                verdict is not None,
                sp.finished_ms or 0.0,
                f"等级 {int(verdict.risk_level)}" if verdict else "定级失败",
            )
        )
        if verdict is None:
            return await self._finish(result)
        result.verdict = verdict

        plan_degradations = len(result.degradations)
        async with instrumentation.span("stage_plan_ms", trace_id=trace) as plan_span:
            with self._tracer.span("stage_plan_ms", trace_id=trace) as sp:
                units = await self._plan(verdict, result, trace)
            if not units:
                instrumentation.mark_error(plan_span, "决策未产出任务单元", code="plan_empty")
            elif len(result.degradations) > plan_degradations:
                instrumentation.mark_degraded(plan_span, result.degradations[-1])
            instrumentation.set_attributes(plan_span, span_attrs={"aegis.task_units": len(units)})
        result.stages.append(
            StageResult(
                "plan",
                "agent" if any(u.created_by != "platform.task_parser" for u in units) else "local",
                bool(units),
                sp.finished_ms or 0.0,
                f"生成 {len(units)} 个任务单元",
            )
        )
        result.task_units = units

        execute_degradations = len(result.degradations)
        async with instrumentation.span("stage_execute_ms", trace_id=trace) as execute_span:
            with self._tracer.span("stage_execute_ms", trace_id=trace) as sp:
                warning, used_agent = await self._execute(verdict, result, trace, started_at=started_at)
            if warning is None:
                instrumentation.mark_error(execute_span, "预警发布失败", code="execute_failed")
            elif len(result.degradations) > execute_degradations:
                instrumentation.mark_degraded(execute_span, result.degradations[-1])
            instrumentation.set_attributes(
                execute_span,
                span_attrs={"aegis.warning_id": warning.warning_id if warning else "", "aegis.used_agent": used_agent},
            )
        result.stages.append(
            StageResult(
                "execute",
                "agent" if used_agent else "local",
                warning is not None,
                sp.finished_ms or 0.0,
                warning.warning_id if warning else "预警发布失败",
            )
        )
        result.warning = warning
        if warning:
            self._warnings[warning.warning_id] = warning

        async with instrumentation.span("stage_feedback_ms", trace_id=trace) as feedback_span:
            with self._tracer.span("stage_feedback_ms", trace_id=trace) as sp:
                await self._feedback(warning, trace)
            instrumentation.set_attributes(feedback_span, span_attrs={"aegis.reach_expected": warning is not None})
        result.stages.append(StageResult("feedback", "local", True, sp.finished_ms or 0.0, "触达回执已归档"))
        return await self._finish(result)

    async def process_many(self, grouped: dict[str, list[TelemetryReading]]) -> list[ChainResult]:
        """多区域并发处理（一区域一条链路，互不阻塞）。"""
        return list(await asyncio.gather(*(self.process(readings, region_code=region) for region, readings in grouped.items())))

    # ---------- 各段实现 ----------

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

    @property
    def _agent_txn_ms(self) -> int:
        """LLM 段事务的预算（§5 A 案）：与段预算同一档，不用两个数互相打脸。"""
        return int(self._settings.sla_agent_txn_ms)

    @property
    def _agent_txn_ttl_ms(self) -> int:
        # TTL 必须宽于预算：默认 30s 的 TTL 会在投递前就把 60s 事务判成"超期丢弃"，
        # 等于让一个传输层默认值偷偷改小业务预算。留 5s 给排队与回程。
        return int(self._settings.sla_agent_txn_ms) + 5_000

    async def _assess(
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
                deadline_ms=self._agent_txn_ms,
                ttl_ms=self._agent_txn_ttl_ms,
                priority=1,
            )
        except AegisError as exc:
            result.degradations.append(f"研判降级: {exc.message}")
            return local, False

        if (verdict := self._parse_agent_assessment(response.payload, region, hits)) is not None:
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
                # 事务预算 = LLM 档（§5 A 案）：与 `stage_*_ms` 同档同钟；
                # 不再用 `llm_timeout_seconds`（单次 HTTP 超时），也不蹭 ≤10s 调度档。
                deadline_ms=self._agent_txn_ms,
                ttl_ms=self._agent_txn_ttl_ms,
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
        return units or local

    async def _recall_cases(self, verdict: RiskVerdict, result: ChainResult) -> list[dict[str, Any]]:
        """规划前的案例召回：LLM 与图谱都不进这条路径的必需依赖，失败只记降级、绝不阻断预警。

        预算来自 knowledge_recall_budget_ms：宁可少给几条参考案例，也不能让一次知识检索
        把"≤3min 预警生成"的窗口吃掉。命中切片由 `CaseMatch.planning_brief()` 给出，
        与案例知识 API 共用同一份映射（链路不自己抄一遍字段）。
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
        from aegis.retrieval.service import RetrievalQuery  # 延迟导入：链路不因此依赖检索层的 I/O 面

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
                    # 事务预算 = LLM 档（§5 A 案）：与 `stage_*_ms` 同档同钟；
                    # 不再用 `llm_timeout_seconds`（单次 HTTP 超时），也不蹭 ≤10s 调度档。
                    deadline_ms=self._agent_txn_ms,
                    ttl_ms=self._agent_txn_ttl_ms,
                    priority=1,
                )
                used_agent = True
                record = self._adopt_agent_copy(record, response.payload, result, verdict=verdict)
            except AegisError as exc:
                result.degradations.append(f"执行智能体失败，文案回落平台模板: {exc.message}")
        dispatched = await self._direct_dispatch(record, result)
        if dispatched is None:
            return None, used_agent
        await self._broadcast(dispatched, trace)
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
        facts = HazardResponseChain._copy_facts(record, verdict)
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

    async def _direct_dispatch(self, record: WarningRecord, result: ChainResult) -> WarningRecord | None:
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

    async def _feedback(self, record: WarningRecord | None, trace: str) -> None:
        if record is None:
            return
        delivered = sum(1 for d in record.deliveries if d.status in ("delivered", "retried"))
        event = make_event(
            source="platform.feedback",
            action=Action.FEEDBACK_STATUS.value,
            trace_id=trace,
            target="platform.feedback_sink",
            payload={
                "warning_id": record.warning_id,
                "reach_stats": {
                    "channels": len(record.deliveries),
                    "delivered": delivered,
                    "reach_seconds": record.reach_seconds(),
                },
                "response_state": "pending" if delivered else "unknown",
                "observed_at": now_iso(),
            },
        )
        await self._gateway.publish_to(subjects.ops("feedback", "status"), event)

    @property
    def warnings(self) -> dict[str, WarningRecord]:
        return dict(self._warnings)
