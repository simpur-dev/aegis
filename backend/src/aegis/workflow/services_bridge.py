"""把工作流节点所需能力桥接到平台既有服务。

引擎与节点只依赖 WorkflowServices 的函数签名，因此：
- 单测可注入纯 Python 桩件；
- 生产可注入真实实现（本模块）；
- 未来可注入智能体代理（经总线 request-reply），三处共用同一套节点定义。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from aegis.domain.enums import HazardType, RiskLevel
from aegis.domain.messages import TelemetryReading, TriggerHit, WarningRecord, make_event, now_iso
from aegis.services.risk_engine import RiskVerdict
from aegis.workflow.nodes import WorkflowServices

if TYPE_CHECKING:
    from aegis.bus.gateway import AgentGateway
    from aegis.services.delivery import DeliveryDispatcher
    from aegis.services.risk_engine import RiskEngine
    from aegis.services.trigger_rules import RuleEngine
    from aegis.services.warning_service import WarningService
    from aegis.storage.store import StoreProtocol
    from aegis.workflow.outbound import OutboundCaller


def _to_trigger_hits(raw: Any, *, default_region: str) -> list[TriggerHit]:
    """兼容两种上游证据形态：智能体产出的 TriggerHit，与 threshold 节点产出的度量证据。

    后者没有灾种与规则 ID，按证据所在区域构造并走规则引擎的默认等级，
    宁可给出保守的、可解释的定级，也不要让链路因为格式差异而中断。
    """
    hits: list[TriggerHit] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        if {"rule_id", "hazard_type", "region_code"} <= item.keys():
            hits.append(TriggerHit.model_validate(item))
            continue
        metric = item.get("metric")
        if metric and item.get("rule"):
            hits.append(
                TriggerHit(
                    rule_id=f"R-EVID-{metric}",
                    hazard_type=str(item.get("hazard_type", HazardType.UNKNOWN.value)),
                    region_code=str(item.get("region_code", default_region)),
                    score=float(item.get("score", 1.0)),
                    evidence_refs=[str(item["rule"])],
                )
            )
    return hits


def build_workflow_services(
    *,
    store: StoreProtocol,
    rule_engine: RuleEngine,
    risk_engine: RiskEngine,
    warning_service: WarningService,
    dispatcher: DeliveryDispatcher,
    gateway: AgentGateway,
    outbound: OutboundCaller | None = None,
) -> WorkflowServices:
    """装配节点服务。

    `outbound` 缺席或未配白名单时 `http_call` 留 None：`api_call`/`device_control`
    会按"缺少依赖服务"响亮失败，而不是悄悄往画布上填的任意地址发请求。
    """

    async def telemetry_query(
        *,
        region_code: str | None = None,
        metric: str | None = None,
        limit: int = 200,
        **_extra: Any,
    ) -> list[dict[str, Any]]:
        rows = store.telemetry.query(region_code=region_code, metric=metric, limit=int(limit))
        return [row.model_dump() for row in rows]

    async def identify(payload: dict[str, Any]) -> dict[str, Any]:
        readings = [TelemetryReading.model_validate(row) for row in payload.get("readings", [])]
        evaluation = rule_engine.evaluate(readings, region_code=payload.get("region_code"))
        return {
            "hazards": sorted({hit.hazard_type for hit in evaluation.hits}),
            "hits": [hit.model_dump() for hit in evaluation.hits],
            "stations": evaluation.stations,
        }

    async def assess(payload: dict[str, Any]) -> dict[str, Any]:
        raw_hits = payload.get("hits") or []
        hits = _to_trigger_hits(raw_hits, default_region=str(payload.get("region_code", "540100")))
        verdict = risk_engine.assess(hits, region_code=payload.get("region_code"))
        if verdict is None:
            # 无命中时给保守结论而不是抛错：链路仍需继续，但要显式标注降级
            return {
                "risk_level": int(RiskLevel.BLUE),
                "confidence": 0.4,
                "rationale": "无触发命中，采用保守四级",
                "degraded": True,
            }
        return verdict.as_payload()

    async def simulate(payload: dict[str, Any]) -> dict[str, Any]:
        level = int(payload.get("risk_level", int(RiskLevel.BLUE)))
        horizon = int(payload.get("horizon_minutes", 60))
        trend = "升级" if level <= 2 else "持平" if level <= 3 else "缓解"
        return {
            "horizon_minutes": horizon,
            "scenarios": [
                {"name": "保守", "expected_level": min(5, level + 1)},
                {"name": "中性", "expected_level": level},
                {"name": "不利", "expected_level": max(1, level - 1)},
            ],
            "trend": trend,
        }

    async def generate_warning(payload: dict[str, Any]) -> dict[str, Any]:
        risk = payload.get("risk") or payload.get("assess") or {}
        if not isinstance(risk, dict) or "risk_level" not in risk:
            raise ValueError("warning_generate 需要含 risk_level 的 risk 载荷")
        verdict = RiskVerdict(
            hazard_type=HazardType(str(risk.get("hazard_type", HazardType.UNKNOWN.value))),
            region_code=str(risk.get("region_code", "540100")),
            risk_level=RiskLevel(int(risk["risk_level"])),
            confidence=float(risk.get("confidence", 0.6)),
            rationale=str(risk.get("rationale", "工作流定级结论"))[:1000],
            hits=_to_trigger_hits(
                payload.get("hits") or risk.get("evidence_hits") or [],
                default_region=str(risk.get("region_code", "540100")),
            ),
        )
        draft = await warning_service.generate(verdict, event_id=payload.get("event_id"))
        return draft.record.model_dump() | {"translation_pending": draft.translation_pending}

    async def publish_warning(payload: dict[str, Any]) -> dict[str, Any]:
        raw = payload.get("warning")
        if not isinstance(raw, dict) or "warning_id" not in raw:
            raise ValueError("warning_publish 需要完整 warning 对象")
        record = WarningRecord.model_validate(raw)
        channels = payload.get("channels") or record.channels
        record.channels = list(dict.fromkeys([*channels, *record.channels]))
        published = await dispatcher.dispatch(record)
        await store.warnings.put(published)
        delivered = sum(1 for attempt in published.deliveries if attempt.status in ("delivered", "retried"))
        return {
            "warning_id": published.warning_id,
            "delivered": delivered,
            "channels": [attempt.channel for attempt in published.deliveries],
            "released_at": published.released_at,
        }

    async def collect_feedback(payload: dict[str, Any]) -> dict[str, Any]:
        warning_id = str(payload.get("warning_id", ""))
        record = store.warnings.get(warning_id)
        if record is None:
            return {"warning_id": warning_id, "known": False}
        return {
            "warning_id": warning_id,
            "known": True,
            "delivered": sum(1 for d in record.deliveries if d.status in ("delivered", "retried")),
            "reach_seconds": record.reach_seconds(),
            "observed_at": now_iso(),
        }

    async def notify(payload: dict[str, Any]) -> None:
        event = make_event(
            source="platform.workflow",
            action="notify.sent",
            payload={"level": payload.get("level", "info"), "text": payload.get("text", "")},
            trace_id=str(payload.get("trace_id") or "trc_" + "0" * 16),
            target="platform.notify_sink",
        )
        await gateway.publish_to("ops.workflow.notify", event)

    return WorkflowServices(
        telemetry_query=telemetry_query,
        identify=identify,
        assess=assess,
        simulate=simulate,
        generate_warning=generate_warning,
        publish_warning=publish_warning,
        collect_feedback=collect_feedback,
        notify=notify,
        http_call=outbound if outbound is not None and outbound.enabled else None,
    )
