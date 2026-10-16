"""人工上报接入腿（架构文档 §6.2 场景二）：文本 → 三路融合解析 → 与监测事件同一条链路。

（P4 纯搬运：`submit` 与 `_open_report_review` 的方法体逐字未改，原先是 `container.py` 的两个方法。）

这条腿住进容器是因为"要拿解析器、链路、tracer、工作流引擎"，而它做的事是业务判定不是装配：
容器因此长出 68 个字段里的四个共用依赖与一段 140 行的流程。现在它自己一个模块，
容器只留 `report_intake` 一个字段——**不留同名委托方法**，两处都能调的入口迟早会长出第二套口径。

低置信度上报要开人工核签工单，工单本身是工作流实例；`workflow` 与 `chain` 都由装配点注入，
本模块不自己造引擎，也不碰任何可选腿的 I/O 面（铁律 3）。
"""

from __future__ import annotations

import logging
import time
from typing import Any

from aegis.domain.chain import ChainResult
from aegis.domain.messages import TriggerHit, now_iso
from aegis.observability.tracer import Tracer
from aegis.pipeline.workflow_chain import WorkflowChain
from aegis.services.interpret.semantic_parser import DisasterTextParser, ParsedDisaster
from aegis.services.memory.label_flywheel import CAPTURE_MARKER
from aegis.workflow.engine import WorkflowEngine
from aegis.workflow.templates import REPORT_REVIEW_TEMPLATE

log = logging.getLogger("aegis.pipeline")


class ReportIntake:
    """上报受理口：解析、定级建议、链路触发与核签工单开单，一条链路的入口翻译。"""

    def __init__(
        self,
        *,
        parser: DisasterTextParser | None,
        chain: WorkflowChain,
        tracer: Tracer,
        workflow: WorkflowEngine,
        report_stats: dict[str, int],
    ) -> None:
        self.parser = parser
        self.chain = chain
        self.tracer = tracer
        self.workflow = workflow
        #: 计数仍挂在容器上（`latency_report()` 的 `reports` 一格读它），这里拿的是同一个 dict 引用。
        self.report_stats = report_stats

    async def submit(
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
        之后的研判、决策、预警生成、靶向触达、降级留痕与时延量测全部走链路入口 `self.chain`。

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
                # 这两项是给数据飞轮用的：`capture_label` 声明"这一单裁决的是某条结论成不成立"，
                # `observed_at` 给出真值的观测时刻（工单就在处理该上报的时刻开出，即上报处理时刻）
                "capture_label": CAPTURE_MARKER,
                "observed_at": now_iso(),
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
