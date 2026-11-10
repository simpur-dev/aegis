"""灾种闭环的**总线化**验证：五步是不是真的经总线派出去。

这条用例守着本次重构最核心的一条主张：闭环（Manager）向五个兄弟本体派单时走的是
**总线**，不是进程内函数直调。直调会绕过消息信封、deadline、`msg_id` 幂等与类型化
错误——那样"接了总线"就只是一句口号，平台层的门禁与观测全都照不到它。

怎么证明"走了总线"：订阅总线，收集所有上行消息，看有没有发往
`agent.<段>.in.<兄弟本体 id>` 的请求。这比"断言某个函数被调用过"硬——
进程内直调不会在总线上留下任何痕迹。
"""

from __future__ import annotations

import asyncio
import importlib
from typing import Any

import pytest

pytest.importorskip("langgraph", reason="灾种模块依赖可选 extra agents")

from aegis.agents.factory import build_hazard_agents
from aegis.agents.hazards.bus_context import BusContext, SiblingRef
from aegis.bus import subjects
from aegis.bus.gateway import AgentGateway
from aegis.bus.inmemory import InMemoryBus
from aegis.domain.enums import AgentType, MessageKind

TRACE = "trc_" + "c" * 16

#: 闭环的五个兄弟本体：能力 → (流程阶段, 承接它的本体 id)。这些 id 来自能力矩阵，
#: 不是另写一份——另写一份迟早会与矩阵对不上，而对不上时闭环会静默地少一条腿。
_SIBLINGS = {
    "data_quality": (AgentType.ASSESS, "assess.data01"),
    "factor_risk_assess": (AgentType.ASSESS, "assess.factor01"),
    "scenario_sim": (AgentType.ASSESS, "assess.sim01"),
    "warn_content": (AgentType.EXECUTE, "execute.hazard01"),
    "chain_audit": (AgentType.FEEDBACK, "feedback.audit01"),
}


class _RaisingStructured:
    """一调用就抛：图里的解释/验证节点随即走模板/规则兜底——那本就是设计的一部分。"""

    def invoke(self, _messages: Any) -> Any:
        raise RuntimeError("测试禁网：LLM 不可用（走模板/规则兜底）")


class _BrokenLlm:
    def with_structured_output(self, _schema: Any, **_kw: Any) -> _RaisingStructured:
        return _RaisingStructured()


@pytest.fixture(autouse=True)
def _no_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    """图里的解释/验证节点换成"必抛"桩：模板/规则兜底本就是设计的一部分。

    用例不拨号、不花钱、不依赖网络。桩必须**抛异常**而不是返回 None——返回 None
    会让节点在别处炸掉，把"兜底路径"这条真正要验的东西变成"节点崩了"。
    """
    for module_name in (
        "aegis.agents.hazards.risk_agent.nodes.risk_assess_node",
        "aegis.agents.hazards.risk_agent.nodes.risk_validate_node",
        "aegis.agents.hazards.simulation_agent.nodes.scenario_design_node",
        "aegis.agents.hazards.simulation_agent.nodes.scenario_validate_node",
        "aegis.agents.hazards.warning_agent.nodes.llm_explanation_node",
        "aegis.agents.hazards.feedback_agent.nodes.feedback_analyze_node",
        "aegis.agents.hazards.feedback_agent.nodes.reflection_node",
        "aegis.agents.hazards.feedback_agent.nodes.suggestion_node",
    ):
        try:
            module = importlib.import_module(module_name)
        except ImportError:  # pragma: no cover - 某些节点不在某些版本里
            continue
        for attr in ("get_risk_llm", "get_simulation_llm", "get_warning_llm", "get_feedback_llm"):
            if hasattr(module, attr):
                monkeypatch.setattr(module, attr, lambda: _BrokenLlm())


