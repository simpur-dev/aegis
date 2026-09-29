"""测试夹具：所有测试共用一套可复现的内存总线平台。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import pytest

from aegis.bus.gateway import AgentGateway, ContractRegistry
from aegis.bus.inmemory import InMemoryBus
from aegis.bus.registry import AgentRegistry
from aegis.config import Settings
from aegis.container import PlatformContainer, create_container
from aegis.domain.enums import AgentType, HazardType, RiskLevel
from aegis.domain.messages import TelemetryReading, now_iso, utc_now
from aegis.observability.tracer import Tracer

REGION = "540121"


@pytest.fixture
def settings() -> Settings:
    return Settings(
        env="test",
        bus_backend="memory",
        simulator_enabled=False,
        simulator_interval_seconds=0.05,
        heartbeat_interval_seconds=0.2,
        heartbeat_miss_limit=2,
        default_request_deadline_ms=1_500,
        delivery_mode="mock",
    )


@pytest.fixture
def contracts() -> ContractRegistry:
    return ContractRegistry()


@pytest.fixture
def tracer() -> Tracer:
    return Tracer()


@pytest.fixture
async def bus() -> AsyncIterator[InMemoryBus]:
    memory_bus = InMemoryBus()
    await memory_bus.connect()
    yield memory_bus
    await memory_bus.close()


@pytest.fixture
def registry(settings: Settings) -> AgentRegistry:
    return AgentRegistry(settings)


@pytest.fixture
def gateway(bus: InMemoryBus, registry: AgentRegistry, contracts: ContractRegistry, tracer: Tracer) -> AgentGateway:
    return AgentGateway(bus, registry, contracts, tracer, default_deadline_ms=1_500)


@pytest.fixture
async def container(settings: Settings) -> AsyncIterator[PlatformContainer]:
    ctn = create_container(settings, with_simulator=False)
    await ctn.start()
    yield ctn
    await ctn.shutdown()


def reading(
    metric: str,
    value: float,
    *,
    station_id: str = "RG-TEST-01",
    region_code: str = REGION,
    moment: Any = None,
    quality: str = "ok",
    unit: str = "mm",
) -> TelemetryReading:
    observed = (moment or utc_now()) - timedelta(seconds=30)
    return TelemetryReading(
        station_id=station_id,
        metric=metric,
        value=value,
        unit=unit,
        region_code=region_code,
        observed_at=observed.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        ingested_at=now_iso(),
        source="pytest",
        quality_flag=quality,
    )


def readings_rain_burst(**overrides: Any) -> list[TelemetryReading]:
    """泥石流触发组合：短时雨强 + 累计雨量 + 泥位。可用 overrides 覆盖单项以做边界测试。"""
    values = {
        "rain_10min": 42.0,
        "rain_cumulative_24h": 96.0,
        "debris_level": 1.6,
    }
    values.update(overrides)
    return [reading(metric, value, station_id=f"RG-{index:02d}") for index, (metric, value) in enumerate(values.items(), start=1)]


def registration(agent_type: AgentType, capability: str, *, agent_id: str | None = None) -> dict[str, Any]:
    return {
        "agent_id": agent_id or f"{agent_type.value}.test01",
        "agent_type": agent_type.value,
        "capabilities": [capability],
        "hazard_types": [h.value for h in HazardType if h is not HazardType.UNKNOWN],
        "max_concurrency": 4,
        "version": "test-1.0",
    }


def risk_payload(level: RiskLevel = RiskLevel.ORANGE, **overrides: Any) -> dict[str, Any]:
    payload = {
        "hazard_type": HazardType.DEBRIS_FLOW.value,
        "region_code": REGION,
        "risk_level": int(level),
        "confidence": 0.8,
        "rationale": "fixture 定级",
        "evidence_refs": ["R-DEBRIS-RAIN-1"],
    }
    payload.update(overrides)
    return payload


def sample_verdict(level: RiskLevel = RiskLevel.RED, hazard: HazardType = HazardType.DEBRIS_FLOW) -> Any:
    from aegis.domain.messages import TriggerHit
    from aegis.services.risk_engine import RiskVerdict

    hits = [
        TriggerHit(rule_id="R-DEBRIS-RAIN-1", hazard_type=hazard.value, region_code=REGION, score=0.9),
        TriggerHit(rule_id="R-DEBRIS-RAIN-2", hazard_type=hazard.value, region_code=REGION, score=0.8),
    ]
    return RiskVerdict(
        hazard_type=hazard,
        region_code=REGION,
        risk_level=level,
        confidence=0.85,
        rationale="fixture 定级依据",
        hits=hits,
    )


def sample_units(level: RiskLevel = RiskLevel.RED, hazard: HazardType = HazardType.DEBRIS_FLOW) -> list[Any]:
    from aegis.services.task_parser import TaskParser

    return TaskParser().parse(sample_verdict(level, hazard))


@pytest.fixture
def valid_message_payload() -> dict[str, Any]:
    import json

    from aegis.domain.messages import make_request

    message = make_request(
        source="platform.gateway",
        target="assess.test01",
        action="assess.hazard",
        payload={"region_code": REGION},
        trace_id="trc_" + "c" * 16,
        reply_to="reply.gw00000009.inbox",
        deadline_ms=1_000,
    )
    return json.loads(message.model_dump_json(exclude_none=True))


@pytest.fixture
def valid_stu_payload() -> dict[str, Any]:
    import json

    return json.loads(sample_units()[0].model_dump_json(exclude_none=True))
