"""灾种闭环模块（`aegis.agents.hazards`）过《智能体接入规范》§7 的一致性门禁。

【本文件在测什么】
  适配层已删除：灾种模块与平台内环/mock 平级，直接吃平台契约。
  本文件钉住"它在总线上的行为与平台自建内环同一套口径"：

  - 注册面：六个本体全部注册、**能力互不重叠**（每项能力恰好一个主人）；
  - 回执面：合法 response / 合法 error，全部过 `agent_message.v1`；
  - 生命周期：幂等、超时、错误回执、心跳摘除；
  - 数据边界：只经总线与请求载荷取数，不直连存储层；
  - 契约面：灾种键就是平台键（旧键只在别名表里）；五个受支持灾种；地震拒绝；
  - 清退面：`adapter` / `contract_face` / `levels` 三个遗留模块必须已不存在。

【LLM 隔离】
  图里的解释/验证节点用 autouse fixture 换成"必抛"桩：模板/规则兜底本就是设计的一部分，
  门禁不拨号、不花钱、不依赖网络。
"""

from __future__ import annotations

import ast
import asyncio
import importlib
import inspect
import json
import time
from pathlib import Path
from typing import Any

import pytest

from aegis.agents.factory import build_hazard_agents
from aegis.agents.hazards.hazard_registry import (
    HAZARD_ALIAS,
    SUPPORTED_HAZARDS,
    UnsupportedHazardError,
    normalize_hazard_type,
    require_hazard,
)
from aegis.agents.hazards.risk_agent import agent as agent_module
from aegis.agents.hazards.risk_agent.agent import FactorAssessAgent
from aegis.bus import subjects
from aegis.bus.gateway import AgentGateway
from aegis.bus.inmemory import InMemoryBus
from aegis.bus.registry import AgentRegistry
from aegis.domain.assess import AssessErrorCode, AssessEvidence, AssessRequest, AssessResponse, FactorObservation
from aegis.domain.enums import Action, AgentType, HazardType, MessageKind, RiskLevel
from aegis.domain.messages import AgentMessage, make_request
from aegis.errors import AegisError, NoCapableAgentError

TRACE = "trc_" + "5" * 16
REGION = "540121"
TS = "2024-07-08T03:00:00.000Z"

#: 外部实现的包根：门禁 6 的静态检查扫这里。
EXT_PACKAGE = Path(__file__).resolve().parents[2] / "src" / "aegis" / "agents" / "hazards"


# ============================================================
# 夹具与构造
# ============================================================


class _BrokenStructured:
    def invoke(self, _messages: Any) -> Any:
        raise RuntimeError("门禁禁网：LLM 不可用（走模板/规则兜底）")


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


def _obs(factor: str, value: float | str) -> FactorObservation:
    common = {"factor": factor, "samples": 2, "observed_at": TS, "sources": ["gate"], "derivation": "gate"}
    if isinstance(value, str):
        return FactorObservation(class_value=value, **common)
    return FactorObservation(min_value=value, max_value=value, mean_value=value, **common)


DEBRIS_VALUES: dict[str, float | str] = {
    "rainfall_mm_h": 65.0,
    "catchment_area_km2": 12.0,
    "channel_gradient": 0.35,
    "sediment_supply_index": 0.7,
    "water_source_type": "rainstorm",
    "air_temp_c": 18.0,
    "snow_depth_cm": 0.0,
    "upstream_breach_flag": "no",
    "spring_surge_flag": "no",
}


def _evidence(observations: list[FactorObservation] | None = None, *, region: str = REGION) -> AssessEvidence:
    return AssessEvidence(
        region_code=region,
        window="60m",
        generated_at=TS,
        observations=[_obs(name, value) for name, value in DEBRIS_VALUES.items()] if observations is None else observations,
        provenance={"data_kind": "observed", "sources": ["platform.telemetry:mqtt"], "method": "telemetry_rule"},
    )