def _complete_data_state(task: str, hazard_type: str) -> dict[str, Any]:
    """一份**完整**的取数产出。

    她的 mock 数据源故意留了一条缺 `time` 的记录（用来验证缺失标记路径），那份数据
    会被数据质量门判为"不足以支撑评估"，闭环于是停在第一步——那是对的，但验不了后面
    四步。这里换一份完整的，好把五步全走一遍。
    """
    # 因子字段要齐：因子引擎按"覆盖度"拒答，缺因子它宁可不给分也不填 0。
    from aegis.agents.hazards.risk_agent.tools.hazard_risk_registry import get_hazard_risk_profile

    profile = get_hazard_risk_profile(hazard_type)
    events = [
        {
            "event_id": f"E-{index}",
            "time": "2024-07-08T03:00:00Z",
            "latitude": 29.6 + index * 0.01,
            "longitude": 95.3 + index * 0.01,
            "region": "西藏林芝",
            "source": "mock",
            **profile.get("sample_values", {}),
        }
        for index in range(1, 4)
    ]
    return {
        "hazard_type": hazard_type,
        "processed_data": {"events": events, "data_source": "mock", "hazard_type": hazard_type, "is_mock": True},
        "data_quality": {
            "is_valid": True,
            "completeness_ratio": 1.0,
            "validation_reason": "完整度 100.0%，异常率 0.0%",
            "total_records": len(events),
            "complete_records": len(events),
        },
        "missing_data": [],
        "data_sufficiency": {"sufficient": True, "reason": "数据质量合格"},
        "status": "success",
        "error_message": "",
    }


async def _collect_bus_requests(bus: InMemoryBus, agent_ids: list[tuple[AgentType, str]]) -> list[Any]:
    """收集发往这些本体**专属收件箱**的上行消息。

    订的是专属口（`agent.<段>.in.<agent_id>`）而不是类型级公共口：闭环按兄弟的
    `agent_id` 点名派单，请求落在专属口上。
    """
    seen: list[Any] = []

    async def collector(message: Any) -> None:
        seen.append(message)

    for agent_type, agent_id in agent_ids:
        await bus.subscribe(subjects.agent_in(agent_type, agent_id), collector)
    return seen


async def test_闭环五步都经总线派单(
    monkeypatch: pytest.MonkeyPatch,
    bus: InMemoryBus,
    gateway: AgentGateway,
) -> None:
    """闭环的每一步都要在总线上留下痕迹，且收件箱是**兄弟本体的专属收件箱**。"""
    data_main = importlib.import_module("aegis.agents.hazards.data_agent.main")
    monkeypatch.setattr(
        data_main,
        "run_data_agent",
        lambda task, hazard_type: _complete_data_state(task, hazard_type),
    )

    await gateway.start()

    agents = build_hazard_agents(bus)
    for agent in agents:
        await agent.start(heartbeat_interval=0.05)
    await bus.idle()
    await asyncio.sleep(0.05)
    await bus.idle()

    seen = await _collect_bus_requests(bus, list(_SIBLINGS.values()))
    siblings = {
        capability: SiblingRef(capability=capability, agent_type=agent_type, agent_id=agent_id)
        for capability, (agent_type, agent_id) in _SIBLINGS.items()
    }
    bus_ctx = BusContext(
        transport=bus,
        caller_id="plan.loop01",
        siblings=siblings,
        deadline_ms=120_000,
        loop=asyncio.get_running_loop(),
    )

    from aegis.agents.hazards.manager_agent.main import run_manager_agent

    try:
        report = await asyncio.to_thread(
            run_manager_agent,
            hazard_type="debris_flow",
            task_type="full_analysis",
            region="540121",
            event_id="evt_" + "d" * 12,
            require_simulation=True,
            bus=bus_ctx,
            trace_id=TRACE,
        )

        # ① 总线上确实有发往五个兄弟专属收件箱的请求
        request_targets = {message.target for message in seen if message.kind is MessageKind.REQUEST and message.target != "*"}
        for capability, (_agent_type, agent_id) in _SIBLINGS.items():
            assert agent_id in request_targets, f"{capability} 这一步没有经总线派给 {agent_id}（实际目标：{sorted(request_targets)}）"

        # ② 闭环报告里五步都有交代
        status = report.get("agent_status") or {}
        assert status.get("data_agent"), status
        assert status.get("risk_agent"), status
        assert status.get("warning_agent"), status
        assert status.get("feedback_agent"), status
    finally:
        for agent in agents:
            await agent.stop()
        await gateway.close()
