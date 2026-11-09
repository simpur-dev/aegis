"""外部实现（`agents_ext`）的运行时装配：**只注册它真正实现的契约动作**。

当前只有研判一个动作是一等实现（`assess.hazard`）。其余动作（plan/execute/feedback）
不注册——网关查不到承接方会记 `no_agent`，平台按既有本地降级轨走；这比注册一个
"接了但接不了"的本体诚实：后者会先被派单、再报错误，看起来像故障。

依赖（langgraph 等）是可选 extra：没装时抛 `ExternalAgentsUnavailableError`，
由容器如实退回自建内环。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aegis.agents.agentkit import AgentKit
from aegis.domain.enums import AgentType

if TYPE_CHECKING:
    from aegis.bus.transport import BusTransport


class ExternalAgentsUnavailableError(RuntimeError):
    """外部实现未装配（依赖没装 / 导入失败）：平台据此退回自建内环，而不是整条链路不可用。"""


def build_ext_agents(transport: BusTransport, llm: object | None = None, *, pool_workers: int = 8) -> list[AgentKit]:
    """构造外部实现的内环（不启动）；调用方负责 `start()`。

    `pool_workers` 是研判专用线程池容量（平台侧部署键 `AEGIS_EXT_ASSESS_POOL_WORKERS`），
    由装配层传入；不传时用模块默认值，便于出进程/独立使用。
    """
    try:
        from aegis.agents_ext.hazard_registry import SUPPORTED_HAZARDS
        from aegis.agents_ext.risk_agent.agent import RiskAssessAgent
    except ImportError as exc:  # pragma: no cover - 取决于是否装了 extra
        raise ExternalAgentsUnavailableError(f"外部智能体实现不可用（依赖未安装或导入失败）：{exc}；平台退回自建内环") from exc
    return [
        RiskAssessAgent(
            transport=transport,
            agent_type=AgentType.ASSESS,
            agent_id="assess.agents_ext",
            capabilities=["risk_assess"],
            hazard_types=list(SUPPORTED_HAZARDS),
            version="agents-ext-1.0.0",
            llm=llm,
            pool_workers=pool_workers,
        )
    ]


__all__ = ["ExternalAgentsUnavailableError", "build_ext_agents"]
