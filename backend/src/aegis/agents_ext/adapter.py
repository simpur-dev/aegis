"""外部智能体模块与平台契约之间的**适配层**（契约壳样板）。

## 这一层现在只做一件事

实现平台契约壳认识的唯一内环形态：`AgentKit.handle(request: AgentMessage) -> Reply`。
订阅、心跳、幂等、deadline、错误回执这些**契约壳的样板**由 `AgentKit` 提供；
"两套语义怎么对"（契约 payload ↔ 她的入口参数、她的结果 ↔ 契约响应）全部在
`contract_face.py` 与 `levels.py` 里。

**为什么这样分层**：翻译逻辑混进执行壳，会让"改一条映射"需要读懂整份订阅/幂等/超时代码，
而执行壳与翻译的**变化理由完全不同**（前者跟契约走，后者跟外部实现走）。
现在：
- `adapter.py`（本文件）：AgentKit 样板 + 动作分派；
- `contract_face.py`：契约语义 ↔ 外部语义的翻译；
- `levels.py`：等级档位与状态四态的口径（外部语义的知识）。

## 两种运行模型怎么接起来

她的实现是**单进程 LangGraph 图编排**（`run_xxx_agent(...) -> dict`，进程内互相调用）；
平台契约是**分布式总线**（智能体是总线上的独立服务，唯一交互载体是 `AgentMessage v1`，
见 `contracts/AGENT_INTEGRATION_SPEC.v1.md`）。本层把前者适配成后者认识的形态，
于是她那整套图、规则引擎、五灾种注册表就成了平台契约下的一个合法实现——
与平台自建内环（`aegis.agents`）**并列**，由 `AEGIS_AGENT_IMPL=builtin|ext` 选择，默认 builtin。

## 三条不能含糊的口径（在 contract_face/levels 里落地）

1. **数据面不假装**：她的数据工具是内存 Mock 源，结论带 `source=agents_ext_mock`，
   读的人一眼能看出数据出处——不冒充平台实测数据。
2. **风险等级口径唯一**：平台 1–5 与她的 high/medium/low 双向映射，不引第三套口径。
3. **"没法判断"不能变成"没有风险"**：她的 status 四态逐个区分，只有 success 才产出定级。
"""

from __future__ import annotations

import logging
from typing import Any

from aegis.agents.agentkit import AgentKit, Reply
from aegis.agents_ext.contract_face import ExternalAgentError, assess_face
from aegis.agents_ext.levels import CODE_ACTION_UNSUPPORTED
from aegis.bus.transport import BusTransport
from aegis.domain.enums import AgentType
from aegis.domain.messages import AgentMessage, ModelInfo

log = logging.getLogger("aegis.agents_ext.adapter")


class ExternalAgentsUnavailableError(RuntimeError):
    """外部实现未装配（依赖没装 / 导入失败）：平台据此退回自建内环，而不是整条链路不可用。"""


def _load() -> dict[str, Any]:
    """延迟导入她的实现：依赖（langgraph 等）是可选 extra，没装时不能炸在导入期。"""
    try:
        from aegis.agents_ext.data_agent.main import run_data_agent
        from aegis.agents_ext.feedback_agent.main import run_feedback_agent
        from aegis.agents_ext.manager_agent.main import run_manager_agent
        from aegis.agents_ext.risk_agent.main import run_risk_agent
        from aegis.agents_ext.warning_agent.main import run_warning_agent
    except ImportError as exc:  # pragma: no cover - 取决于是否装了 extra
        raise ExternalAgentsUnavailableError(f"外部智能体实现不可用（依赖未安装或导入失败）：{exc}；平台退回自建内环") from exc
    return {
        "data": run_data_agent,
        "feedback": run_feedback_agent,
        "manager": run_manager_agent,
        "risk": run_risk_agent,
        "warning": run_warning_agent,
    }


def available() -> bool:
    """外部实现此刻能不能用（依赖在位且能导入）。装配点用它决定"要不要换实现"。"""
    try:
        _load()
    except ExternalAgentsUnavailableError:
        return False
    return True


