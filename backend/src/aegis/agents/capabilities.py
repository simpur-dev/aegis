"""统一能力矩阵：capability（路由键）与 action（语义动词）的唯一对照表。

这张表是智能体层的**单一真源**，回答三个问题：

1. 平台上到底有哪些能力、每一项由谁承接（`agent_id`）、走哪个动作；
2. 每一项能力在代码里**有没有真的派发点**——注册了却没人派的能力就是"接了但接不了"。
   灾种模块的作者当年正是因此拒绝注册另外五个本体（"注册一个『接了但接不了』的本体
   诚实性更低"），这里用机器把这条纪律钉死，而不是靠人记得；
3. 派发点与注册表**一一对应**——派了却没人接，等于每次都走 `no_agent` 降级。

总线的路由键是 `(agent_type, capability)`（见 `bus/registry.py` 的 `candidates()`），
**不是 action**：action 只表达语义，capability 才决定谁接单。因此每一项能力必须由
**恰好一个**本体承接——两个本体声明同一项能力，`registry.pick()` 会在两者之间按
`(inflight, -served)` 取舍，"谁应答"就变成不可复现的随机数。

三项纪律由 `tests/unit/agents/test_capability_matrix.py` 守着，改这张表必须同步改派发点。
"""

from __future__ import annotations

from dataclasses import dataclass

from aegis.domain.enums import Action, AgentType


@dataclass(frozen=True, slots=True)
class CapabilitySpec:
    """一项能力的完整口径。

    `dispatched=False` 表示**平台不会派单**给这项能力：本体自己发事件（事件腿），
    或只被闭环内部调用。这类能力仍然要在册——它决定了本体的注册内容与能力路由的
    边界，只是没有"平台 → 智能体"的请求入口。
    """

    capability: str
    agent_type: AgentType
    action: Action
    owner: str
    dispatched: bool = True


#: 能力矩阵。顺序按流程阶段排，读起来就是平台的一条完整链路。
CAPABILITY_MATRIX: tuple[CapabilitySpec, ...] = (
    # ---------- 感知 ----------
    CapabilitySpec(
        capability="anomaly_detect",
        agent_type=AgentType.PERCEIVE,
        action=Action.ASSESS_HAZARD,
        owner="perceive.real01",
    ),
    CapabilitySpec(
        capability="trigger_recognize",
        agent_type=AgentType.PERCEIVE,
        action=Action.PERCEIVE_TRIGGER_HIT,
        owner="perceive.real01",
        # 事件腿：本体主动发 `perceive.trigger_hit`，平台不派单。
        dispatched=False,
    ),
    # ---------- 研判 ----------
    CapabilitySpec(
        capability="risk_assess",
        agent_type=AgentType.ASSESS,
        action=Action.ASSESS_HAZARD,
        owner="assess.real01",
    ),
    CapabilitySpec(
        capability="data_quality",
        agent_type=AgentType.ASSESS,
        action=Action.ASSESS_DATA_QUALITY,
        owner="assess.data01",
    ),
    CapabilitySpec(
        capability="factor_risk_assess",
        agent_type=AgentType.ASSESS,
        action=Action.ASSESS_FACTOR_RISK,
        owner="assess.factor01",
    ),
    CapabilitySpec(
        capability="scenario_sim",
        agent_type=AgentType.ASSESS,
        action=Action.ASSESS_SCENARIO_SIM,
        owner="assess.sim01",
        # 闭环内腿：只被灾种闭环调用（BUS_LEGS），不进平台热路径——
        # 它的输入是闭环上游的原生交付物，平台链路给不齐，硬喂就是制造假输入。
        dispatched=False,
    ),
    # ---------- 决策 ----------
    CapabilitySpec(
        capability="task_decompose",
        agent_type=AgentType.PLAN,
        action=Action.PLAN_STU,
        owner="plan.real01",
    ),
    CapabilitySpec(
        capability="hazard_workflow",
        agent_type=AgentType.PLAN,
        action=Action.PLAN_HAZARD_WORKFLOW,
        owner="plan.loop01",
    ),
    # ---------- 执行 ----------
    CapabilitySpec(
        capability="warn_publish",
        agent_type=AgentType.EXECUTE,
        action=Action.EXECUTE_WARN,
        owner="execute.real01",
    ),
    CapabilitySpec(
        capability="warn_content",
        agent_type=AgentType.EXECUTE,
        action=Action.EXECUTE_WARN_CONTENT,
        owner="execute.hazard01",
    ),
    # ---------- 反馈 ----------
    CapabilitySpec(
        capability="reach_track",
        agent_type=AgentType.FEEDBACK,
        action=Action.FEEDBACK_ATTRIBUTE,
        owner="feedback.real01",
    ),
    CapabilitySpec(
        capability="chain_audit",
        agent_type=AgentType.FEEDBACK,
        action=Action.FEEDBACK_CHAIN_AUDIT,
        owner="feedback.audit01",
        # 闭环内腿：审计的是闭环五腿交付物之间的自洽性，输入只有闭环才产得齐
        # （缺 data_result 会被审计工具记成 major 问题——那是假告警）。平台链路的
        # 一致性由各段的契约校验把守，不需要也不应该再花一次热路径预算。
        dispatched=False,
    ),
)

#: capability → 规格
CAPABILITY_SPECS: dict[str, CapabilitySpec] = {spec.capability: spec for spec in CAPABILITY_MATRIX}


def spec_for(capability: str) -> CapabilitySpec:
    """查一项能力的规格；不在矩阵里就是实现缺陷，不给"猜一个默认值"的余地。"""
    try:
        return CAPABILITY_SPECS[capability]
    except KeyError as exc:
        raise KeyError(f"能力 {capability!r} 不在能力矩阵里：先在 agents/capabilities.py 登记，再改派发点") from exc


def capabilities_for(agent_type: AgentType) -> tuple[str, ...]:
    """某阶段下全部能力名（装配时用它给本体填 `capabilities`）。"""
    return tuple(spec.capability for spec in CAPABILITY_MATRIX if spec.agent_type is agent_type)


__all__ = [
    "CAPABILITY_MATRIX",
    "CAPABILITY_SPECS",
    "CapabilitySpec",
    "capabilities_for",
    "spec_for",
]