def _assess_payload(
    *,
    hazard: str = "debris_flow",
    evidence: AssessEvidence | None = None,
    hits: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    request = AssessRequest(
        region_code=REGION,
        window="60m",
        hazard_type=hazard,
        evidence=_evidence() if evidence is None else evidence,
        hits=hits or [],
    )
    return request.model_dump(mode="json")


def _valid_response(request: AssessRequest) -> AssessResponse:
    return AssessResponse(
        hazard_type=request.hazard_type if request.hazard_type is not None else HazardType.DEBRIS_FLOW,
        region_code=request.region_code,
        risk_level=RiskLevel.YELLOW,
        confidence=0.44,
        confidence_basis="coverage=0.60;validation=pass;method=factor_engine_mock;cap=0.60",
        rationale="门禁桩：因子引擎输出 P/T/R",
        evidence_refs=["factor:rainfall_mm_h"],
        assessed_by="assess.factor01",
        human_review_suggested=True,
        source="hazard.factor_engine",
        data_provenance={
            "data_kind": "observed",
            "sources": ["platform.telemetry:mqtt"],
            "method": "factor_engine_mock",
            "is_mock": True,
        },
        assessment={"formula": {"expression": "R_h(t) = 100 × P_h × T_h(t)", "status": "prototype_candidate"}},
    )


def _fake_runner(*, delay_s: float = 0.0, exc: BaseException | None = None) -> Any:
    """替最里层那一腿（LangGraph + LLM）；服务壳、总线、网关全是真身。"""

    def runner(request: AssessRequest, _evidence: AssessEvidence, **_kwargs: Any) -> AssessResponse:
        if delay_s:
            time.sleep(delay_s)
        if exc is not None:
            raise exc
        return _valid_response(request)

    return runner


async def bring_up_ext(bus: InMemoryBus, gateway: AgentGateway) -> FactorAssessAgent:
    """拉起灾种模块的六个本体，只把因子引擎这一个启起来做门禁的被测对象。"""
    agents = build_hazard_agents(bus)
    assert len(agents) == 6, "灾种模块有六个本体：数据质量门/因子引擎/情景仿真/预警素材/链内审计/闭环"
    agent = next(a for a in agents if isinstance(a, FactorAssessAgent))
    await gateway.start()
    await agent.start(heartbeat_interval=0.05)
    await bus.idle()
    await asyncio.sleep(0.01)
    await bus.idle()
    return agent


async def collect_receipts(bus: InMemoryBus, request: AgentMessage) -> list[AgentMessage]:
    receipts: list[AgentMessage] = []

    async def collector(message: AgentMessage) -> None:
        receipts.append(message)

    await bus.subscribe(request.reply_to or "", collector)
    return receipts


async def wait_for_receipts(bus: InMemoryBus, receipts: list[AgentMessage], *, count: int = 1, timeout_s: float = 3.0) -> None:
    waited = 0.0
    while len(receipts) < count and waited < timeout_s:
        await asyncio.sleep(0.02)
        await bus.idle()
        waited += 0.02


async def settle(bus: InMemoryBus, seconds: float = 0.15) -> None:
    await asyncio.sleep(seconds)
    await bus.idle()


# ============================================================
# 门禁 1：回执合法性与字段齐全
# ============================================================


class TestGate1SchemaLegality:
    async def test_研判回执是合法assess_v1响应(self, bus: InMemoryBus, gateway: AgentGateway, registry: AgentRegistry) -> None:
        agent = await bring_up_ext(bus, gateway)
        reply = await gateway.dispatch(
            AgentType.ASSESS,
            Action.ASSESS_FACTOR_RISK,
            _assess_payload(),
            trace_id=TRACE,
            capability="factor_risk_assess",
            hazard_type="debris_flow",
        )
        assert gateway.counters["rejected"] == 0, "灾种模块的回执必须过 agent_message.v1"
        assert reply.kind is MessageKind.RESPONSE
        assert reply.trace_id == TRACE
        assert reply.causation_id is not None
        # 这条腿的应答不是 assess.v1（它有自己的动作），所以按字段断言而不是套 DTO
        payload = reply.payload
        assert payload["source"] == "hazard.factor_engine"
        assert payload["hazard_type"] == HazardType.DEBRIS_FLOW.value
        assert payload["region_code"] == REGION
        assert 0.0 < payload["confidence"] <= 0.60
        assert payload["confidence_basis"]
        assert payload["data_provenance"]["method"] == "factor_engine_mock"
        assert payload["data_provenance"]["is_mock"] is True
        assert payload["human_review_suggested"] is True
        # D10 溯源字段缺省即未知——不臆造模型版本：外部实现没有可声明的模型信息就留空
        assert reply.model_info is None
        await agent.stop()
        await gateway.close()


# ============================================================
# 门禁 2：幂等
# ============================================================


class TestGate2Idempotency:
    async def test_重复请求只回一封(self, bus: InMemoryBus, gateway: AgentGateway, registry: AgentRegistry) -> None:
        agent = await bring_up_ext(bus, gateway)
        request = make_request(
            source="platform.gateway",
            target=agent.agent_id,
            action=Action.ASSESS_FACTOR_RISK.value,
            payload=_assess_payload(),
            trace_id=TRACE,
            reply_to="reply.gwext0001.inbox",
            deadline_ms=800,
        )
        receipts = await collect_receipts(bus, request)
        await bus.publish(subjects.agent_in(AgentType.ASSESS), request)
        await bus.publish(subjects.agent_in(AgentType.ASSESS), request)
        await bus.idle()
        await wait_for_receipts(bus, receipts)
        await settle(bus)

        assert len(receipts) == 1, "重复 msg_id 不得产生第二次响应"
        assert agent.duplicates_skipped == 1
        await agent.stop()
        await gateway.close()


# ============================================================
# 门禁 3：超时语义
# ============================================================


class TestGate3TimeoutSemantics:
    async def test_超期按超时记账_事务不计成功(
        self, bus: InMemoryBus, gateway: AgentGateway, registry: AgentRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent = await bring_up_ext(bus, gateway)
        monkeypatch.setattr(agent_module, "run_risk_agent", _fake_runner(delay_s=0.4))
        with pytest.raises(AegisError):
            await gateway.dispatch(
                AgentType.ASSESS,
                Action.ASSESS_FACTOR_RISK,
                _assess_payload(),
                trace_id=TRACE,
                capability="factor_risk_assess",
                deadline_ms=200,
            )
        assert gateway.transactions[-1].outcome != "ok", "超期的事务不许记成成功"
        assert gateway.transactions[-1].error_code == "E_TIMEOUT"
        assert gateway.success_rate() == 0.0
        await asyncio.sleep(0.5)  # 等慢线程收尾
        await agent.stop()
        await gateway.close()

    async def test_超期后不再补发成功回执(
        self, bus: InMemoryBus, gateway: AgentGateway, registry: AgentRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent = await bring_up_ext(bus, gateway)
        monkeypatch.setattr(agent_module, "run_risk_agent", _fake_runner(delay_s=0.4))
        request = make_request(
            source="platform.gateway",
            target=agent.agent_id,
            action=Action.ASSESS_FACTOR_RISK.value,
            payload=_assess_payload(),
            trace_id=TRACE,
            reply_to="reply.gwext0002.inbox",
            deadline_ms=200,
        )
        receipts = await collect_receipts(bus, request)
        await bus.publish(subjects.agent_in(AgentType.ASSESS), request)
        await wait_for_receipts(bus, receipts)

        assert len(receipts) == 1, "超期只允许一封回执"
        assert receipts[0].kind is MessageKind.ERROR, "超期必须走 error 通道"
        assert receipts[0].payload["code"] == "E_TIMEOUT"
        assert receipts[0].payload["retryable"] is True

        await asyncio.sleep(0.5)
        await bus.idle()
        assert len(receipts) == 1, "超期后不得再补一封成功回执"
        assert agent.handled == 1, "超期不得触发二次执行"
        await agent.stop()
        await gateway.close()


# ============================================================
# 门禁 4：错误回执
# ============================================================


class TestGate4ErrorReply:
    async def test_内部异常收敛成类型化错误回执(
        self, bus: InMemoryBus, gateway: AgentGateway, registry: AgentRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent = await bring_up_ext(bus, gateway)
        monkeypatch.setattr(agent_module, "run_risk_agent", _fake_runner(exc=RuntimeError("图炸了")))
        with pytest.raises(AegisError) as info:
            await gateway.dispatch(
                AgentType.ASSESS,
                Action.ASSESS_FACTOR_RISK,
                _assess_payload(),
                trace_id=TRACE,
                capability="factor_risk_assess",
            )
        assert info.value.retryable is True, "内部异常可重试：平台据此走重试/降级轨"
        assert info.value.detail["code"] == AssessErrorCode.FAILED.value
        assert gateway.transactions[-1].outcome == "error"
        assert gateway.transactions[-1].error_code == AssessErrorCode.FAILED.value
        await agent.stop()
        await gateway.close()

    async def test_失败不得是正常response(
        self, bus: InMemoryBus, gateway: AgentGateway, registry: AgentRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent = await bring_up_ext(bus, gateway)
        monkeypatch.setattr(agent_module, "run_risk_agent", _fake_runner(exc=RuntimeError("图炸了")))
        request = make_request(
            source="platform.gateway",
            target=agent.agent_id,
            action=Action.ASSESS_FACTOR_RISK.value,
            payload=_assess_payload(),
            trace_id=TRACE,
            reply_to="reply.gwext0003.inbox",
            deadline_ms=800,
        )
        receipts = await collect_receipts(bus, request)
        await bus.publish(subjects.agent_in(AgentType.ASSESS), request)
        await bus.idle()
        await wait_for_receipts(bus, receipts)
        await settle(bus)

        assert len(receipts) == 1
        assert receipts[0].kind is MessageKind.ERROR
        assert receipts[0].payload["code"] == AssessErrorCode.FAILED.value
        assert "risk_level" not in receipts[0].payload, "失败回执不得夹带走定级的结论"
        await agent.stop()
        await gateway.close()

    async def test_数据不足走错误通道而非无风险(self, bus: InMemoryBus, gateway: AgentGateway, registry: AgentRegistry) -> None:
        """证据包没有观测 → 错误通道 `E_ASSESS_INSUFFICIENT`，绝不是 5=无风险。"""
        agent = await bring_up_ext(bus, gateway)
        with pytest.raises(AegisError) as info:
            await gateway.dispatch(
                AgentType.ASSESS,
                Action.ASSESS_FACTOR_RISK,
                _assess_payload(evidence=_evidence(observations=[])),
                trace_id=TRACE,
                capability="factor_risk_assess",
            )
        assert info.value.detail["code"] == AssessErrorCode.INSUFFICIENT.value
        assert info.value.retryable is False
        assert gateway.transactions[-1].outcome == "error"
        await agent.stop()
        await gateway.close()

    async def test_未注册动作的上行被网关拒收(self, bus: InMemoryBus, gateway: AgentGateway) -> None:
        await gateway.start()
        await bus.publish(
            subjects.agent_out(AgentType.ASSESS),
            AgentMessage(
                source="assess.factor01",
                target="platform.sink",
                kind=MessageKind.EVENT,
                action="assess.teleport",
                payload={},
                trace_id=TRACE,
            ),
        )
        await bus.idle()
        assert gateway.counters["rejected"] == 1
        await gateway.close()


# ============================================================
# 门禁 5：生命周期
# ============================================================


class TestGate5Lifecycle:
    async def test_注册即在册且能力声明正确(self, bus: InMemoryBus, gateway: AgentGateway, registry: AgentRegistry) -> None:
        agent = await bring_up_ext(bus, gateway)
        entry = registry.get(agent.agent_id)
        assert entry is not None, "外部实现注册后必须立刻在册"
        assert entry.healthy
        assert entry.reg.capabilities == ["factor_risk_assess"]
        assert entry.reg.agent_type == AgentType.ASSESS.value
        assert entry.reg.hazard_types == SUPPORTED_HAZARDS
        assert registry.pick(AgentType.ASSESS.value, "factor_risk_assess") is not None
        await agent.stop()
        await gateway.close()

    async def test_断心跳后摘除路由_恢复后重新可用(self, bus: InMemoryBus, gateway: AgentGateway, registry: AgentRegistry) -> None:
        agent = await bring_up_ext(bus, gateway)
        await agent.stop()
        entry = registry.get(agent.agent_id)
        assert entry is not None
        entry.last_hb_mono = 0.0
        assert await registry.sweep_and_notify() == [agent.agent_id]
        assert registry.pick(AgentType.ASSESS.value, "factor_risk_assess") is None, "失联的本体不得再被派单"

        await agent.start(heartbeat_interval=0.05)
        await bus.idle()
        assert registry.pick(AgentType.ASSESS.value, "factor_risk_assess") is not None
        await agent.stop()
        await gateway.close()


# ============================================================
# 门禁 6：数据边界
# ============================================================


class TestGate6DataBoundary:
    def test_外部实现不直连存储层与服务层(self) -> None:
        """静态检查：只允许总线路由与契约 DTO 两类平台依赖。"""
        allowed = {"aegis.agents", "aegis.agents.hazards", "aegis.bus", "aegis.domain", "aegis.errors"}
        forbidden_roots = {
            "aegis.storage",
            "aegis.persistence",
            "aegis.services",
            "aegis.pipeline",
            "aegis.api",
            "aegis.workflow",
        }
        offenders: list[str] = []
        for path in EXT_PACKAGE.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                else:
                    continue
                for name in names:
                    if not name.startswith("aegis"):
                        continue
                    root = ".".join(name.split(".")[:2])
                    if root in forbidden_roots or root not in allowed:
                        offenders.append(f"{path.name}:{name}")
        assert offenders == [], f"外部实现越界引用平台内部模块: {offenders}"

    def test_服务实例上没有数据库客户端(self, bus: InMemoryBus) -> None:
        agent = build_hazard_agents(bus)[0]
        fields = {name.lower() for name in vars(agent)}
        assert not (fields & {"session", "engine", "connection", "repository", "store", "db"})

    async def test_取数只来自平台推送的证据(self, bus: InMemoryBus, gateway: AgentGateway, registry: AgentRegistry) -> None:
        """响应里的证据引用必须能追溯到请求推送的因子（证明它真读了那一份）。"""
        agent = await bring_up_ext(bus, gateway)
        reply = await gateway.dispatch(
            AgentType.ASSESS,
            Action.ASSESS_FACTOR_RISK,
            _assess_payload(),
            trace_id=TRACE,
            capability="factor_risk_assess",
        )
        refs = reply.payload["evidence_refs"]
        assert refs, "结论必须声明用到的证据"
        assert any(ref.startswith("factor:rainfall_mm_h") for ref in refs), refs
        await agent.stop()
        await gateway.close()


# ============================================================
# 门禁 7：上下文可恢复
# ============================================================


class TestGate7ContextSnapshot:
    async def test_研判结论带齐可恢复快照所需字段(self, bus: InMemoryBus, gateway: AgentGateway, registry: AgentRegistry) -> None:
        agent = await bring_up_ext(bus, gateway)
        reply = await gateway.dispatch(
            AgentType.ASSESS,
            Action.ASSESS_FACTOR_RISK,
            _assess_payload(),
            trace_id=TRACE,
            capability="factor_risk_assess",
        )
        payload = reply.payload
        for field in (
            "hazard_type",
            "region_code",
            "risk_level",
            "confidence",
            "confidence_basis",
            "rationale",
            "evidence_refs",
            "assessed_by",
            "data_provenance",
            "assessment",
        ):
            assert field in payload, f"快照要恢复的字段缺了：{field}"
        assert payload["hazard_type"] in [item.value for item in HazardType]
        assert int(payload["risk_level"]) in [int(level) for level in RiskLevel]
        assert 0.0 <= float(payload["confidence"]) <= 1.0
        assert payload["rationale"], "快照要能恢复出「依据」，不能只剩一个等级"
        assert json.loads(json.dumps(payload, ensure_ascii=False)) == payload, "结论必须是可序列化的纯数据"
        await agent.stop()
        await gateway.close()

    async def test_没有承接方的能力记no_agent而不是error(self, bus: InMemoryBus, gateway: AgentGateway, registry: AgentRegistry) -> None:
        """派一项**没人声明**的能力：网关记 `no_agent`，平台走既有本地降级轨。

        这条现在仍然成立：灾种模块的决策段本体承接的是 `hazard_workflow`，不是
        `task_decompose`——后者归平台自建的决策内环。
        """
        agent = await bring_up_ext(bus, gateway)
        with pytest.raises(NoCapableAgentError):
            await gateway.dispatch(
                AgentType.PLAN,
                Action.PLAN_STU,
                {"event_id": "evt_" + "c" * 12, "risk": {}},
                trace_id=TRACE,
                capability="task_decompose",
            )
        assert gateway.counters["no_agent"] == 1
        assert gateway.transactions[-1].outcome == "no_agent"
        await agent.stop()
        await gateway.close()


# ============================================================
# 契约面：灾种键 / 支持清单 / 不支持
# ============================================================


class Test边界口径:
    def test_灾种标准键就是平台键(self) -> None:
        """两套名字不再需要翻译：标准键直接取平台枚举值。"""
        expected = [
            HazardType.LANDSLIDE.value,
            HazardType.ROCKFALL.value,
            HazardType.DEBRIS_FLOW.value,
            HazardType.AVALANCHE.value,
            HazardType.LAKE_OUTBURST.value,
        ]
        assert expected == SUPPORTED_HAZARDS
        assert len(SUPPORTED_HAZARDS) == 5

    def test_旧交付键只作为别名归一化(self) -> None:
        assert normalize_hazard_type("ice_rock_snow_avalanche") == HazardType.AVALANCHE.value
        assert normalize_hazard_type("glacial_lake_outburst") == HazardType.LAKE_OUTBURST.value
        assert HAZARD_ALIAS["ice_rock_snow_avalanche"] == HazardType.AVALANCHE.value
        assert HAZARD_ALIAS["glacial_lake_outburst"] == HazardType.LAKE_OUTBURST.value

    def test_地震不支持(self) -> None:
        with pytest.raises(UnsupportedHazardError):
            require_hazard("earthquake")
        assert not any("earthquake" in str(value) for value in SUPPORTED_HAZARDS)

    async def test_平台独有灾种明确拒绝不硬塞(self, bus: InMemoryBus, gateway: AgentGateway, registry: AgentRegistry) -> None:
        """`quake_triggered` 在平台枚举里，但外部实现不支持：明确拒绝，不挑近似灾种。"""
        agent = await bring_up_ext(bus, gateway)
        with pytest.raises(AegisError) as info:
            await gateway.dispatch(
                AgentType.ASSESS,
                Action.ASSESS_FACTOR_RISK,
                _assess_payload(hazard="quake_triggered"),
                trace_id=TRACE,
                capability="factor_risk_assess",
            )
        assert info.value.detail["code"] == AssessErrorCode.HAZARD_UNSUP.value
        assert gateway.transactions[-1].outcome == "error"
        await agent.stop()
        await gateway.close()

    async def test_外部署名与平台自建内环不同(self, bus: InMemoryBus, gateway: AgentGateway, registry: AgentRegistry) -> None:
        from aegis.agents.factory import build_real_agents

        agent = await bring_up_ext(bus, gateway)
        reply = await gateway.dispatch(
            AgentType.ASSESS,
            Action.ASSESS_FACTOR_RISK,
            _assess_payload(),
            trace_id=TRACE,
            capability="factor_risk_assess",
        )
        builtin_ids = {item.agent_id for item in build_real_agents(bus)}
        assessed_by = reply.payload["assessed_by"]
        assert assessed_by == agent.agent_id
        assert assessed_by not in builtin_ids, "外部实现不能与平台自建内环共用署名"
        await agent.stop()
        await gateway.close()


# ============================================================
# 清退面与接线真实性
# ============================================================


class Test接线真实性:
    def test_遗留适配层模块已删除(self) -> None:
        """`adapter` / `contract_face` / `levels` 必须整层消失（不是留着不用）。"""
        for name in (
            "aegis.agents.hazards.adapter",
            "aegis.agents.hazards.contract_face",
            "aegis.agents.hazards.levels",
        ):
            with pytest.raises(ModuleNotFoundError):
                importlib.import_module(name)

    def test_运行时装配注册全部六个本体且能力互不重叠(self, bus: InMemoryBus) -> None:
        """六个本体全部在册，且每项能力**恰好一个**主人。

        两项能力撞在同一对 `(agent_type, capability)` 上，网关的 `pick()` 会在两个本体
        之间按负载取舍——"谁应答"就变成不可复现的随机数。所以这项断言不是"看着整齐"，
        是路由正确性的前提。
        """
        agents = build_hazard_agents(bus)
        assert len(agents) == 6
        seen: dict[tuple[str, str], str] = {}
        for agent in agents:
            assert agent.hazard_types == SUPPORTED_HAZARDS
            for capability in agent.capabilities:
                key = (agent.agent_type.value, capability)
                assert key not in seen, f"能力 {capability} 有两个主人：{seen.get(key)} 与 {agent.agent_id}"
                seen[key] = agent.agent_id
        assert set(seen) == {
            ("assess", "data_quality"),
            ("assess", "factor_risk_assess"),
            ("assess", "scenario_sim"),
            ("execute", "warn_content"),
            ("feedback", "chain_audit"),
            ("plan", "hazard_workflow"),
        }

    def test_服务入口吃统一契约(self) -> None:
        pytest.importorskip("langgraph", reason="未安装可选依赖 agents（`uv sync --extra agents`）")
        from aegis.agents.hazards.risk_agent.main import run_risk_agent

        params = inspect.signature(run_risk_agent).parameters
        assert list(params)[:2] == ["request", "evidence"]
        annotations = {name: param.annotation for name, param in params.items()}
        assert annotations["request"] is AssessRequest
        assert annotations["evidence"] is AssessEvidence
        assert inspect.signature(run_risk_agent).return_annotation is AssessResponse

    def test_服务只认assess_factor_risk动作(self) -> None:
        source = Path(agent_module.__file__).read_text(encoding="utf-8")
        assert "Action.ASSESS_FACTOR_RISK.value" in source
        assert "Action.PLAN" not in source
