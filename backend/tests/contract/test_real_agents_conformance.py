"""真智能体（非 Mock）过《智能体接入规范》§7 门禁——MA 验收"真智能体上总线 + 7 项门禁"的离线证据。

用内存总线 + 假 LLM 把真实 assess/plan 内环挂上网关跑一遍：证明 agentkit 生命周期、
契约合法性、幂等、异常降级、D10 溯源、STU 100% 合法在真智能体上同样成立。
（真 LLM/真回执的现场量测属 MA 出口门，需要密钥与服务，不在此离线断言范围内。）
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from aegis.agents.assess import AssessAgent, AssessVerdict
from aegis.agents.plan import PlanAgent, PlanRefinement
from aegis.bus.gateway import AgentGateway
from aegis.bus.inmemory import InMemoryBus
from aegis.bus.registry import AgentRegistry
from aegis.domain.enums import Action, AgentType
from aegis.domain.messages import AgentMessage

TRACE = "trc_" + "4" * 16


class FakeLLM:
    """最小 LLM 网关替身：按脚本回结构化结论，并提供 D10 model_info。"""

    def __init__(self, verdicts: list[Any], *, available: bool = True, raises: Exception | None = None) -> None:
        self._q = list(verdicts)
        self.available = available
        self._raises = raises
        self.calls: list[str] = []

    async def chat_structured(
        self, messages, *, schema, task="default", agent_id=None, trace_id=None, prompt_key=None, prompt_vars=None, **kw
    ):
        self.calls.append(task)
        if self._raises is not None:
            raise self._raises
        return self._q.pop(0)

    def model_info_for_task(self, task):
        return {"model": "qwen-plus", "fallback_model": "", "task": task, "prompt_version": "9.9.9-stub"}


async def bring_up(bus: InMemoryBus, gateway: AgentGateway, agent: Any) -> Any:
    await gateway.start()
    await agent.start(heartbeat_interval=0.05)
    await bus.idle()
    await asyncio.sleep(0.01)
    await bus.idle()
    return agent


def _assess(llm: FakeLLM, agent_id: str = "assess.real01") -> AssessAgent:
    return AssessAgent(transport=None, agent_type=AgentType.ASSESS, agent_id=agent_id, capabilities=["risk_assess"], llm=llm)  # type: ignore[arg-type]


class TestGate1SchemaLegality:
    async def test_real_assess_reply_is_contract_legal_with_provenance(
        self, bus: InMemoryBus, gateway: AgentGateway, registry: AgentRegistry
    ) -> None:
        llm = FakeLLM([AssessVerdict(risk_level=3, hazard_type="debris_flow", confidence=0.7, rationale="降雨", evidence_refs=["R-1"])])
        agent = _assess(llm)
        agent.transport = bus
        await bring_up(bus, gateway, agent)
        assert registry.get("assess.real01") is not None, "真智能体注册即在册"
        reply = await gateway.dispatch(
            AgentType.ASSESS,
            Action.ASSESS_HAZARD,
            {"region_code": "540121", "hits": [{"rule_id": "R-1", "hazard_type": "debris_flow", "region_code": "540121", "score": 0.9}]},
            trace_id=TRACE,
            capability="risk_assess",
        )
        assert gateway.counters["rejected"] == 0
        assert reply.payload["risk_level"] == 3
        assert reply.causation_id is not None
        # D10：真智能体经总线回传 model_info，且过契约（schema 已声明该可选字段）。
        # 9.9.9-stub 是**桩件自己**报的版本（不是注册表里的真实版本号）：这条判的是"版本有没有被透传进信封"，
        # 用一个一看就是假号的值，免得后来人把它读成"assess 的 prompt 就是 1.0.0"。
        assert reply.model_info is not None and reply.model_info.prompt_version == "9.9.9-stub"
        await agent.stop()
        await gateway.close()


class TestGate2Idempotency:
    async def test_duplicate_request_single_reply(self, bus: InMemoryBus, gateway: AgentGateway, registry: AgentRegistry) -> None:
        from aegis.bus import subjects
        from aegis.domain.messages import make_request

        llm = FakeLLM([AssessVerdict(risk_level=3, hazard_type="debris_flow", confidence=0.7, rationale="r")])
        agent = _assess(llm)
        agent.transport = bus
        await bring_up(bus, gateway, agent)
        request = make_request(
            source="platform.gateway",
            target="assess.real01",
            action=Action.ASSESS_HAZARD.value,
            payload={"region_code": "540121", "hits": []},
            trace_id=TRACE,
            reply_to="reply.gwreal0001.inbox",
            deadline_ms=800,
        )
        replies: list[AgentMessage] = []

        async def collector(m: AgentMessage) -> None:
            replies.append(m)

        await bus.subscribe(request.reply_to or "", collector)
        await bus.publish(subjects.agent_in(AgentType.ASSESS), request)
        await bus.publish(subjects.agent_in(AgentType.ASSESS), request)
        await bus.idle()
        await asyncio.sleep(0.05)
        await bus.idle()
        assert len(replies) == 1
        assert agent.duplicates_skipped == 1
        await agent.stop()
        await gateway.close()


class TestGate4Error:
    async def test_llm_internal_error_becomes_typed_error_reply(
        self, bus: InMemoryBus, gateway: AgentGateway, registry: AgentRegistry
    ) -> None:
        from aegis.errors import AegisError
        from aegis.services.llm.llm_gateway import MalformedLlmOutputError

        llm = FakeLLM([], raises=MalformedLlmOutputError("坏输出"))
        agent = _assess(llm)
        agent.transport = bus
        await bring_up(bus, gateway, agent)
        with pytest.raises(AegisError):
            await gateway.dispatch(AgentType.ASSESS, Action.ASSESS_HAZARD, {"region_code": "540121", "hits": []}, trace_id=TRACE)
        assert gateway.transactions[-1].outcome == "error"
        await agent.stop()
        await gateway.close()


class TestGate7StuLegality:
    async def test_real_plan_reply_task_units_all_contract_legal(
        self, bus: InMemoryBus, gateway: AgentGateway, registry: AgentRegistry, contracts
    ) -> None:
        llm = FakeLLM([PlanRefinement(adjustments=[])])
        agent = PlanAgent(
            transport=bus, agent_type=AgentType.PLAN, agent_id="plan.real01", capabilities=["task_decompose"], llm=llm, contracts=contracts
        )
        await bring_up(bus, gateway, agent)
        reply = await gateway.dispatch(
            AgentType.PLAN,
            Action.PLAN_STU,
            {
                "event_id": "evt_" + "d" * 12,
                "risk": {
                    "hazard_type": "debris_flow",
                    "region_code": "540121",
                    "risk_level": 1,
                    "confidence": 0.9,
                    "rationale": "强降雨",
                    "evidence_refs": ["R-1"],
                },
                "objective": "处置",
                "constraints": {},
                "reference_cases": [],
                "retrieved_context": [],
            },
            trace_id=TRACE,
            capability="task_decompose",
        )
        units = reply.payload["task_units"]
        assert units, "决策智能体必须产出 STU"
        assert contracts.stu_errors(units) == [], "真智能体交付 100% 合法"
        assert all(u["created_by"] == "plan.real01" for u in units)
        assert all("context_snapshot" in u for u in units)
        await agent.stop()
        await gateway.close()
