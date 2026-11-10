"""研判段真栈内存演练：平台证据包 → ext 统一契约响应 → 链路结论与台账。

这组用例证的是"链路通"而不是"结论准"：外部因子权重是 MOCK、公式是原型候选，
所以断言只钉契约与溯源（`assess.v1`、`factor_engine_mock`、`is_mock`、置信度上限、人工核签），
不拿等级高低当验收。数据不足的分支必须走降级轨，不得产出"无风险"。
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("langgraph", reason="外部实现依赖可选 extra agents_ext")

from aegis.config import Settings
from aegis.connectors.simulator import HazardScenarioSimulator
from aegis.container import PlatformContainer, create_container
from aegis.domain.messages import utc_now
from aegis.domain.site import SiteProfile

DEMO_PROFILES = Path(__file__).resolve().parents[2] / "data" / "site_profiles.demo.jsonl"


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
        agent_impl="ext",
    )
    container = create_container(settings, with_simulator=False)
    await container.start()
    return container


async def _load_demo_profiles(container: PlatformContainer) -> None:
    for line in DEMO_PROFILES.read_text(encoding="utf-8").splitlines():
        if line.strip():
            await container.store.upsert_site_profile(SiteProfile.model_validate_json(line))


def _surge(region: str) -> dict[str, list]:
    readings = HazardScenarioSimulator(scenario="surge", seed=99).collect_at(utc_now())
    return {region: [r for r in readings if r.region_code == region]}


async def test_ext_研判段产出带溯源的一等结论(monkeypatch: pytest.MonkeyPatch) -> None:
    _block_ext_llm(monkeypatch)
    container = await _build_container()
    try:
        await _load_demo_profiles(container)
        results = await container.chain.process_many(_surge("540121"))
        assert len(results) == 1
        result = results[0]
        assert result.ok, result.as_dict()
        assert result.verdict is not None

        modes = {stage.name: stage.mode for stage in result.stages}
        assert modes["assess"] == "agent", result.as_dict()["stages"]
        assert not any("E_ASSESS" in item for item in result.degradations), result.degradations

        payload = result.verdict.as_payload()
        assert payload["assessed_by"] == "assess.factor01"
        provenance = payload["data_provenance"]
        assert isinstance(provenance, dict)
        assert provenance["method"] == "factor_engine_mock"
        assert provenance["is_mock"] is True
        assert provenance["disclaimer"], "demo 级结论必须带使用限制"
        assert 0.0 < float(payload["confidence"]) <= 0.6
        assert payload["human_review_suggested"] is True
        assert payload["confidence_basis"]

        assessment = payload["assessment"]
        assert isinstance(assessment, dict) and assessment
        assert assessment["coverage_basis"] == "weighted_factor_coverage"

        # 台账里是同一份 payload（chain_runs.risk 原样存 as_payload）
        ledger = [row.as_dict() for row in container.store.chains.snapshot()]
        match = [row for row in ledger if row["trace_id"] == result.trace_id]
        assert match and match[0]["risk"]["data_provenance"]["method"] == "factor_engine_mock"
    finally:
        await container.shutdown()


async def test_外部拒单的降级文本与本地灾种一致(monkeypatch: pytest.MonkeyPatch) -> None:
    """540221：雨强与冰湖规则同时命中、score 同为 1.0，本地定级取最严重的冰湖溃决。

    外部实现拒单时会用请求灾种组织"数据不足以支撑 X 研判"——请求若按灾种声明顺序
    改发泥石流，台账里就会出现"结论冰湖溃决、降级文本泥石流"两个灾种同屏。
    """
    _block_ext_llm(monkeypatch)
    container = await _build_container()
    try:
        await _load_demo_profiles(container)
        results = await container.chain.process_many(_surge("540221"))
        assert len(results) == 1
        result = results[0]
        assert result.ok, result.as_dict()
        assert result.verdict is not None
        assert result.verdict.hazard_type.value == "lake_outburst"
        assert all("泥石流" not in note for note in result.degradations), result.degradations
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
