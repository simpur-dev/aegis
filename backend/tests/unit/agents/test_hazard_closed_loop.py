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
from aegis.domain.enums import Action, AgentType, MessageKind

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


# ============================================================
# 生产路径回归：HazardWorkflowAgent.handle（专用执行器 + 共享腿池）
# ============================================================


async def test_生产路径闭环在容量1的腿池上不死锁(
    monkeypatch: pytest.MonkeyPatch,
    bus: InMemoryBus,
    gateway: AgentGateway,
) -> None:
    """闭环编排与五条腿的执行器**物理隔离**：腿池容量 1 也必须能跑完整条闭环。

    历史缺陷：编排器曾与腿共用共享池——容量 1 时编排器占住唯一 worker、腿永远
    排队，闭环 100% 全腿超时（确定性复现，不是竞态）。现在编排器用自带的专用
    执行器（HazardWorkflowAgent.workflow_workers），腿池再小也不互相饿死。
    这条用例走的就是生产路径（`agent.handle` → `run_blocking_on(专用执行器)`），
    不再绕道 `asyncio.to_thread`。
    """
    from aegis.agents.hazards.manager_agent.agent import HazardWorkflowAgent
    from aegis.agents.hazards.pool import configure_pool
    from aegis.domain.messages import make_request

    data_main = importlib.import_module("aegis.agents.hazards.data_agent.main")
    monkeypatch.setattr(
        data_main,
        "run_data_agent",
        lambda task, hazard_type: _complete_data_state(task, hazard_type),
    )

    configure_pool(1)  # 腿池最小容量：历史缺陷在此配置下必然死锁
    await gateway.start()
    agents = build_hazard_agents(bus)
    loop_agent = next(a for a in agents if isinstance(a, HazardWorkflowAgent))
    for agent in agents:
        await agent.start(heartbeat_interval=0.05)
    await bus.idle()
    await asyncio.sleep(0.05)
    await bus.idle()

    seen = await _collect_bus_requests(bus, list(_SIBLINGS.values()))
    request = make_request(
        source="platform.gateway",
        target=loop_agent.agent_id,
        action=Action.PLAN_HAZARD_WORKFLOW.value,
        payload={"hazard_type": "debris_flow", "task_type": "full_analysis", "region": "540121", "require_simulation": True},
        trace_id=TRACE,
        reply_to="reply.gwloop.inbox",
        deadline_ms=120_000,
    )
    try:
        reply = await loop_agent.handle(request)
        report = reply.payload
        assert report["task_status"] in {"completed", "partially_completed"}, report["error_message"]
        request_targets = {message.target for message in seen if message.kind is MessageKind.REQUEST and message.target != "*"}
        # _SIBLINGS 的值是 (agent_type, agent_id) 对，取 id 比对
        assert {agent_id for _, agent_id in _SIBLINGS.values()} <= request_targets, sorted(request_targets)
    finally:
        for agent in agents:
            await agent.stop()
        await gateway.close()


class TestBudgetPropagation:
    """外层 deadline 是整条闭环的总预算：单腿预算随剩余预算收缩、耗尽即快速失败。"""

    def _context(self, outer: int | None) -> Any:
        from aegis.agents.hazards.bus_context import BusContext

        class _T:
            name = "null"

        return BusContext(
            transport=_T(),  # type: ignore[arg-type]
            caller_id="plan.loop01",
            siblings={},
            deadline_ms=60_000,
            outer_deadline_ms=outer,
        )

    def test_无外层约束时用单腿默认预算(self) -> None:
        assert self._context(None).leg_budget_ms() == 60_000

    def test_单腿预算随外层预算按五腿拆分(self) -> None:
        assert self._context(50_000).leg_budget_ms() == 10_000
        assert self._context(10_000).leg_budget_ms() == 2_000
        assert self._context(2_000).leg_budget_ms() == 1_000, "拆到最低也有 1s：低于这个数的腿连失败的机会都没有"

    async def test_预算耗尽快速失败而不是磨满超时(self) -> None:
        from aegis.agents.hazards.bus_context import BusContext

        class _T:
            name = "null"

        context = BusContext(
            transport=_T(),  # type: ignore[arg-type]
            caller_id="plan.loop01",
            siblings={},
            deadline_ms=60_000,
            outer_deadline_ms=300,
        )
        started = asyncio.get_running_loop().time()
        with pytest.raises(Exception, match="预算已耗尽"):
            context.ensure_budget()
        # 快速失败：不许真的等 300ms
        assert asyncio.get_running_loop().time() - started < 0.2

    async def test_循环线程内调用同步入口被守卫拒绝(self) -> None:
        """重入守卫：request_sync 在平台事件循环线程内被调用时当场拒绝。

        `future.result` 会堵死循环、协程永远得不到调度——必然挂死到超时。
        守卫把"挂 65 秒后假死"变成"立刻报错"。"""
        from aegis.agents.hazards.bus_context import BusContext

        class _T:
            name = "null"

        context = BusContext(
            transport=_T(),  # type: ignore[arg-type]
            caller_id="plan.loop01",
            siblings={},
            deadline_ms=60_000,
            loop=asyncio.get_running_loop(),
            outer_deadline_ms=100,
        )
        with pytest.raises(Exception, match="事件循环线程内被调用"):
            context.request_sync("data_quality", Action.ASSESS_DATA_QUALITY, {}, trace_id=TRACE)
