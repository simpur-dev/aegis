"""智能体层的**唯一装配点**。

平台上跑着十一个本体：平台自建五段内环（感知/研判/决策/执行/反馈）+ 灾种闭环模块的
六个本体（取数质量门/因子引擎/情景仿真/预警素材/链内审计/闭环编排）。它们共用同一套
总线契约（`AgentKit`）、同一张能力矩阵（`agents/capabilities.py`），也共用这一个装配
入口——分散在多处装配的智能体，迟早会出现"某个本体没被装上，于是每次派单都走
`no_agent` 降级"这种没人看得出来的洞。

**能力与本体的对应关系只有一处说了算**：能力矩阵里写着每项能力由谁承接（`owner`
就是 `agent_id`），工厂照着它建本体、照着它给闭环登记兄弟地址。新增能力只改矩阵，
装配关系自动跟着走。

依赖分层：
- 平台五段内环只吃平台既有依赖，永远装得上；
- 灾种六体要可选 extra `agents`（langgraph / langchain）。没装时
  `build_hazard_agents` 抛 `HazardAgentsUnavailableError`，`build_all_agents` 如实
  留痕跳过——不让一个可选依赖决定整条链路能不能跑。

execute 只产文案，不持发送权：装配它不等于把发送权交给智能体——触达证据只认平台
交付腿的真回执，智能体自报的通道结果一律不采信。
"""

from __future__ import annotations

import logging
from typing import Any

from aegis.agents.agentkit import AgentKit
from aegis.agents.assess import AssessAgent, AssessContextProvider
from aegis.agents.capabilities import spec_for
from aegis.agents.execute import ExecuteAgent
from aegis.agents.feedback import FeedbackAgent
from aegis.agents.perceive import AnomalyDetector, PerceiveAgent, SemanticParser
from aegis.agents.plan import PlanAgent
from aegis.bus.transport import BusTransport
from aegis.domain.enums import AgentType

log = logging.getLogger("aegis.agents.factory")

#: 灾种模块的本体版本串。与平台内环的 `agent-1.0.0` 分开——版本是给运维看"这条腿
#: 是哪套实现"的，不是给网关看的。
HAZARD_VERSION = "hazard-1.0.0"

#: 灾种闭环模块的版本串（沿用原模块的命名口径）。
HAZARD_WORKFLOW_VERSION = "hazard-workflow-1.0.0"

#: 闭环内部要派单的五项能力 → 承接本体的地址。闭环据此找兄弟。
_LOOP_CAPABILITIES = (
    "data_quality",
    "factor_risk_assess",
    "scenario_sim",
    "warn_content",
    "chain_audit",
)


class HazardAgentsUnavailableError(RuntimeError):
    """灾种模块未装配（可选 extra 没装 / 导入失败）。"""


def build_real_agents(
    transport: BusTransport,
    *,
    llm: Any | None = None,
    memory: Any | None = None,
    context: AssessContextProvider | None = None,
    detector: AnomalyDetector | None = None,
    semantic: SemanticParser | None = None,
    contracts: Any | None = None,
    id_suffix: str = "real01",
) -> list[Any]:
    """按共享依赖构造五个真智能体内环（不启动）。execute 只产文案，不持发送权。"""
    return [
        PerceiveAgent(
            transport=transport,
            agent_type=AgentType.PERCEIVE,
            agent_id=f"perceive.{id_suffix}",
            capabilities=["anomaly_detect", "trigger_recognize"],
            detector=detector,
            semantic=semantic,
            llm=llm,
        ),
        ExecuteAgent(
            transport=transport,
            agent_type=AgentType.EXECUTE,
            agent_id=f"execute.{id_suffix}",
            capabilities=["warn_publish"],
            llm=llm,
        ),
        AssessAgent(
            transport=transport,
            agent_type=AgentType.ASSESS,
            agent_id=f"assess.{id_suffix}",
            capabilities=["risk_assess"],
            llm=llm,
            memory=memory,
            context=context,
        ),
        PlanAgent(
            transport=transport,
            agent_type=AgentType.PLAN,
            agent_id=f"plan.{id_suffix}",
            capabilities=["task_decompose"],
            llm=llm,
            contracts=contracts,
        ),
        FeedbackAgent(
            transport=transport,
            agent_type=AgentType.FEEDBACK,
            agent_id=f"feedback.{id_suffix}",
            capabilities=["reach_track"],
            llm=llm,
            memory=memory,
        ),
    ]


