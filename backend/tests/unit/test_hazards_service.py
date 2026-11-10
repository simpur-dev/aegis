"""外部研判服务（`RiskAssessAgent`）：统一契约进、统一契约出、错误走类型化通道。

覆盖：
- 合法请求 → `AssessResponse` 往返（含出处/置信度/人工核签标记）；
- 数据不足（无证据 / 空观测 / 冰湖无扳机）→ `E_ASSESS_INSUFFICIENT`，retryable=False；
- 灾种不支持 → `E_ASSESS_HAZARD_UNSUP`；畸形请求 / 错误动作 / 区域不一致 → `E_ASSESS_BAD_INPUT`；
- 图内部异常 → `E_ASSESS_FAILED`（retryable=True）；
- main 的状态四态 → 错误码映射（uncertain / failed / 档位不可映射）；
- 拉取腿：请求无 evidence 时按环境变量拉取，合法证据被采用、坏 JSON 视为失败。
"""

from __future__ import annotations

import asyncio
import importlib
import json
import threading
import time
from typing import Any

import pytest

from aegis.agents.hazards.risk_agent import agent as agent_module
from aegis.agents.hazards.risk_agent.agent import RiskAssessAgent
from aegis.agents.hazards.risk_agent.errors import AssessRejected
from aegis.agents.hazards.risk_agent.evidence_pull import DATA_API_TOKEN_ENV, DATA_API_URL_ENV
from aegis.agents.hazards.risk_agent.main import run_risk_agent
from aegis.domain.assess import AssessErrorCode, AssessEvidence, AssessRequest, AssessResponse, FactorObservation
from aegis.domain.enums import Action, AgentType, HazardType, RiskLevel
from aegis.domain.messages import make_request

TS = "2024-07-08T03:00:00.000Z"
REGION = "540121"
TRACE = "trc_" + "7" * 16


# ============================================================
# 夹具与构造
# ============================================================


class _NullTransport:
    name = "null"

    async def publish(self, *args: Any, **kwargs: Any) -> None: ...


class _BrokenStructured:
    def invoke(self, _messages: Any) -> Any:
        raise RuntimeError("测试禁网：LLM 不可用")


class _BrokenLlm:
    def with_structured_output(self, _schema: Any, **_kw: Any) -> _BrokenStructured:
        return _BrokenStructured()


@pytest.fixture(autouse=True)
def _no_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    for module_name in (
        "aegis.agents.hazards.risk_agent.nodes.risk_assess_node",
        "aegis.agents.hazards.risk_agent.nodes.risk_validate_node",
    ):
        monkeypatch.setattr(importlib.import_module(module_name), "get_risk_llm", lambda: _BrokenLlm())


def _agent(*, pool_workers: int = 1) -> RiskAssessAgent:
    return RiskAssessAgent(
        transport=_NullTransport(),  # type: ignore[arg-type]
        agent_type=AgentType.ASSESS,
        agent_id="assess.factor01",
        capabilities=["risk_assess"],
        pool_workers=pool_workers,
    )


def _ok_response(request: AssessRequest) -> AssessResponse:
    return AssessResponse(
        hazard_type=HazardType.DEBRIS_FLOW,
        region_code=request.region_code,
        risk_level=RiskLevel.YELLOW,
        confidence=0.4,
        confidence_basis="coverage=0.60;validation=pass;method=factor_engine_mock;cap=0.60",
        source="hazard.factor_engine",
        assessed_by="assess.factor01",
    )


def _obs(factor: str, value: float | str) -> FactorObservation:
    common = {"factor": factor, "samples": 2, "observed_at": TS, "sources": ["test"], "derivation": "test"}
    if isinstance(value, str):
        return FactorObservation(class_value=value, **common)
    return FactorObservation(min_value=value, max_value=value, mean_value=value, **common)


