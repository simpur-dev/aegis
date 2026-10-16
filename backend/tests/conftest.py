"""测试夹具：所有测试共用一套可复现的内存总线平台。"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import timedelta
from typing import Any

import pytest

from aegis.bus.gateway import AgentGateway
from aegis.bus.inmemory import InMemoryBus
from aegis.bus.registry import AgentRegistry
from aegis.config import Settings, get_settings
from aegis.container import PlatformContainer, create_container
from aegis.contracts import ContractRegistry
from aegis.domain.chain import ChainResult
from aegis.domain.enums import AgentType, HazardType, RiskLevel
from aegis.domain.messages import TelemetryReading, now_iso, utc_now
from aegis.observability.tracer import Tracer
from aegis.pipeline.stages import HazardStages
from aegis.pipeline.workflow_chain import ChainStations, WorkflowChain
from aegis.workflow.engine import WorkflowEngine
from aegis.workflow.nodes import WorkflowServices
from aegis.workflow.templates import register_standard_chain_template

# 测试进程一律不许吃本机 `.env`：那是部署面，里面可能有真密钥与真模型路由。
# 光把 Settings 的 dotenv 源摘掉不够——实测 `graphiti_core` 在 import 时调 dotenv 的
# `find_dotenv()`，它从 site-packages 一路向上摸到仓库根那一份，把 AEGIS_LLM_* 整批灌进
# os.environ；此后任何 Settings 构造（连 `_env_file=None` 的也一样）都从进程环境读到它，
# "内置默认值"的断言就随机器状态漂（本机实测：assess 路由 qwen-plus → deepseek-chat）。
# 顺带这也挡住一件更贵的事：测试里谁要是构了一个不带假客户端的网关，泄漏进来的真 key
# 会让它拨号出网、花真钱。
Settings.model_config["env_file"] = None
get_settings.cache_clear()
# 只清 LLM/凭据面：CI 的 integration-nats 用 AEGIS_TEST_NATS_URL 传总线地址，那是夹具该留的。
for _key in [k for k in os.environ if k.startswith("AEGIS_LLM_") or k == "AEGIS_SERVICE_TOKEN"]:
    del os.environ[_key]
try:  # 装法没有 dotenv 可选依赖时，上面的清理已经挡住了本批泄漏
    import dotenv

    dotenv.load_dotenv = lambda *args, **kwargs: False
    dotenv.dotenv_values = lambda *args, **kwargs: {}
except ImportError:  # pragma: no cover
    pass

REGION = "540121"


def api_surface(app: Any) -> dict[str, set[str]]:
    """从装配好的 app 取"客户端看见的那张 API 表"：`{路径: {方法}}`，只含 `/api/` 之下。

    别改成遍历 `app.routes`：FastAPI 0.141 的 `include_router` 在路由表里留的是 `_IncludedRouter`
    包装（端点藏在 `original_router.routes` 里），直接遍历只能数出包装、数不出端点。
    那样"每条业务 API 都默认过闸""知识层只有一个写入口"这类门禁会数出比真实少的条数——
    覆盖面缩水而表现是全绿，正是要防的那种坏（P3 把端点搬进 routers/ 后实测：路由表从 51 条数成 15 条）。
    """
    return {
        path: {method.upper() for method in operations} for path, operations in app.openapi()["paths"].items() if path.startswith("/api/")
    }


async def chain_entry(
    stages: HazardStages,
    *,
    on_result: Callable[[ChainResult], Awaitable[None]] | None = None,
    settings: Settings | None = None,
) -> WorkflowChain:
    """给单测一个**生产同款**的链路入口：DAG 引擎 + 标准五段模板，段实现就是传进来的 `stages`。

    chain 退役后这是唯一一条编排路径，所以这里不提供"不用引擎的简化链路"作为第二选项：
    多留一条简化路径，就等于允许单测跑在生产根本不走的那条路上——那正是退役要消灭的东西。
    """
    stations = ChainStations(stages)
    engine = WorkflowEngine(services=WorkflowServices(run_chain_stage=stations))
    await register_standard_chain_template(engine, agent_txn_ms=int((settings or Settings(env="test")).sla_agent_txn_ms))
    return WorkflowChain(stages, engine, stations=stations, on_result=on_result)


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
    from aegis.domain.verdict import RiskVerdict

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
    from aegis.services.response.task_parser import TaskParser

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
