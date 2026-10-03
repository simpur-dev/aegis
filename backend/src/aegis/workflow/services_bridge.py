"""把工作流节点所需能力桥接到平台既有服务。

引擎与节点只依赖 WorkflowServices 的函数签名，因此：
- 单测可注入纯 Python 桩件；
- 生产可注入真实实现（本模块）；
- 未来可注入智能体代理（经总线 request-reply），三处共用同一套节点定义。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from aegis.domain.enums import HazardType, RiskLevel
from aegis.domain.messages import TelemetryReading, TriggerHit, WarningRecord, make_event, now_iso
from aegis.services.risk_engine import RiskVerdict
from aegis.workflow.nodes import WorkflowServices

if TYPE_CHECKING:
    from collections.abc import Sequence

    from aegis.bus.gateway import AgentGateway
    from aegis.services.delivery import DeliveryDispatcher
    from aegis.services.risk_engine import RiskEngine
    from aegis.services.trigger_rules import RuleEngine
    from aegis.services.warning_service import WarningService
    from aegis.storage.store import StoreProtocol
    from aegis.workflow.outbound import OutboundCaller

# 推演一次取几条相似案例：三条是"多情景"的下限（降级态也是三档），再多就把画布输出撑爆，
# 而知识层的召回预算本来就是按个位数命中量标定的（预算实现在提供者一侧，桥不读配置）。
DEFAULT_SCENARIO_CASE_LIMIT = 3
DEFAULT_SCENARIO_HORIZON_MINUTES = 60

# 案例里可直接引用的量化要素：键名与知识层 `HazardCase` 对齐，但这里只认数据键不认类型——
# 内核环 import 可选腿是被 AST 门禁禁止的（架构铁律 3），跨环只传 dict。
_SCALAR_FACTOR_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("estimated_delay_hours", "估计延迟", "小时"),
    ("confidence", "案例置信度", ""),
)
_LIST_FACTOR_FIELDS: tuple[tuple[str, str], ...] = (
    ("applies_to_levels", "适用等级"),
    ("monitoring_metrics", "监测指标"),
)


@runtime_checkable
class ScenarioProvider(Protocol):
    """态势推演需要的"相似案例"能力：只读召回，由装配点注入。

    为什么是协议而不是直接 import `knowledge`/`retrieval`：`workflow/` 属内核环，
    架构铁律 3（有 AST 门禁）禁止它 import 可选腿，连延迟导入也不行。协议只做结构匹配，
    知识图谱、混合检索、甚至外部智能体的 `assess.*` 代理都能实现它并作为 `scenario_provider` 接进来。

    返回 `Sequence[Mapping[str, Any]]` 而不是腿里的 `CaseMatch`/`HazardCase`：跨环只传数据、不传类型。
    实现方可给的键（缺什么就不出什么要素，推演不会替它补数）：
    `case_id`（必需，无此键的命中不可追溯，直接丢弃）、`title`、`estimated_delay_hours`、
    `applies_to_levels`、`monitoring_metrics`、`confidence`、`matched_on`、`source`。
    """

    async def recall_scenarios(
        self,
        *,
        hazard_type: str | None = None,
        region_code: str | None = None,
        risk_level: int | None = None,
        limit: int = DEFAULT_SCENARIO_CASE_LIMIT,
    ) -> Sequence[Mapping[str, Any]]: ...


def _case_factors(case: Mapping[str, Any], *, case_id: str) -> list[dict[str, Any]]:
    """把案例自带的量化要素原样搬出来。

    案例没给的字段一律不出现在清单里——"没测到的如实写未测得，不编数"这条铁律在推演节点上
    同样成立：一个看起来合理的默认延迟小时数，比没有数字更危险。
    """
    factors: list[dict[str, Any]] = []
    for key, label, unit in _SCALAR_FACTOR_FIELDS:
        value = case.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            factors.append({"key": key, "label": label, "value": round(float(value), 3), "unit": unit, "provenance": case_id})
    for key, label in _LIST_FACTOR_FIELDS:
        raw = case.get(key)
        if isinstance(raw, (list, tuple, set, frozenset)) and raw:
            factors.append(
                {
                    "key": key,
                    "label": label,
                    "value": [item if isinstance(item, (int, float)) else str(item) for item in raw][:12],
                    "unit": "",
                    "provenance": case_id,
                }
            )
    return factors


def _expected_level(declared: int, applies_to_levels: Any) -> tuple[int, str]:
    """情景等级取"案例自己声明的适用等级里与本次定级最接近的一档"，同距取更严重的一档。

    这是 ±1 启发式被换掉的地方：偏移量不再由代码猜，而是来自案例声明（可回溯到 case_id）。
    案例没声明适用等级时就如实沿用本次定级，不做任何猜测性偏移。
    """
    valid: set[int] = set()
    if isinstance(applies_to_levels, (list, tuple, set, frozenset)):
        for item in applies_to_levels:
            if isinstance(item, bool) or not isinstance(item, (int, float)):
                continue
            if float(item).is_integer():
                valid.add(int(item))
    usable = sorted(item for item in valid if 1 <= item <= 5)
    if not usable:
        return declared, "案例未声明适用等级：沿用本次定级，不做偏移猜测"
    nearest = min(usable, key=lambda item: (abs(item - declared), item))
    return nearest, f"案例声明适用等级 {usable}，取与本次定级 {declared} 最接近的一档（同距取更严重）"


def _declared_level(payload: Mapping[str, Any]) -> int:
    """定级结论可能在载荷顶层，也可能裹在 `risk_assess` 的 `risk` 里（节点是否合并上游由 handler 决定）。"""
    risk = payload.get("risk")
    source: Any = risk if isinstance(risk, Mapping) and "risk_level" in risk else payload
    return int(source.get("risk_level", int(RiskLevel.BLUE)))


def _hazard_and_region(payload: Mapping[str, Any]) -> tuple[str | None, str | None]:
    """灾种与区域可能在载荷顶层，也可能裹在定级结论里：两处都读一次，读不到就给 None（召回不限定）。"""
    raw_risk = payload.get("risk")
    risk: Mapping[str, Any] = raw_risk if isinstance(raw_risk, Mapping) else {}
    hazard = payload.get("hazard_type") or risk.get("hazard_type")
    region = payload.get("region_code") or risk.get("region_code")
    return (str(hazard) if hazard else None, str(region) if region else None)


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
    scenario_provider: ScenarioProvider | None = None,
) -> WorkflowServices:
    """装配节点服务。

    `outbound` 缺席或未配白名单时 `http_call` 留 None：`api_call`/`device_control`
    会按"缺少依赖服务"响亮失败，而不是悄悄往画布上填的任意地址发请求。

    `scenario_provider` 缺席时 `situation_simulate` 走显式降级（`degraded: True` 并写明原因），
    行为与升级前一致地保守、但不再假装是案例驱动：内核不得 import 知识/检索腿，
    案例这条腿只能由装配点（container/integrations）注入。
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
        """案例驱动的多情景推演；没有案例可依据时如实降级，不再用 ±1 冒充推演结论。

        画布与既有用例在读的三个键（`horizon_minutes` / `scenarios` / `trend`）保持原样，
        新增的是审计面：`degraded` / `degraded_reason` / `case_count` / `dropped_cases`。
        `nodes.py` 的 `_situation_simulate` 会把这几个键一起带进节点输出（并把上游
        `risk_assess`/`hazard_identify` 的等级与灾种并进 payload）——"这条结论有没有案例佐证"
        必须同时看得见两处：情景各自的 `basis`/`refs`，以及整条推演的降级原因。
        """
        level = _declared_level(payload)
        horizon = int(payload.get("horizon_minutes", DEFAULT_SCENARIO_HORIZON_MINUTES))
        hazard_type, region_code = _hazard_and_region(payload)
        # 条数下限守成 1：画布上填了 0 或负数时，宁可问一条，也不要拿着空 limit 去报"无命中"
        limit = max(1, int(payload.get("case_limit") or DEFAULT_SCENARIO_CASE_LIMIT))

        recalled: list[Mapping[str, Any]] = []
        degraded_reason = ""
        if scenario_provider is None:
            degraded_reason = "未注入案例能力（scenario_provider 缺位）：等级偏移只由本次声明等级推得，不含案例证据与任何量化数值"
        else:
            try:
                recalled = list(
                    await scenario_provider.recall_scenarios(
                        hazard_type=hazard_type, region_code=region_code, risk_level=level, limit=limit
                    )
                )
            except Exception as exc:  # 案例腿故障不等于态势没了：降级继续，但原因必须留在结论里
                degraded_reason = f"案例能力调用失败（{type(exc).__name__}: {str(exc)[:160]}）：本轮无案例佐证"

        usable = [case for case in recalled if isinstance(case, Mapping) and str(case.get("case_id") or "").strip()]
        dropped = len(recalled) - len(usable)
        if not usable and not degraded_reason:
            degraded_reason = "案例召回无命中（相似案例 0 条）：等级偏移只由本次声明等级推得，不含案例证据"

        if usable:
            scenarios: list[dict[str, Any]] = []
            for case in usable[:limit]:
                case_id = str(case.get("case_id")).strip()
                expected, basis = _expected_level(level, case.get("applies_to_levels"))
                matched_on = case.get("matched_on")
                scenarios.append(
                    {
                        # 情景名直接用案例标题：读者在画布上就能看到"这个情景是从哪条案例来的"
                        "name": str(case.get("title") or case_id)[:64],
                        "expected_level": expected,
                        "impact_factors": _case_factors(case, case_id=case_id),
                        "refs": [case_id],
                        "basis": basis,
                        "matched_on": [str(item) for item in matched_on][:8] if isinstance(matched_on, (list, tuple)) else [],
                        "case_source": str(case.get("source") or "")[:32],
                    }
                )
            expected_levels = [int(item["expected_level"]) for item in scenarios]
            # 趋势由案例声明的适用等级推得：有情景比本次更严重就是"升级"，全部更轻才是"缓解"。
            trend = "升级" if min(expected_levels) < level else "缓解" if max(expected_levels) > level else "持平"
            return {
                "horizon_minutes": horizon,
                "scenarios": scenarios,
                "trend": trend,
                "degraded": False,
                "degraded_reason": "",
                "case_count": len(scenarios),
                "dropped_cases": dropped,
                "declared_level": level,
                "hazard_type": hazard_type,
                "region_code": region_code,
            }

        # 降级分支：三档命名与算法沿用升级前口径（画布与用例行号都认这三个名字），
        # 但每一档都自带"这是从声明等级推的、没有案例"的说明，且要素清单里只有等级本身。
        degraded_basis = f"降级推演：{degraded_reason}"
        declared_factor = {"key": "declared_risk_level", "label": "本次声明风险等级", "value": level, "unit": "", "provenance": "payload"}
        scenarios = [
            {
                "name": "保守",
                "expected_level": min(5, level + 1),
                "impact_factors": [dict(declared_factor)],
                "refs": [],
                "basis": degraded_basis,
            },
            {"name": "中性", "expected_level": level, "impact_factors": [dict(declared_factor)], "refs": [], "basis": degraded_basis},
            {
                "name": "不利",
                "expected_level": max(1, level - 1),
                "impact_factors": [dict(declared_factor)],
                "refs": [],
                "basis": degraded_basis,
            },
        ]
        trend = "升级" if level <= 2 else "持平" if level <= 3 else "缓解"
        return {
            "horizon_minutes": horizon,
            "scenarios": scenarios,
            "trend": trend,
            "degraded": True,
            "degraded_reason": degraded_reason,
            "case_count": 0,
            "dropped_cases": dropped,
            "declared_level": level,
            "hazard_type": hazard_type,
            "region_code": region_code,
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
