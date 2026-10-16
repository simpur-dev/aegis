"""推演沙盒的角色侧（§4.4 / §3.6）：ephemeral 角色智能体 + 它们的行动面。

数值不在这儿。`services/simulation.py` 里那两个模型才是数字的出处（降雨→泥位、峰值流量衰减），
角色只做一件事：**在给定三档预案里选一档，并说为什么**。这条界线是方案的红线
（"数字冻结自遥测，LLM 不得发明数字"），也是这套演示能被复算的前提——
一旦模型能吐数字，同一颗种子两次跑出来的概率矩阵就不可能一致。

`RulePolicy`（确定性薄基线）是默认档，`LlmRolePolicy` 是接上 LLM 时的增强档：
两者实现同一个协议，`Sandbox` 不知道对面是谁，所以"换内环"不改变数值口径。
LLM 那一档失败要塌在自己腿上（回落到该轮给定的预案并计数），
不能把一次已经算出来的推演报告整体变成 503。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: 三档预案：与 `services/simulation.py` 的 `PLAYBOOK_MITIGATION` 同集合。
#: 白名单必须逐字给进提示词——只说"取其一"，模型会回"组织群众转移"这种中文描述，
#: schema 当场拒收，这条腿就结构上不可能成功（assess/plan 都栽过一次）。
ACTIONS: tuple[str, ...] = ("转移", "封路", "不行动")


@dataclass(frozen=True, slots=True)
class RoleProfile:
    """一个 ephemeral 角色的画像：它是谁、在意什么、看得见的字段有哪些。"""

    key: str
    title: str
    concern: str

    def as_row(self) -> dict[str, str]:
        return {"key": self.key, "title": self.title, "concern": self.concern}


#: 论文 §3.2.3 的四个角色 + 各自立场。写死在这里是因为它是**设计事实**，不是运行时数据。
ROLE_PROFILES: tuple[RoleProfile, ...] = (
    RoleProfile("commander", "指挥员", "要在响应迟滞与误报代价之间取舍，倾向可回退的决策"),
    RoleProfile("rescue", "救援队", "关心通路是否可通行、作业面暴露时长"),
    RoleProfile("residents", "受影响群众", "关心预警是否来得及、转移路线是否清楚"),
    RoleProfile("monitor", "监测员", "关心读数变化趋势与是否需要加密观测"),
)

ROLES_BY_KEY: Mapping[str, RoleProfile] = {profile.key: profile for profile in ROLE_PROFILES}


class RoleAction(BaseModel):
    """角色的 schema 约束产出：**只有档位与理由，没有数字字段**。

    不给它任何数值字段是结构性的防幻觉闸：模型没有地方写数字，就不会有数字被当成实测。
    `action` 逐字校验白名单；rationale 只作叙述，不进任何算式。
    """

    model_config = ConfigDict(extra="forbid")

    action: str = Field(min_length=2, max_length=12)
    rationale: str = Field(default="", max_length=200)

    @field_validator("action")
    @classmethod
    def _action_in_whitelist(cls, value: str) -> str:
        text = value.strip()
        if text not in ACTIONS:
            raise ValueError(f"action 只能逐字取 {list(ACTIONS)} 之一，收到 {value!r}")
        return text


class RoleChooser(Protocol):
    """异步角色行动面（`Sandbox` 唯一认识的接口）。"""

    async def choose(self, *, role: str, round_index: int, intensity: float, preferred: str, mudlevel_unit: str = "cm") -> str: ...


class RulePolicy:
    """确定性薄基线：按给定预案执行，永远离线可复算（也是没接 LLM 时的那一档）。"""

    name = "rule"

    async def choose(self, *, role: str, round_index: int, intensity: float, preferred: str, mudlevel_unit: str = "cm") -> str:
        return preferred


class LlmRolePolicy:
    """LLM 角色内环：每个角色每轮一次结构化调用，只回档位。

    降级口径是这一档存在的前提：单次调用失败/超预算 → 该轮回落 `preferred` 并记一次
    `fallbacks`。**不**因为模型腿抖了就整份报告作废（推演在演练视野，不在 SLA 热路径），
    也不静默——`fallbacks` 与 `calls` 会随报告外显，读的人能看出这份推演有几轮是模型选的。
    """

    name = "llm"

    def __init__(self, gateway: Any, *, trace_id: str = "") -> None:
        self._gateway = gateway
        self._trace_id = trace_id
        self.calls = 0
        self.fallbacks = 0

    async def choose(self, *, role: str, round_index: int, intensity: float, preferred: str, mudlevel_unit: str = "cm") -> str:
        """一次结构化调用换一档预案；任何不合规都只塌这一轮，不把整份推演拖下水。

        宽 `except` 是有意的：这一档的失败面很杂（超时、熔断、schema 拒收、修复轮用尽），
        而对报告的语义只有一种——"这一轮不是模型选的"。具体为什么抖，网关自己的
        span/用量账本里记着，不在这里再抄一份分类。
        """
        profile = ROLES_BY_KEY.get(role)
        if profile is None or self._gateway is None:
            self.fallbacks += 1
            return preferred
        self.calls += 1
        try:
            payload = await self._gateway.chat_structured(
                [],
                schema=RoleAction,
                task="simulation",
                agent_id=f"sim.{role}",
                trace_id=self._trace_id,
                prompt_key="simulation",
                prompt_vars={
                    "role": profile.title,
                    "concern": profile.concern,
                    "round": round_index,
                    "intensity": f"{intensity}{mudlevel_unit}",
                    "actions": "、".join(ACTIONS),
                    "preferred": preferred,
                },
            )
            # 再校验一次不是重复劳动：`chat_structured` 的闸在它自己的实现里，
            # 而这一档要防的是"拿到个不是 RoleAction 的东西"（替身、降级路径、修复轮回直 dict）。
            # 读 `payload.action` 之前不过 schema，越界写法就会直接进票箱。
            action = RoleAction.model_validate(payload)
        except Exception:
            self.fallbacks += 1
            return preferred
        return action.action
