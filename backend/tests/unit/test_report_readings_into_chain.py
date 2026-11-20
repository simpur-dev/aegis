"""人工上报的读数要随链路进证据包（不再只喂规则轨）。

**问题**：解析器已经把上报里的雨量、位移等解析成 `TelemetryReading`（`source="report"`），
但上报接入腿 `ReportIntake.submit` 传给链路的是**空读数列表**。后果是两条腿看的是两批数据——
规则轨用上报的雨量判阈值，证据包里却没有 `rainfall_mm_h`，因子引擎于是说"缺必需因子"。

**为什么放在链路的 readings 里而不是落遥测库**：只有这一条链路看得到这次上报的观测，
不让一次人工上报污染同区域的其它链路（其它链路的证据包仍只来自真实遥测）。

这组用例钉住"上报的读数确实进了链路"以及"它们不会被当成模拟数据"。
"""

from __future__ import annotations

from typing import Any

import pytest

from aegis.domain.messages import TelemetryReading
from aegis.observability.tracer import Tracer
from aegis.pipeline.report_intake import ReportIntake
from aegis.services.interpret.semantic_parser import DisasterTextParser
from aegis.workflow.engine import WorkflowEngine

RAIN_REPORT = "24小时累计降雨95毫米，沟道泥位抬升1.2米"
TEN_MIN_REPORT = "10分钟降雨45毫米，短时强降雨"


class _RecordingChain:
    """记下链路收到的读数；不真跑链路。"""

    def __init__(self) -> None:
        self.readings: list[TelemetryReading] = []
        self.intake: str | None = None

    async def process(
        self,
        readings: list[TelemetryReading],
        *,
        region_code: str,
        preset_hits: Any = None,
        preset_verdict: Any = None,
        intake: str = "telemetry",
    ) -> Any:
        self.readings = list(readings)
        self.intake = intake
        from aegis.domain.chain import ChainResult

        return ChainResult(trace_id="trc_test", event_id="evt_test")


def _stats() -> dict[str, int]:
    """上报计数的初始形状（与容器装配时同一份）。"""
    return {"submitted": 0, "measured_by_rule": 0, "review_required": 0, "reviews_opened": 0}


@pytest.mark.asyncio
async def test_上报的读数随链路进去了() -> None:
    chain = _RecordingChain()
    intake = ReportIntake(
        parser=DisasterTextParser(),
        chain=chain,  # type: ignore[arg-type]
        tracer=Tracer(),
        workflow=WorkflowEngine(),
        report_stats=_stats(),
    )

    await intake.submit(note=TEN_MIN_REPORT, region_code="540121")

    assert chain.readings, "上报解析出了读数，却没随链路进去"
    metrics = {reading.metric for reading in chain.readings}
    assert "rain_10min" in metrics, f"上报里的雨量没进链路：读到的是 {metrics}"
    assert all(reading.source == "report" for reading in chain.readings)


@pytest.mark.asyncio
async def test_上报读数不被当成模拟数据() -> None:
    """`source="report"` 不在模拟来源集合里——人工上报是真实观测，不是演练数据。"""
    chain = _RecordingChain()
    intake = ReportIntake(
        parser=DisasterTextParser(),
        chain=chain,  # type: ignore[arg-type]
        tracer=Tracer(),
        workflow=WorkflowEngine(),
        report_stats=_stats(),
    )

    await intake.submit(note=TEN_MIN_REPORT, region_code="540121")

    assert chain.intake == "report"
    assert {reading.source for reading in chain.readings} == {"report"}


@pytest.mark.asyncio
async def test_上报的二十四小时累计雨量也进链路() -> None:
    """`rain_cumulative_24h` 与 `rain_10min` 是两个 metric，前者不进因子引擎（口径不同），
    但它进规则轨——两条腿看到的是同一批上报数据。"""
    chain = _RecordingChain()
    intake = ReportIntake(
        parser=DisasterTextParser(),
        chain=chain,  # type: ignore[arg-type]
        tracer=Tracer(),
        workflow=WorkflowEngine(),
        report_stats=_stats(),
    )

    await intake.submit(note=RAIN_REPORT, region_code="540121")

    metrics = {reading.metric for reading in chain.readings}
    assert "rain_cumulative_24h" in metrics
    assert "debris_level" in metrics