class ExternalAgentAdapter(AgentKit):
    """把她的一个 agent 入口适配成平台契约内环。

    本类**只做契约壳的样板与动作分派**；语义翻译在 `contract_face`。
    """

    def __init__(
        self,
        transport: BusTransport,
        *,
        agent_type: AgentType,
        agent_id: str,
        capabilities: list[str],
        action: str,
        version: str = "agents-ext-1.0.0",
    ) -> None:
        super().__init__(
            transport=transport,
            agent_type=agent_type,
            agent_id=agent_id,
            capabilities=capabilities,
            version=version,
        )
        self._action = action
        self._runners = _load()

    async def handle(self, request: AgentMessage) -> Reply:
        if request.action != self._action:
            # 动作不匹配：按契约走 error 通道（`E_EXT_ACTION_UNSUP`），而不是静默或猜着做。
            raise ExternalAgentError(
                CODE_ACTION_UNSUPPORTED,
                f"外部智能体 {self.agent_id} 不处理动作 {request.action}（本实例处理 {self._action}）",
            )
        payload = dict(request.payload or {})

        if self._action == "assess.hazard":
            result = await self._run_assess(payload)
            return Reply(payload=result, model_info=self._provenance())

        # 其余动作（plan.stu / execute.warn / feedback.attribute）的语义翻译尚未接线：
        # 先按契约明确回"不支持"（走 error 通道），而不是悄悄用一种猜的方式执行。
        # 接线时每加一个动作，都在 `contract_face` 里补一条对应函数，不在这里堆分支。
        raise ExternalAgentError(
            CODE_ACTION_UNSUPPORTED,
            f"外部实现暂未接入动作 {request.action}",
        )

    async def _run_assess(self, payload: dict[str, Any]) -> dict[str, Any]:
        """研判：调用她的入口（同步的 LangGraph 运行）并收结果。

        她的入口是同步函数（图在同进程内跑完），所以放在线程里跑——
        直接在事件循环里调会阻塞总线上的其他消息。

        **代价（如实记着）**：线程里的图**取消不掉**——`AgentKit` 的 deadline 只取消协程，
        超时后那张图仍在跑（内部还有自己的重试环），会继续烧 CPU 与模型调用。
        真要掐断得走进程隔离，不在本期范围（ADR-0011 的"已知限制"）。
        """
        import asyncio

        return await asyncio.to_thread(assess_face, payload, self._runners["risk"])

    @staticmethod
    def _provenance() -> ModelInfo:
        """溯源字段（D10）：**实现名不是模型名**。

        原先这里写的是 `model="agents_ext"`——那是实现的名字，冒充模型名会让
        "哪条结论出自哪个模型"变成一句废话。真实模型名由外部模块自己的配置面给，
        这里读不到就**留空**（缺省即未知，不臆造模型/版本）。
        """
        from aegis.agents_ext import config

        return ModelInfo(model=config.ZHIPU_MODEL or None, model_version=None, prompt_version=None)


def build_ext_agents(
    transport: BusTransport,
    *,
    id_suffix: str = "ext01",
) -> list[Any]:
    """构造外部实现的五个内环（不启动）。

    与 `aegis.agents.factory.build_real_agents` 同一形状：调用方负责 `start()`。
    依赖没装 → 抛 `ExternalAgentsUnavailableError`，由装配点如实退回自建内环。
    """
    specs = (
        # 平台往 PERCEIVE 段也发 `assess.hazard`（探测灾种），不是 perceive.anomaly
        (AgentType.PERCEIVE, "assess.hazard", ["anomaly_detect", "trigger_recognize"]),
        (AgentType.ASSESS, "assess.hazard", ["risk_assess"]),
        (AgentType.PLAN, "plan.stu", ["task_decompose"]),
        (AgentType.EXECUTE, "execute.warn", ["warn_publish"]),
        # 平台发给 FEEDBACK 段的是 `feedback.attribute`；`feedback.status` 是平台自己往 ops
        # 流广播的事件（stages.py），不会发给智能体——注册成它就永远接不到单。
        (AgentType.FEEDBACK, "feedback.attribute", ["reach_track"]),
    )
    return [
        ExternalAgentAdapter(
            transport,
            agent_type=agent_type,
            agent_id=f"{agent_type.value}.{id_suffix}",
            capabilities=capabilities,
            action=action,
        )
        for agent_type, action, capabilities in specs
    ]


__all__ = [
    "ExternalAgentAdapter",
    "ExternalAgentsUnavailableError",
    "available",
    "build_ext_agents",
]