def _debris_observations(*, rainfall: float = 65.0) -> list[FactorObservation]:
    values: dict[str, float | str] = {
        "rainfall_mm_h": rainfall,
        "catchment_area_km2": 12.0,
        "channel_gradient": 0.35,
        "sediment_supply_index": 0.7,
        "water_source_type": "rainstorm",
        "air_temp_c": 18.0,
        "snow_depth_cm": 0.0,
        "upstream_breach_flag": "no",
        "spring_surge_flag": "no",
    }
    return [_obs(name, value) for name, value in values.items()]


def _evidence(*, region: str = REGION, observations: list[FactorObservation] | None = None) -> AssessEvidence:
    return AssessEvidence(
        region_code=region,
        window="60m",
        generated_at=TS,
        observations=_debris_observations() if observations is None else observations,
        provenance={"data_kind": "observed", "sources": ["platform.telemetry:mqtt"], "method": "telemetry_rule"},
    )


def _request(
    payload: dict[str, Any] | None = None,
    *,
    action: str = Action.ASSESS_HAZARD.value,
    target: str = "assess.factor01",
) -> Any:
    return make_request(
        source="platform.gateway",
        target=target,
        action=action,
        payload=payload or {},
        trace_id=TRACE,
        reply_to="reply.gw0000000a.inbox",
        deadline_ms=5000,
    )


def _payload(*, evidence: AssessEvidence | None = None, hazard: str = "debris_flow", region: str = REGION) -> dict[str, Any]:
    request = AssessRequest(
        region_code=region,
        window="60m",
        hazard_type=hazard,
        evidence=_evidence(region=region) if evidence is None else evidence,
    )
    return request.model_dump(mode="json")


# ============================================================
# 正常路径
# ============================================================


async def test_happy_path_returns_contract_response() -> None:
    """合法泥石流请求 → 合法 AssessResponse；区域/灾种回显，出处为 mock 方法。"""
    reply = await _agent().handle(_request(_payload()))
    response = AssessResponse.model_validate(reply.payload)
    assert response.region_code == REGION
    assert response.hazard_type == HazardType.DEBRIS_FLOW
    assert response.source == "hazard.factor_engine"
    assert response.data_provenance.method == "factor_engine_mock"
    assert response.data_provenance.is_mock is True
    assert 0 < response.confidence <= 0.60
    assert response.confidence_basis
    assert response.human_review_suggested is True
    assert response.double_blind == "not_applicable"
    assert response.assessment["formula"]["status"] == "prototype_candidate"


async def test_all_supported_hazards_use_the_same_contract() -> None:
    """五个受支持灾种只有两种结局：合法 AssessResponse，或 E_ASSESS_INSUFFICIENT 拒绝。

    没有第三种形状，也没有"未知灾种"的旁路——这条钉住"五灾种全部走统一 DTO"。
    """
    from aegis.agents.hazards.hazard_registry import SUPPORTED_HAZARDS

    outcomes: dict[str, str] = {}
    for hazard in SUPPORTED_HAZARDS:
        try:
            reply = await _agent().handle(_request(_payload(hazard=hazard)))
        except AssessRejected as exc:
            assert exc.error_code == AssessErrorCode.INSUFFICIENT, (hazard, exc.error_code, exc.message)
            outcomes[hazard] = "insufficient"
        else:
            response = AssessResponse.model_validate(reply.payload)
            assert response.hazard_type.value == hazard
            assert 0 < response.confidence <= 0.60
            outcomes[hazard] = "response"
    assert outcomes["debris_flow"] == "response"
    assert outcomes["lake_outburst"] == "insufficient"


# ============================================================
# 拒绝路径
# ============================================================


async def test_wrong_action_rejected() -> None:
    """只处理 assess.hazard；别的动作 → E_ASSESS_BAD_INPUT。"""
    with pytest.raises(AssessRejected) as exc_info:
        await _agent().handle(_request(_payload(), action=Action.PLAN_STU.value))
    assert exc_info.value.error_code == AssessErrorCode.BAD_INPUT


async def test_malformed_payload_rejected() -> None:
    """畸形请求（缺 region/window）→ E_ASSESS_BAD_INPUT，detail 带字段错误。"""
    with pytest.raises(AssessRejected) as exc_info:
        await _agent().handle(_request({"hazard_type": "debris_flow"}))
    assert exc_info.value.error_code == AssessErrorCode.BAD_INPUT
    assert exc_info.value.error_detail.get("errors")


