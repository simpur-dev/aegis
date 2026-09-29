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
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from aegis.bus import subjects
from aegis.bus.gateway import AgentGateway, ContractRegistry
from aegis.bus.registry import AgentRegistry
from aegis.config import Settings, get_settings
from aegis.domain.enums import Action, AgentType, HazardType, RiskLevel
from aegis.domain.messages import (
    AgentMessage,
    DeliveryAttempt,
    StandardizedTaskUnit,
    TelemetryReading,
    TriggerHit,
    WarningRecord,
    make_event,
    new_event_id,
    new_trace_id,
    now_iso,
    parse_iso,
    utc_now,
)
from aegis.errors import AegisError
from aegis.observability.tracer import Tracer
from aegis.services.delivery import AllChannelsFailedError, DeliveryDispatcher
from aegis.services.risk_engine import RiskEngine, RiskVerdict
from aegis.services.task_parser import TaskParser
from aegis.services.trigger_rules import RuleEngine
from aegis.services.warning_service import WarningService

log = logging.getLogger("aegis.pipeline")

_STAGE_ORDER = ("perceive", "assess", "plan", "execute", "feedback")


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
    ) -> ChainResult:
        trace = trace_id or new_trace_id()
        result = ChainResult(trace_id=trace, event_id=event_id or new_event_id())
        region = region_code or (readings[0].region_code if readings else "UNKNOWN")

        with self._tracer.span("stage_perceive_ms", trace_id=trace) as sp:
            hits, agent_hit_count = await self._perceive(readings, region, trace)
        result.stages.append(
            StageResult(
                "perceive",
                "hybrid" if agent_hit_count else "local",
                True,
                sp.finished_ms or 0.0,
                f"命中 {len(hits)} 条（智能体 {agent_hit_count} 条）",
            )
        )
        result.hits = hits

        if not hits:
            result.stages.extend([StageResult(n, "skipped", True, 0.0, "无触发条件命中") for n in _STAGE_ORDER[1:]])
            return await self._finish(result)

        with self._tracer.span("stage_assess_ms", trace_id=trace) as sp:
            verdict = await self._assess(hits, region, result, trace)
        result.stages.append(
            StageResult(
                "assess",
                "agent" if verdict and verdict.assessed_by != "platform.risk_engine" else "local",
                verdict is not None,
                sp.finished_ms or 0.0,
                f"等级 {int(verdict.risk_level)}" if verdict else "定级失败",
            )
        )
        if verdict is None:
            return await self._finish(result)
        result.verdict = verdict

        with self._tracer.span("stage_plan_ms", trace_id=trace) as sp:
            units = await self._plan(verdict, result, trace)
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

        with self._tracer.span("stage_execute_ms", trace_id=trace) as sp:
            warning, used_agent = await self._execute(verdict, result, trace)
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

        with self._tracer.span("stage_feedback_ms", trace_id=trace) as sp:
            await self._feedback(warning, trace)
        result.stages.append(StageResult("feedback", "local", True, sp.finished_ms or 0.0, "触达回执已归档"))
        return await self._finish(result)

    async def process_many(self, grouped: dict[str, list[TelemetryReading]]) -> list[ChainResult]:
        """多区域并发处理（一区域一条链路，互不阻塞）。"""
        return list(await asyncio.gather(*(self.process(readings, region_code=region) for region, readings in grouped.items())))

    # ---------- 各段实现 ----------

    async def _perceive(self, readings: list[TelemetryReading], region: str, trace: str) -> tuple[list[TriggerHit], int]:
        local_hits = self._rule_engine.evaluate(readings, region_code=region).hits
        agent_hits = self._drain_agent_hits(region)
        merged = list(local_hits)
        seen = {h.rule_id for h in merged}
        merged.extend(h for h in agent_hits if h.rule_id not in seen)
        for _hit in agent_hits:
            self._tracer.record("perceive_agent_hit_ms", 0.0, trace_id=trace, outcome="agent")
        return merged, len(agent_hits)

    async def _assess(self, hits: list[TriggerHit], region: str, result: ChainResult, trace: str) -> RiskVerdict | None:
        local = self._risk_engine.assess(hits, region_code=region)
        if self._registry.pick(AgentType.ASSESS.value, "risk_assess") is None:
            return local
        try:
            response = await self._gateway.dispatch(
                AgentType.ASSESS,
                Action.ASSESS_HAZARD,
                {
                    "region_code": region,
                    "window": "1h",
                    "candidate_hazards": sorted({h.hazard_type for h in hits}),
                    "hits": [h.model_dump() for h in hits],
                },
                trace_id=trace,
                capability="risk_assess",
                deadline_ms=int(self._settings.llm_timeout_seconds * 1000),
                priority=1,
            )
        except AegisError as exc:
            result.degradations.append(f"研判降级: {exc.message}")
            return local

        if (verdict := self._parse_agent_assessment(response.payload, region, hits)) is not None:
            return verdict
        result.degradations.append("研判结论不合契约，采用规则定级")
        return local

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
        local = self._task_parser.parse(verdict, event_id=result.event_id)
        if self._registry.pick(AgentType.PLAN.value, "task_decompose") is None:
            return local
        try:
            response = await self._gateway.dispatch(
                AgentType.PLAN,
                Action.PLAN_STU,
                {
                    "event_id": result.event_id,
                    "risk": verdict.as_payload(),
                    "objective": "按灾种处置剧本生成标准化任务单元",
                    "constraints": {"sla_warning_gen_seconds": self._settings.sla_warning_gen_seconds},
                },
                trace_id=trace,
                capability="task_decompose",
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

    async def _execute(
        self,
        verdict: RiskVerdict,
        result: ChainResult,
        trace: str,
    ) -> tuple[WarningRecord | None, bool]:
        draft = await self._warning_service.generate(verdict, event_id=result.event_id, trace_id=trace)
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
                    },
                    trace_id=trace,
                    capability="warn_publish",
                    priority=1,
                )
                record.released_at = now_iso()
                record.deliveries = self._parse_ack(response.payload, record)
                used_agent = True
                self._record_agent_reach(record, trace, response.source)
            except AegisError as exc:
                result.degradations.append(f"执行智能体失败，转平台直发: {exc.message}")
                dispatched = await self._direct_dispatch(record, result)
                if dispatched is None:
                    return None, used_agent
                record = dispatched
        else:
            dispatched = await self._direct_dispatch(record, result)
            if dispatched is None:
                return None, False
            record = dispatched

        if record is None:
            return None, used_agent
        await self._broadcast(record, trace)
        return record, used_agent

    async def _direct_dispatch(self, record: WarningRecord, result: ChainResult) -> WarningRecord | None:
        if self._dispatcher is None:
            record.released_at = now_iso()
            return record
        try:
            return await self._dispatcher.dispatch(record)
        except AllChannelsFailedError as exc:
            result.errors.append(f"全部通道失败: {exc.message}")
            return None

    @staticmethod
    def _parse_ack(payload: dict[str, Any], record: WarningRecord) -> list[DeliveryAttempt]:
        attempts: list[DeliveryAttempt] = []
        for item in payload.get("channel_results", []):
            if not isinstance(item, dict):
                continue
            try:
                status = str(item.get("status", "delivered"))
                attempts.append(
                    DeliveryAttempt(
                        channel=str(item["channel"]),
                        audience_count=int(item.get("audience_count", 0)),
                        status=status,
                        receipt_at=item.get("receipt_at") or (now_iso() if status == "delivered" else None),
                        provider_msg_id=item.get("provider_msg_id"),
                    )
                )
            except (KeyError, ValueError, TypeError):
                continue
        return attempts or [
            DeliveryAttempt(channel=c, audience_count=len(record.audiences), status="delivered", receipt_at=now_iso())
            for c in record.channels
        ]

    def _record_agent_reach(self, record: WarningRecord, trace: str, agent_id: str) -> None:
        """智能体代发时的触达时延：以回执时间为证据，无回执时用 ack 到达时刻兜底。"""
        reach = record.reach_seconds()
        if reach is None:
            anchor = record.released_at or record.generated_at
            reach = max((utc_now() - parse_iso(anchor)).total_seconds(), 0.0)
        self._tracer.record("warning_reach_ms", max(reach, 0.0) * 1000, trace_id=trace, agent_id=agent_id)

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
