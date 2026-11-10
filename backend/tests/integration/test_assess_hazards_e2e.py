"""因子引擎腿的真栈演练：平台真实证据包 → 总线 → 因子引擎定级结论。

这组用例证的是"链路通"而不是"结论准"：因子权重是 MOCK、公式是原型候选，
所以断言只钉契约与溯源（`source=hazard.factor_engine`、`factor_engine_mock`、`is_mock`、
置信度上限、人工核签），不拿等级高低当验收。数据不足的分支必须走错误通道，
不得产出"无风险"。

【它在链路里的位置】
因子引擎是**异构第二意见**：与平台主研判（LLM + 规则轨 + 双盲仲裁）并行跑的确定性腿。
它不产主结论，所以这里直接经总线派单给 `assess.factor_risk`，而不是从链路结论里读。
"""

from __future__ import annotations

import asyncio
import importlib
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("langgraph", reason="灾种模块依赖可选 extra agents")

from aegis.config import Settings
from aegis.connectors.simulator import HazardScenarioSimulator
from aegis.container import PlatformContainer, create_container
from aegis.domain.assess import AssessRequest
from aegis.domain.enums import Action, AgentType, HazardType
from aegis.domain.messages import utc_now
from aegis.domain.site import SiteProfile

DEMO_PROFILES = Path(__file__).resolve().parents[2] / "data" / "site_profiles.demo.jsonl"

TRACE = "trc_" + "9" * 16


class _RaisingRunnable:
    """节点一调用就失败，走各自的确定性兜底——测试不出网、不烧模型。"""

    def invoke(self, _messages: Any) -> Any:
        raise RuntimeError("测试替身：不发起外部模型请求")


class _NoNetworkLlm:
    def with_structured_output(self, _schema: Any) -> _RaisingRunnable:
        return _RaisingRunnable()


def _block_ext_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _NoNetworkLlm()
    for name in (
        "aegis.agents.hazards.risk_agent.nodes.risk_assess_node",
        "aegis.agents.hazards.risk_agent.nodes.risk_validate_node",
    ):
        module = importlib.import_module(name)
        monkeypatch.setattr(module, "get_risk_llm", lambda: fake)


async def _build_container() -> PlatformContainer:
    settings = Settings(
        env="test",
        bus_backend="memory",
        delivery_mode="mock",
        simulator_seed=99,
        agent_runtime="real",
    )
    container = create_container(settings, with_simulator=False)
    await container.start()
    await _await_registration(container)
    return container


async def _await_registration(container: PlatformContainer, *, count: int = 11, timeout_s: float = 3.0) -> None:
    """等注册帧被网关处理完。

    注册是**异步广播**：本体启动后把 `agent.register` 发出去，网关收到才把它记进
    注册表。派单早于这一步就会 `no_agent`——那是时序，不是"没有承接方"。
    """
    waited = 0.0
    while len(container.registry.snapshot()) < count and waited < timeout_s:
        await asyncio.sleep(0.02)
        await container.transport.idle()
        waited += 0.02


async def _load_demo_profiles(container: PlatformContainer) -> None:
    for line in DEMO_PROFILES.read_text(encoding="utf-8").splitlines():
        if line.strip():
            await container.store.upsert_site_profile(SiteProfile.model_validate_json(line))


def _surge(region: str) -> dict[str, list]:
    readings = HazardScenarioSimulator(scenario="surge", seed=99).collect_at(utc_now())
    return {region: [r for r in readings if r.region_code == region]}


async def _dispatch_factor(container: PlatformContainer, region: str, *, hazard: HazardType) -> Any:
    """用平台真实数据面造证据包，经总线派给因子引擎。"""
    assert container.assess_evidence is not None
    readings = _surge(region)[region]
    evidence = await container.assess_evidence.build(region=region, hits=[], readings=readings, intake="telemetry")
    request = AssessRequest(
        region_code=region,
        window="60m",
        hazard_type=hazard,
        evidence=evidence,
    )
    return await container.gateway.dispatch(
        AgentType.ASSESS,
        Action.ASSESS_FACTOR_RISK,
        request.model_dump(mode="json"),
        trace_id=TRACE,
        capability="factor_risk_assess",
    )


async def test_因子引擎经总线产出带溯源的一等结论(monkeypatch: pytest.MonkeyPatch) -> None:
    _block_ext_llm(monkeypatch)
    container = await _build_container()
    try:
        await _load_demo_profiles(container)
        reply = await _dispatch_factor(container, "540121", hazard=HazardType.DEBRIS_FLOW)

        payload = reply.payload
        assert payload["source"] == "hazard.factor_engine"
        assert payload["assessed_by"] == "assess.factor01"
        assert payload["hazard_type"] == HazardType.DEBRIS_FLOW.value
        assert payload["region_code"] == "540121"
        provenance = payload["data_provenance"]
        assert isinstance(provenance, dict)
        assert provenance["method"] == "factor_engine_mock"
        assert provenance["is_mock"] is True
        assert provenance["disclaimer"], "demo 级结论必须带使用限制"
        assert 0.0 < float(payload["confidence"]) <= 0.6
        assert payload["human_review_suggested"] is True
        assert payload["confidence_basis"]
        assert payload["status"] == "success"

        assessment = payload["assessment"]
        assert isinstance(assessment, dict) and assessment
        assert assessment["coverage_basis"] == "weighted_factor_coverage"
    finally:
        await container.shutdown()


async def test_因子引擎拒单时走错误通道且不产无风险结论(monkeypatch: pytest.MonkeyPatch) -> None:
    """540221：雨强与冰湖规则同时命中、score 同为 1.0，本地定级取最严重的冰湖溃决。

    因子引擎拒单时会用请求灾种组织"数据不足以支撑 X 研判"——请求若按灾种声明顺序
    改发泥石流，台账里就会出现"结论冰湖溃决、降级文本泥石流"两个灾种同屏。
    """
    _block_ext_llm(monkeypatch)
    container = await _build_container()
    try:
        await _load_demo_profiles(container)
        with pytest.raises(Exception) as info:
            await _dispatch_factor(container, "540221", hazard=HazardType.LAKE_OUTBURST)
        message = str(getattr(info.value, "message", "") or info.value)
        assert "E_ASSESS" in str(getattr(info.value, "detail", {})) or "研判" in message
        assert "泥石流" not in message
    finally:
        await container.shutdown()


async def test_证据构建失败时研判降级且不产无风险结论(monkeypatch: pytest.MonkeyPatch) -> None:
    _block_ext_llm(monkeypatch)
    container = await _build_container()
    try:
        await _load_demo_profiles(container)

        def _boom(**_kwargs: Any) -> Any:
            raise RuntimeError("取证坏了（测试注入）")

        assert container.assess_evidence is not None
        monkeypatch.setattr(container.assess_evidence, "build", _boom)

        results = await container.chain.process_many(_surge("540121"))
        assert len(results) == 1
        result = results[0]
        assert result.verdict is not None
        modes = {stage.name: stage.mode for stage in result.stages}
        assert modes["assess"] == "local", result.as_dict()["stages"]
        assert any("研判证据构建降级" in item for item in result.degradations), result.degradations
        # 本地兜底也必须带出处：证据没建成时是 unknown，不是 observed
        payload = result.verdict.as_payload()
        assert payload["data_provenance"]["data_kind"] == "unknown"
        assert payload["assessed_by"] != "assess.factor01"
    finally:
        await container.shutdown()