async def test_no_evidence_and_no_pull_config_insufficient(monkeypatch: pytest.MonkeyPatch) -> None:
    """请求无证据且未配置拉取 → E_ASSESS_INSUFFICIENT，绝不猜。"""
    monkeypatch.delenv(DATA_API_URL_ENV, raising=False)
    payload = _payload()
    payload.pop("evidence")
    with pytest.raises(AssessRejected) as exc_info:
        await _agent().handle(_request(payload))
    assert exc_info.value.error_code == AssessErrorCode.INSUFFICIENT
    assert exc_info.value.retryable is False


async def test_empty_observations_insufficient() -> None:
    """证据包存在但没有观测 → E_ASSESS_INSUFFICIENT（不是"无风险"）。"""
    with pytest.raises(AssessRejected) as exc_info:
        await _agent().handle(_request(_payload(evidence=_evidence(observations=[]))))
    assert exc_info.value.error_code == AssessErrorCode.INSUFFICIENT


async def test_lake_outburst_without_triggers_insufficient() -> None:
    """冰湖只给 P 组（库容/水位比/坝高/冰川），T 组扳机全缺 → 诚实拒绝，不产"无风险"。"""
    observations = [
        _obs("lake_volume_m3", 25_000_000.0),
        _obs("water_level_ratio", 0.85),
        _obs("dam_height_m", 80.0),
        _obs("glacier_activity", "active"),
        _obs("downstream_distance_km", 12.0),
    ]
    payload = _payload(evidence=_evidence(observations=observations), hazard="lake_outburst")
    with pytest.raises(AssessRejected) as exc_info:
        await _agent().handle(_request(payload))
    assert exc_info.value.error_code == AssessErrorCode.INSUFFICIENT
    assert exc_info.value.retryable is False


async def test_unsupported_hazard_rejected() -> None:
    """枚举合法但外部实现不支持的灾种（地震触发）→ E_ASSESS_HAZARD_UNSUP。"""
    with pytest.raises(AssessRejected) as exc_info:
        await _agent().handle(_request(_payload(hazard="quake_triggered")))
    assert exc_info.value.error_code == AssessErrorCode.HAZARD_UNSUP


async def test_evidence_region_mismatch_rejected() -> None:
    """证据区域与请求不一致 → E_ASSESS_BAD_INPUT（防止拿别处的数据顶账）。"""
    payload = _payload(evidence=_evidence(region="540999"))
    with pytest.raises(AssessRejected) as exc_info:
        await _agent().handle(_request(payload))
    assert exc_info.value.error_code == AssessErrorCode.BAD_INPUT


