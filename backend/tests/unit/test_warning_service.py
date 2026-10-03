"""预警生成测试：等级/受众矩阵、文案要素、藏汉双语降级、生成时延埋点。"""

from __future__ import annotations

import pytest

from aegis.config import Settings
from aegis.domain.enums import Channel, HazardType, RiskLevel
from aegis.domain.messages import TriggerHit
from aegis.observability.tracer import Tracer
from aegis.services.risk_engine import RiskVerdict
from aegis.services.warning_service import AUDIENCE_MATRIX, CHANNEL_MATRIX, WarningService

REGION = "540121"


def verdict(level: RiskLevel = RiskLevel.RED, hazard: HazardType = HazardType.DEBRIS_FLOW) -> RiskVerdict:
    return RiskVerdict(
        hazard_type=hazard,
        region_code=REGION,
        risk_level=level,
        confidence=0.83,
        rationale="短时强降雨 42mm/10min",
        hits=[TriggerHit(rule_id="R-DEBRIS-RAIN-1", hazard_type=hazard.value, region_code=REGION, score=0.9)],
    )


@pytest.fixture
def service(settings: Settings) -> WarningService:
    return WarningService(Tracer(), settings=settings)


class TestComposition:
    def test_title_includes_hazard_and_level(self, service: WarningService) -> None:
        title = service.compose_title(verdict())
        assert "泥石流" in title and "红色" in title

    def test_body_carries_evidence_and_action(self, service: WarningService) -> None:
        body = service.compose_body(verdict())
        assert REGION in body
        assert "83%" in body  # 置信度
        assert "R-DEBRIS-RAIN-1" in body or "强降雨" in body
        assert "沟道" in body  # 灾种专属处置建议

    @pytest.mark.parametrize(
        ("hazard", "keyword"),
        [
            (HazardType.LANDSLIDE, "滑坡体"),
            (HazardType.ROCKFALL, "危岩"),
            (HazardType.AVALANCHE, "雪崩"),
            (HazardType.LAKE_OUTBURST, "冰湖"),
            (HazardType.UNKNOWN, "应急预案"),
        ],
    )
    async def test_each_hazard_has_specific_action(self, service: WarningService, hazard: HazardType, keyword: str) -> None:
        draft = await service.generate(verdict(RiskLevel.ORANGE, hazard))
        assert keyword in draft.record.body_zh

    def test_matrices_cover_every_level(self) -> None:
        assert set(AUDIENCE_MATRIX) == set(RiskLevel)
        assert set(CHANNEL_MATRIX) == set(RiskLevel)

    def test_audience_broadens_as_level_rises(self) -> None:
        assert len(AUDIENCE_MATRIX[RiskLevel.RED]) >= len(AUDIENCE_MATRIX[RiskLevel.ORANGE]) > len(AUDIENCE_MATRIX[RiskLevel.BLUE])
        assert AUDIENCE_MATRIX[RiskLevel.NONE] == []

    def test_channels_include_beidou_only_for_red(self) -> None:
        assert Channel.BEIDOU in CHANNEL_MATRIX[RiskLevel.RED]
        assert Channel.BEIDOU not in CHANNEL_MATRIX[RiskLevel.ORANGE]

    def test_none_level_builds_empty_targets(self, service: WarningService) -> None:
        record = service.build(verdict(RiskLevel.NONE))
        assert record.audiences == [] and record.channels == []


class TestGeneration:
    async def test_generate_records_trace_and_event_ids(self, service: WarningService) -> None:
        draft = await service.generate(verdict(), event_id="evt_" + "a" * 12, trace_id="trc_" + "b" * 16)
        assert draft.record.event_id == "evt_" + "a" * 12
        assert draft.record.trace_id == "trc_" + "b" * 16
        # 不给链路起点时只量本步：它得是个"小而有意义"的数，而不是恒等于 0 的装饰
        assert 0.0 <= draft.generation_seconds < 1.0, draft.generation_seconds

    async def test_generation_latency_is_traced(self, settings: Settings) -> None:
        tracer = Tracer()
        service = WarningService(tracer, settings=settings)
        await service.generate(verdict())
        stats = tracer.ledger.stats("warning_generation_ms")
        assert stats.count == 1
        assert 0.0 <= stats.max < 1000.0, stats

    async def test_翻译耗时算进生成时延(self, settings: Settings) -> None:
        """翻译是这一步里唯一会变慢的真实工作：它没被算进来，指标就是在替平台圆场。"""
        import asyncio

        async def slow_translate(text: str) -> str:
            await asyncio.sleep(0.05)
            return "bo:" + text

        service = WarningService(Tracer(), settings=settings, translator=slow_translate)
        draft = await service.generate(verdict())
        assert draft.generation_seconds >= 0.04, draft.generation_seconds

    async def test_生成时延按链路起点量而不是拿刚盖的章反推(self, settings: Settings) -> None:
        """钉住一个"永远绿的假判定"。

        旧算法是 `now - record.generated_at`，而 `generated_at` 就在同一次调用里刚盖上——
        等于拿现在减自己，任何真实时延下都得到 0.0ms，于是"预警生成 ≤3min"这项永远达标。
        """
        import time

        tracer = Tracer()
        tracer.ledger.set_budget("warning_generation_ms", settings.sla_warning_gen_seconds * 1000.0)
        service = WarningService(tracer, settings=settings)
        started = time.perf_counter() - 240.0  # 链路已经走了 4 分钟才到执行段（必须与被测代码同一个时钟取起点）
        draft = await service.generate(verdict(), started_at=started)
        assert 239.0 < draft.generation_seconds < 245.0, draft.generation_seconds
        stats = tracer.ledger.stats("warning_generation_ms")
        assert stats.breaches == 1, "超 3min 的生成时间必须被判违约，而不是仍然 0.0ms 一片绿"

    async def test_ids_are_unique_across_runs(self, service: WarningService) -> None:
        ids = {(await service.generate(verdict())).record.warning_id for _ in range(20)}
        assert len(ids) == 20


class TestBilingual:
    async def test_no_translator_marks_pending_and_leaves_bo_empty(self, settings: Settings) -> None:
        service = WarningService(Tracer(), settings=settings)
        draft = await service.generate(verdict())
        assert draft.translation_pending is True
        assert draft.record.body_bo is None  # 绝不伪造藏语文本

    async def test_translator_fills_bo(self, settings: Settings) -> None:
        async def translator(text: str) -> str:
            return f"བོད་::{text[:8]}"

        service = WarningService(Tracer(), settings=settings, translator=translator)
        draft = await service.generate(verdict())
        assert draft.record.body_bo and draft.translation_pending is False

    async def test_translator_failure_degrades_to_zh(self, settings: Settings) -> None:
        async def broken(text: str) -> str:
            raise RuntimeError("翻译服务不可用")

        service = WarningService(Tracer(), settings=settings, translator=broken)
        draft = await service.generate(verdict())
        assert draft.translation_pending is True
        assert draft.record.body_bo is None
        assert draft.record.body_zh  # 中文正文仍可用，发布不阻断

    async def test_none_level_not_marked_pending(self, settings: Settings) -> None:
        service = WarningService(Tracer(), settings=settings)
        draft = await service.generate(verdict(RiskLevel.NONE))
        assert draft.translation_pending is False