def build_hazard_agents(transport: BusTransport, *, pool_workers: int = 8) -> list[AgentKit]:
    """构造灾种闭环的六个本体（不启动）。

    `pool_workers` 是灾种模块共享线程池的容量（部署键 `AEGIS_HAZARD_POOL_WORKERS`）。
    研判/仿真/预警三条腿都是阻塞调用（LLM 一次 11–20s），共用一个专用池而不是各开
    一个：六个本体本来就在同一条闭环上串行跑，各开各的等于把线程数乘六。
    """
    try:
        from aegis.agents.hazards.data_agent.agent import DataQualityAgent
        from aegis.agents.hazards.feedback_agent.agent import ChainAuditAgent
        from aegis.agents.hazards.hazard_registry import SUPPORTED_HAZARDS
        from aegis.agents.hazards.manager_agent.agent import HazardWorkflowAgent
        from aegis.agents.hazards.pool import configure_pool
        from aegis.agents.hazards.risk_agent.agent import FactorAssessAgent
        from aegis.agents.hazards.simulation_agent.agent import ScenarioSimAgent
        from aegis.agents.hazards.warning_agent.agent import WarningContentAgent
    except ImportError as exc:  # pragma: no cover - 取决于是否装了 extra
        raise HazardAgentsUnavailableError(f"灾种模块不可用（依赖未安装或导入失败）：{exc}；平台只跑自建五段内环") from exc

    configure_pool(pool_workers)
    # 灾种模块不支持平台独有的震入灾害链，能力域按它自己支持的五种灾种登记。
    hazard_types = list(SUPPORTED_HAZARDS)

    def _owner(capability: str) -> str:
        return spec_for(capability).owner

    return [
        DataQualityAgent(
            transport=transport,
            agent_type=AgentType.ASSESS,
            agent_id=_owner("data_quality"),
            capabilities=["data_quality"],
            hazard_types=hazard_types,
            version=HAZARD_VERSION,
        ),
        FactorAssessAgent(
            transport=transport,
            agent_type=AgentType.ASSESS,
            agent_id=_owner("factor_risk_assess"),
            capabilities=["factor_risk_assess"],
            hazard_types=hazard_types,
            version=HAZARD_VERSION,
        ),
        ScenarioSimAgent(
            transport=transport,
            agent_type=AgentType.ASSESS,
            agent_id=_owner("scenario_sim"),
            capabilities=["scenario_sim"],
            hazard_types=hazard_types,
            version=HAZARD_VERSION,
        ),
        WarningContentAgent(
            transport=transport,
            agent_type=AgentType.EXECUTE,
            agent_id=_owner("warn_content"),
            capabilities=["warn_content"],
            hazard_types=hazard_types,
            version=HAZARD_VERSION,
        ),
        ChainAuditAgent(
            transport=transport,
            agent_type=AgentType.FEEDBACK,
            agent_id=_owner("chain_audit"),
            capabilities=["chain_audit"],
            hazard_types=hazard_types,
            version=HAZARD_VERSION,
        ),
        HazardWorkflowAgent(
            transport=transport,
            agent_type=AgentType.PLAN,
            agent_id=_owner("hazard_workflow"),
            capabilities=["hazard_workflow"],
            hazard_types=hazard_types,
            version=HAZARD_WORKFLOW_VERSION,
            siblings=_loop_siblings(),
        ),
    ]


def _loop_siblings() -> dict[str, Any]:
    """闭环要寻址的五个兄弟本体（地址取自能力矩阵，不另写一份）。"""
    from aegis.agents.hazards.bus_context import SiblingRef

    siblings: dict[str, Any] = {}
    for capability in _LOOP_CAPABILITIES:
        spec = spec_for(capability)
        siblings[capability] = SiblingRef(
            capability=capability,
            agent_type=spec.agent_type,
            agent_id=spec.owner,
        )
    return siblings


def build_all_agents(
    transport: BusTransport,
    *,
    llm: Any | None = None,
    memory: Any | None = None,
    context: AssessContextProvider | None = None,
    detector: AnomalyDetector | None = None,
    semantic: SemanticParser | None = None,
    contracts: Any | None = None,
    id_suffix: str = "real01",
    pool_workers: int = 8,
) -> list[Any]:
    """构造平台上**全部**本体（不启动）：平台五段 + 灾种六体。

    灾种模块的依赖是可选 extra。没装时如实留一条 warning 并只返回平台五段——平台不能
    因为一个可选依赖装不上就整条不可用，但也不能假装那六个本体在册（它们一次都接不到
    单，这正是原模块当初拒绝注册另外五个本体的理由）。
    """
    agents: list[Any] = list(
        build_real_agents(
            transport,
            llm=llm,
            memory=memory,
            context=context,
            detector=detector,
            semantic=semantic,
            contracts=contracts,
            id_suffix=id_suffix,
        )
    )
    try:
        agents.extend(build_hazard_agents(transport, pool_workers=pool_workers))
    except HazardAgentsUnavailableError as exc:
        log.warning("灾种模块未装配，平台上只有自建五段内环", extra={"reason": str(exc)[:200]})
    return agents


async def start_real_agents(
    transport: BusTransport,
    *,
    llm: Any | None = None,
    memory: Any | None = None,
    context: AssessContextProvider | None = None,
    detector: AnomalyDetector | None = None,
    semantic: SemanticParser | None = None,
    contracts: Any | None = None,
    heartbeat_interval: float = 0.5,
    id_suffix: str = "real01",
) -> list[Any]:
    """构造并启动真智能体集：注册 + 心跳 + 订阅 agent_in。返回实例（调用方负责 stop）。"""
    agents = build_real_agents(
        transport,
        llm=llm,
        memory=memory,
        context=context,
        detector=detector,
        semantic=semantic,
        contracts=contracts,
        id_suffix=id_suffix,
    )
    for agent in agents:
        await agent.start(heartbeat_interval=heartbeat_interval)
    return agents


__all__ = [
    "HazardAgentsUnavailableError",
    "build_all_agents",
    "build_hazard_agents",
    "build_real_agents",
    "start_real_agents",
]