async def test_graph_exception_maps_to_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    """图内部异常 → E_ASSESS_FAILED，retryable=True（重试或许能恢复）。"""

    def boom(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("图炸了")

    monkeypatch.setattr(agent_module, "run_risk_agent", boom)
    with pytest.raises(AssessRejected) as exc_info:
        await _agent().handle(_request(_payload()))
    assert exc_info.value.error_code == AssessErrorCode.FAILED
    assert exc_info.value.retryable is True


async def test_runner_region_mismatch_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """runner 返回的区域与请求不一致 → E_ASSESS_BAD_INPUT。"""

    def wrong_region(_req: AssessRequest, _evidence: AssessEvidence, **_kwargs: Any) -> AssessResponse:
        return AssessResponse(
            hazard_type=HazardType.DEBRIS_FLOW,
            region_code="540999",
            risk_level=RiskLevel.YELLOW,
            confidence=0.4,
            confidence_basis="coverage=0.60;validation=pass;method=factor_engine_mock;cap=0.60",
            source="hazard.factor_engine",
            assessed_by="assess.factor01",
        )

    monkeypatch.setattr(agent_module, "run_risk_agent", wrong_region)
    with pytest.raises(AssessRejected) as exc_info:
        await _agent().handle(_request(_payload()))
    assert exc_info.value.error_code == AssessErrorCode.BAD_INPUT


# ============================================================
# main：状态四态 → 错误码
# ============================================================


class _FakeGraph:
    def __init__(self, state: dict) -> None:
        self._state = state

    def invoke(self, _state: dict) -> dict:
        return self._state


def _patch_graph(monkeypatch: pytest.MonkeyPatch, state: dict) -> None:
    target = importlib.import_module("aegis.agents.hazards.risk_agent.main")
    monkeypatch.setattr(target, "create_risk_agent", lambda: _FakeGraph(state))


def _state(**overrides: Any) -> dict:
    base = {
        "status": "success",
        "risk_result": {"risk_level": "medium", "reasoning": "r", "assessment": {"rule_hits": [], "factor_scores": {}}},
        "confidence": 0.4,
        "confidence_basis": "coverage=0.60;validation=pass;method=factor_engine_mock;cap=0.60",
        "data_provenance": {},
    }
    base.update(overrides)
    return base


def test_status_uncertain_maps_to_uncertain(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_graph(monkeypatch, _state(status="uncertain"))
    with pytest.raises(AssessRejected) as exc_info:
        run_risk_agent(AssessRequest(region_code=REGION, window="60m", hazard_type="debris_flow"), _evidence())
    assert exc_info.value.error_code == AssessErrorCode.UNCERTAIN
    assert exc_info.value.retryable is True


def test_status_failed_maps_to_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_graph(monkeypatch, _state(status="failed"))
    with pytest.raises(AssessRejected) as exc_info:
        run_risk_agent(AssessRequest(region_code=REGION, window="60m", hazard_type="debris_flow"), _evidence())
    assert exc_info.value.error_code == AssessErrorCode.FAILED


def test_unmappable_level_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """内部档位无法映射（unknown）→ E_ASSESS_BAD_LEVEL，绝不做假定级。"""
    _patch_graph(monkeypatch, _state(risk_result={"risk_level": "unknown", "reasoning": "r", "assessment": {}}))
    with pytest.raises(AssessRejected) as exc_info:
        run_risk_agent(AssessRequest(region_code=REGION, window="60m", hazard_type="debris_flow"), _evidence())
    assert exc_info.value.error_code == AssessErrorCode.BAD_LEVEL


# ============================================================
# 拉取腿
# ============================================================


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *_exc: Any) -> bool:
        return False


async def test_pull_leg_used_when_request_has_no_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    """请求无 evidence：按 AEGIS_EXT_DATA_API_URL 拉取；合法证据被采用。"""
    pull_module = importlib.import_module("aegis.agents.hazards.risk_agent.evidence_pull")
    calls: list[Any] = []
    body = _evidence().model_dump(mode="json")

    def fake_urlopen(request: Any, timeout: float = 0) -> _FakeResponse:
        calls.append((request, timeout))
        return _FakeResponse(json.dumps(body).encode("utf-8"))

    monkeypatch.setenv(DATA_API_URL_ENV, "http://platform.test")
    monkeypatch.setenv(DATA_API_TOKEN_ENV, "tok")
    monkeypatch.setattr(pull_module, "urlopen", fake_urlopen)

    payload = _payload()
    payload.pop("evidence")
    reply = await _agent().handle(_request(payload))
    response = AssessResponse.model_validate(reply.payload)
    assert response.region_code == REGION
    assert calls, "必须真的走过拉取腿"
    request, timeout = calls[0]
    assert "region_code=540121" in request.full_url and "window_minutes=60" in request.full_url
    assert request.get_header("Authorization") == "Bearer tok"
    assert timeout == 2.0


async def test_pull_leg_invalid_json_is_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """拉回来的不是合法证据 → 视为拉取失败 → E_ASSESS_INSUFFICIENT。"""
    pull_module = importlib.import_module("aegis.agents.hazards.risk_agent.evidence_pull")
    monkeypatch.setenv(DATA_API_URL_ENV, "http://platform.test")
    monkeypatch.setattr(pull_module, "urlopen", lambda *_a, **_k: _FakeResponse(b"not-json"))

    payload = _payload()
    payload.pop("evidence")
    with pytest.raises(AssessRejected) as exc_info:
        await _agent().handle(_request(payload))
    assert exc_info.value.error_code == AssessErrorCode.INSUFFICIENT


async def test_pull_leg_wrong_region_is_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """拉回来的证据区域不一致 → 视为失败（不拿别处数据顶账）。"""
    pull_module = importlib.import_module("aegis.agents.hazards.risk_agent.evidence_pull")
    body = _evidence(region="540999").model_dump(mode="json")
    monkeypatch.setenv(DATA_API_URL_ENV, "http://platform.test")
    monkeypatch.setattr(pull_module, "urlopen", lambda *_a, **_k: _FakeResponse(json.dumps(body).encode("utf-8")))

    payload = _payload()
    payload.pop("evidence")
    with pytest.raises(AssessRejected) as exc_info:
        await _agent().handle(_request(payload))
    assert exc_info.value.error_code == AssessErrorCode.INSUFFICIENT


# ============================================================
# 专用线程池（#5：不与事件循环默认执行器抢线程）
# ============================================================


def test_pool_workers_must_be_positive() -> None:
    """容量 0 在构造期就拒绝，而不是等第一条请求才发现起不来池。"""
    with pytest.raises(ValueError, match="≥1"):
        _agent(pool_workers=0)


def test_build_hazard_agents_carries_pool_workers() -> None:
    """装配层传入的容量必须落到实例上（否则又是个拧不动的空旋钮）。"""
    from aegis.agents.hazards.runtime import build_hazard_agents

    agents = build_hazard_agents(_NullTransport(), pool_workers=3)  # type: ignore[arg-type]
    assert agents[0].pool_workers == 3  # type: ignore[union-attr]


async def test_dedicated_pool_bounds_concurrent_graph_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    """池容量即并发上限：6 个请求、池 2 → 峰值 2，且线程来自 ext 专用池。"""
    active = 0
    peak = 0
    names: set[str] = set()
    lock = threading.Lock()

    def slow_runner(request: AssessRequest, _evidence: AssessEvidence, **_kwargs: Any) -> AssessResponse:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            names.add(threading.current_thread().name)
        time.sleep(0.05)
        with lock:
            active -= 1
        return _ok_response(request)

    monkeypatch.setattr(agent_module, "run_risk_agent", slow_runner)
    agent = _agent(pool_workers=2)
    try:
        replies = await asyncio.gather(*(agent.handle(_request(_payload())) for _ in range(6)))
    finally:
        await agent.stop()

    assert len(replies) == 6
    assert peak == 2, f"专用池容量 2 时并发峰值应为 2，实际 {peak}"
    assert names and all(name.startswith("aegis-ext-assess") for name in names), f"请求跑到了别的执行器上: {names}"


async def test_pool_propagates_request_context(monkeypatch: pytest.MonkeyPatch) -> None:
    """专用池像 asyncio.to_thread 一样带上下文进线程：日志 trace_id 不丢。"""
    from aegis.logging import trace_id_var

    seen: list[str | None] = []

    def runner(request: AssessRequest, _evidence: AssessEvidence, **_kwargs: Any) -> AssessResponse:
        seen.append(trace_id_var.get())
        return _ok_response(request)

    monkeypatch.setattr(agent_module, "run_risk_agent", runner)
    token = trace_id_var.set("trc_pool_propagation")
    agent = _agent(pool_workers=1)
    try:
        await agent.handle(_request(_payload()))
    finally:
        trace_id_var.reset(token)
        await agent.stop()

    assert seen == ["trc_pool_propagation"]


async def test_stop_shuts_down_the_dedicated_pool() -> None:
    """stop() 之后池不再接任务：停机后没有新研判能排进来。"""
    agent = _agent(pool_workers=1)
    await agent.handle(_request(_payload()))
    await agent.stop()

    with pytest.raises(RuntimeError, match="shutdown"):
        agent._pool.submit(int)  # type: ignore[attr-defined]
