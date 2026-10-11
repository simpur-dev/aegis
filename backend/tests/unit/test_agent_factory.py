"""真智能体装配工厂测试（§3 / MA）：四个真内环经工厂挂上总线 → 注册在线 + 网关可服务。

只验装配与总线契约（注册/心跳/应答），不依赖真 LLM（假网关喂结构化结论）。execute 有意缺席。
"""

from __future__ import annotations

import asyncio

from aegis.agents.factory import build_real_agents, start_real_agents
from aegis.bus.gateway import AgentGateway
from aegis.bus.inmemory import InMemoryBus
from aegis.bus.registry import AgentRegistry
from aegis.domain.enums import Action, AgentType

TRACE = "trc_" + "8" * 16


class FakeLLM:
    available = True

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def chat_structured(
        self, messages, *, schema, task="default", agent_id=None, trace_id=None, prompt_key=None, prompt_vars=None, **kw
    ):
        self.calls.append(task)
        return schema(risk_level=3, hazard_type="debris_flow", confidence=0.7, rationale="强降雨", evidence_refs=["R-1"])

    def model_info_for_task(self, task):
        return {"model": "qwen-plus", "fallback_model": "", "task": task, "prompt_version": "1.0.0"}


async def test_five_real_agents_register_and_assess_serves(bus: InMemoryBus, gateway: AgentGateway, registry: AgentRegistry) -> None:
    await gateway.start()
    agents = await start_real_agents(bus, llm=FakeLLM(), heartbeat_interval=0.05)
    await bus.idle()
    await asyncio.sleep(0.02)
    await bus.idle()

    assert registry.online_count == 5, "五个真内环都应在册（execute 只产文案，不带发送权）"
    for agent_id in ("perceive.real01", "assess.real01", "plan.real01", "feedback.real01", "execute.real01"):
        assert registry.get(agent_id) is not None, agent_id

    reply = await gateway.dispatch(
        AgentType.ASSESS,
        Action.ASSESS_HAZARD,
        {"region_code": "540121", "hits": [{"rule_id": "R-1", "hazard_type": "debris_flow", "region_code": "540121", "score": 0.9}]},
        trace_id=TRACE,
        capability="risk_assess",
    )
    assert gateway.counters["rejected"] == 0
    assert reply.payload["risk_level"] == 3
    assert reply.payload["double_blind"] == "not_applicable"  # 黄级不触发双盲（真内环经真总线应答）

    for agent in agents:
        await agent.stop()
    await gateway.close()


async def test_build_without_start_registers_nobody(bus: InMemoryBus, registry: AgentRegistry) -> None:
    agents = build_real_agents(bus, llm=FakeLLM())
    assert len(agents) == 5
    assert {a.agent_type.value for a in agents} == {"perceive", "assess", "plan", "execute", "feedback"}
    assert registry.online_count == 0, "只构造不启动，不该有人在册"
