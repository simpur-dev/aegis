"""真智能体装配工厂（§3 / MA"五智能体 API 版"）：把已建内环按共享依赖挂上总线。

对标既有 `start_mock_agents`（mock 是平台侧联调替身）；这里拉起【真】智能体内环：同一个 LLM 网关
实例（可选）、共享记忆门面、注入的 detector、可选契约自证。缺哪个依赖就如实降级，不塞会谎报的替身。

execute 暂不装配：现 chain._execute 直信 execute agent 的 channel_results 作触达证据、而自建 agent
无 sanctioned 发送通道——硬装配=伪造触达（违铁律7），发送权归属未决（见进度看板 task #3）。宁可少一个，
不造一个会说谎的。
"""

from __future__ import annotations

from typing import Any

from aegis.agents.assess import AssessAgent, AssessContextProvider
from aegis.agents.feedback import FeedbackAgent
from aegis.agents.perceive import AnomalyDetector, PerceiveAgent
from aegis.agents.plan import PlanAgent
from aegis.bus.transport import BusTransport
from aegis.domain.enums import AgentType


def build_real_agents(
    transport: BusTransport,
    *,
    llm: Any | None = None,
    memory: Any | None = None,
    context: AssessContextProvider | None = None,
    detector: AnomalyDetector | None = None,
    contracts: Any | None = None,
    id_suffix: str = "real01",
) -> list[Any]:
    """按共享依赖构造四个真智能体内环（不启动）。execute 见模块 docstring 的装配缺口说明。"""
    return [
        PerceiveAgent(
            transport=transport,
            agent_type=AgentType.PERCEIVE,
            agent_id=f"perceive.{id_suffix}",
            capabilities=["anomaly_detect", "trigger_recognize"],
            detector=detector,
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


async def start_real_agents(
    transport: BusTransport,
    *,
    llm: Any | None = None,
    memory: Any | None = None,
    context: AssessContextProvider | None = None,
    detector: AnomalyDetector | None = None,
    contracts: Any | None = None,
    heartbeat_interval: float = 0.5,
    id_suffix: str = "real01",
) -> list[Any]:
    """构造并启动真智能体集：注册 + 心跳 + 订阅 agent_in。返回实例（调用方负责 stop）。"""
    agents = build_real_agents(
        transport, llm=llm, memory=memory, context=context, detector=detector, contracts=contracts, id_suffix=id_suffix
    )
    for agent in agents:
        await agent.start(heartbeat_interval=heartbeat_interval)
    return agents
