"""预警内容生成：按风险等级×受众生成差异化预警（生成时延计入 ≤3min 指标）。

藏语文本经可注入的翻译器产出（LLM 或术语表），未配置翻译能力时显式标记待译，
绝不伪造译文——验收口径上宁可标注 pending 也不给出未经审核的藏语预警。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from aegis.config import Settings, get_settings
from aegis.domain.enums import Channel, HazardType, RiskLevel
from aegis.domain.messages import WarningRecord, new_event_id, new_trace_id
from aegis.observability.tracer import Tracer
from aegis.services.risk_engine import RiskVerdict

log = logging.getLogger("aegis.services.warning")

Translator = Callable[[str], Awaitable[str]]

# 受众矩阵：等级越高，触达面越宽（架构文件「按区域/人群/设备精准推送」）
AUDIENCE_MATRIX: dict[RiskLevel, list[str]] = {
    RiskLevel.RED: ["residents", "schools", "construction_sites", "rescue_teams", "commanders"],
    RiskLevel.ORANGE: ["residents", "schools", "rescue_teams", "commanders"],
    RiskLevel.YELLOW: ["residents", "rescue_teams", "commanders"],
    RiskLevel.BLUE: ["residents", "commanders"],
    RiskLevel.NONE: [],
}

CHANNEL_MATRIX: dict[RiskLevel, list[Channel]] = {
    RiskLevel.RED: [Channel.SMS, Channel.BEIDOU, Channel.BROADCAST, Channel.WECHAT],
    RiskLevel.ORANGE: [Channel.SMS, Channel.BROADCAST, Channel.WECHAT],
    RiskLevel.YELLOW: [Channel.SMS, Channel.WECHAT],
    RiskLevel.BLUE: [Channel.WECHAT],
    RiskLevel.NONE: [],
}

_ACTION_TEMPLATE: dict[HazardType, str] = {
    HazardType.DEBRIS_FLOW: "远离沟道与低洼河道，禁止横穿河滩，按预案线路向高处转移",
    HazardType.LANDSLIDE: "撤离滑坡体前后缘，观察坡体裂缝与渗水变化，禁止返回危险区",
    HazardType.ROCKFALL: "远离危岩与陡崖下方，注意落石警示标志，暂停崖下作业",
    HazardType.AVALANCHE: "封闭雪崩通道，禁止进入雪坡坡脚与沟谷，暂停高山作业",
    HazardType.LAKE_OUTBURST: "撤离冰湖下游河道两岸，关注水位与坝体异常，按预案向高地转移",
    HazardType.QUAKE_TRIGGERED: "警惕震后次生山地灾害，避开陡崖、沟谷与松散堆积体",
    HazardType.UNKNOWN: "按属地应急预案转移至安全区域，等待进一步研判结论",
}


@dataclass(frozen=True, slots=True)
class WarningDraft:
    record: WarningRecord
    translation_pending: bool

    @property
    def generation_seconds(self) -> float:
        from aegis.domain.messages import now_iso, parse_iso

        return max((parse_iso(now_iso()) - parse_iso(self.record.generated_at)).total_seconds(), 0.0)


class WarningService:
    def __init__(
        self,
        tracer: Tracer | None = None,
        *,
        settings: Settings | None = None,
        translator: Translator | None = None,
    ) -> None:
        self._tracer = tracer or Tracer()
        self._settings = settings or get_settings()
        self._translator = translator

    def compose_title(self, verdict: RiskVerdict) -> str:
        return f"西藏{verdict.hazard_type.cn}{'预警' if verdict.risk_level is not RiskLevel.NONE else '解除'}（{verdict.risk_level.cn}）"

    def compose_body(self, verdict: RiskVerdict) -> str:
        action = _ACTION_TEMPLATE.get(verdict.hazard_type, _ACTION_TEMPLATE[HazardType.UNKNOWN])
        return (
            f"监测区域 {verdict.region_code} 判定为{verdict.risk_level.cn}{verdict.hazard_type.cn}风险，"
            f"置信度 {verdict.confidence:.0%}。依据：{verdict.rationale}。处置建议：{action}。"
        )

    def build(
        self,
        verdict: RiskVerdict,
        *,
        event_id: str | None = None,
        trace_id: str | None = None,
    ) -> WarningRecord:
        return WarningRecord(
            event_id=event_id or new_event_id(),
            trace_id=trace_id or new_trace_id(),
            hazard_type=verdict.hazard_type.value,
            region_codes=[verdict.region_code],
            risk_level=verdict.risk_level,
            title_zh=self.compose_title(verdict),
            body_zh=self.compose_body(verdict),
            audiences=AUDIENCE_MATRIX[verdict.risk_level],
            channels=[c.value for c in CHANNEL_MATRIX[verdict.risk_level]],
        )

    async def generate(
        self,
        verdict: RiskVerdict,
        *,
        event_id: str | None = None,
        trace_id: str | None = None,
    ) -> WarningDraft:
        """生成预警产物；翻译失败不阻断发布（降级为纯中文并标记待译）。"""
        record = self.build(verdict, event_id=event_id, trace_id=trace_id)
        pending = False
        if self._translator is not None:
            try:
                record.body_bo = await self._translator(record.body_zh)
            except Exception as exc:
                pending = True
                log.warning("藏语翻译失败，降级为中文发布", extra={"error": str(exc)})
        else:
            pending = record.risk_level is not RiskLevel.NONE

        record.translation_pending = pending
        elapsed_ms = self._elapsed_ms(record.generated_at)
        self._tracer.record("warning_generation_ms", elapsed_ms, trace_id=record.trace_id)
        if elapsed_ms / 1000 > self._settings.sla_warning_gen_seconds:
            log.warning("预警生成超出 SLA 预算", extra={"warning_id": record.warning_id})
        return WarningDraft(record=record, translation_pending=pending)

    @staticmethod
    def _elapsed_ms(iso_ts: str) -> float:
        from aegis.domain.messages import now_iso, parse_iso

        return max((parse_iso(now_iso()) - parse_iso(iso_ts)).total_seconds() * 1000, 0.0)
