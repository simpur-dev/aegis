"""灾害响应链路的**固定顺序编排器**（M1 五段）。

D7 编排归一（§4.6）后这里只剩"顺序 + 记账"：五段的实际逻辑（智能体优先、契约校验、降级留痕、
凭证转记）全在 `pipeline/stages.py`，产物定义在 `pipeline/result.py`。本模块不自带任何段实现——
工作流的"标准五段"模板调的是同一个 `HazardStages` 对象，两条编排路径因此不可能各说各话。

每段的 agent/local 语义写在 `stages.py` 的模块文档里；这里保留的是链路的对外形状：
根跨度 `hazard_chain`、五段各自的 `stage_*_ms` 账本样本、以及早退路径也照样 `_finish`（落库）。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from aegis.bus.gateway import AgentGateway, ContractRegistry
from aegis.bus.registry import AgentRegistry
from aegis.config import Settings
from aegis.domain.messages import AgentMessage, TelemetryReading, TriggerHit, WarningRecord, new_event_id, new_trace_id
from aegis.knowledge.provider import KnowledgeProvider
from aegis.observability import instrumentation
from aegis.observability.tracer import Tracer
from aegis.pipeline.result import STAGE_ORDER, ChainResult, StageResult
from aegis.pipeline.stages import HazardStages
from aegis.services.delivery import DeliveryDispatcher
from aegis.services.risk_engine import RiskEngine, RiskVerdict
from aegis.services.task_parser import TaskParser
from aegis.services.trigger_rules import RuleEngine
from aegis.services.warning_service import WarningService

if TYPE_CHECKING:
    # 只在类型面出现：编排器运行时不 import 检索层（铁律 3：内核不依赖可选腿的实现）。
    from aegis.retrieval.service import HybridRetrievalService

log = logging.getLogger("aegis.pipeline")


class HazardResponseChain:
    """按固定顺序跑五段。段的实现见 `HazardStages`——本类不再自带判定分支。"""

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
        stages: HazardStages | None = None,
    ) -> None:
        #: `stages` 是给工作流编排留的中转口：DAG 节点持同一个 HazardStages 实例，
        #: 而不是再造一份"看起来一样"的段逻辑（parity 用例断的正是这层同一性）。
        self._stages = stages or HazardStages(
            gateway=gateway,
            registry=registry,
            contracts=contracts,
            tracer=tracer,
            settings=settings,
            rule_engine=rule_engine,
            risk_engine=risk_engine,
            task_parser=task_parser,
            warning_service=warning_service,
            dispatcher=dispatcher,
            agent_hit_window=agent_hit_window,
            knowledge=knowledge,
            retrieval=retrieval,
        )
        self._tracer = self._stages.tracer
        self._on_result = on_result

    @property
    def stages(self) -> HazardStages:
        return self._stages

    @property
    def warnings(self) -> dict[str, WarningRecord]:
        return self._stages.warnings

    async def handle_agent_message(self, message: AgentMessage) -> None:
        """网关上行事件：感知段的智能体触发命中由此进入 `HazardStages`。"""
        await self._stages.handle_agent_message(message)

    async def _finish(self, result: ChainResult) -> ChainResult:
        if self._on_result is not None:
            await self._on_result(result)
        return result

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
                hits, agent_hit_count = await self._stages.perceive(readings, region, trace, preset_hits=preset_hits)
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
            result.stages.extend([StageResult(name, "skipped", True, 0.0, "无触发条件命中") for name in STAGE_ORDER[1:]])
            return await self._finish(result)

        result.stages.append(await self._assess_stage(result, hits, region, trace, preset_verdict, reported))
        if result.verdict is None:
            return await self._finish(result)

        result.stages.append(await self._plan_stage(result, trace))
        result.stages.append(await self._execute_stage(result, trace, started_at))
        result.stages.append(await self._feedback_stage(result, trace))
        return await self._finish(result)

    # ---------- 逐段的记账（段本身的逻辑全在 HazardStages） ----------

    async def _assess_stage(
        self, result: ChainResult, hits: list[TriggerHit], region: str, trace: str, preset_verdict: RiskVerdict | None, reported: bool
    ) -> StageResult:
        before = len(result.degradations)
        async with instrumentation.span("stage_assess_ms", trace_id=trace) as span:
            with self._tracer.span("stage_assess_ms", trace_id=trace) as sp:
                verdict, answered_by_agent = await self._stages.assess(
                    hits, region, result, trace, preset_verdict=preset_verdict, reported=reported
                )
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
            "agent" if answered_by_agent else "local",
            verdict is not None,
            sp.finished_ms or 0.0,
            f"等级 {int(verdict.risk_level)}" if verdict else "定级失败",
            model_info=result.stage_models.get("assess"),
        )

    async def _plan_stage(self, result: ChainResult, trace: str) -> StageResult:
        verdict = result.verdict
        assert verdict is not None  # 调用方已在研判段之后早退，这里不重开一条降级轨
        before = len(result.degradations)
        async with instrumentation.span("stage_plan_ms", trace_id=trace) as span:
            with self._tracer.span("stage_plan_ms", trace_id=trace) as sp:
                units = await self._stages.plan(verdict, result, trace)
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

    async def _execute_stage(self, result: ChainResult, trace: str, started_at: float) -> StageResult:
        verdict = result.verdict
        assert verdict is not None
        before = len(result.degradations)
        async with instrumentation.span("stage_execute_ms", trace_id=trace) as span:
            with self._tracer.span("stage_execute_ms", trace_id=trace) as sp:
                warning, used_agent = await self._stages.execute(verdict, result, trace, started_at=started_at)
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

    async def _feedback_stage(self, result: ChainResult, trace: str) -> StageResult:
        async with instrumentation.span("stage_feedback_ms", trace_id=trace) as span:
            with self._tracer.span("stage_feedback_ms", trace_id=trace) as sp:
                feedback = await self._stages.feedback(result.warning, trace, result)
            instrumentation.set_attributes(span, span_attrs={"aegis.reach_expected": result.warning is not None})
        return StageResult(
            "feedback",
            "agent" if feedback is not None else "local",
            True,
            sp.finished_ms or 0.0,
            # 归因结论要点进台账：`chain_runs.stages` 是事后唯一还能查到的地方
            self._feedback_note(feedback),
            model_info=result.stage_models.get("feedback"),
        )

    @staticmethod
    def _feedback_note(feedback: dict[str, Any] | None) -> str:
        if feedback is None:
            return "触达回执已归档"
        return (
            f"触达回执已归档｜效果分 {feedback['effect_score']:.3f}｜归因腿 {feedback['decided_by']}｜建议 {feedback['suggestion_status']}"
        )

    async def process_many(self, grouped: dict[str, list[TelemetryReading]]) -> list[ChainResult]:
        """多区域并发处理（一区域一条链路，互不阻塞）。"""
        return list(await asyncio.gather(*(self.process(readings, region_code=region) for region, readings in grouped.items())))
