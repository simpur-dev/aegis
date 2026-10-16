"""推演沙盒（§3.6 / §4.4，D6）：多角色 × K情景 × T轮 → 次生灾害概率矩阵 + 预案对比。

平台 L4 服务，不是第六总线智能体（ephemeral 角色不注册总线、不进 SLA 热路径）。诚实边界：
- **数字冻结自遥测、由确定性数值模型推进**——LLM 不得发明数字；角色只做"三档预案选一档"，
  行动面住在 `aegis.simulation.roles`（`RulePolicy` 确定性档 / `LlmRolePolicy` 增强档），
  两者同一个协议，`Sandbox` 不知道对面是谁，所以"换内环"不改变数值口径。
- 次生灾害概率、暴露度、预案增益系数为**未实测初值**（演示/专利素材，§3.6 验收=矩阵正确性抽查、非 SLA）；
  只保证模型单调性与可复现，不声称任何现场达标。
- 成本上限：K×T×角色×token 预算超限→自动降 K 并如实标 `downscaled`（§3.6 约束）。
- `run` 是 async 的：每轮要问四个角色，而角色那一层可能是 LLM。数值部分仍然纯函数，
  所以"同种子两次跑出逐字节相同的报告"这条只在 `RulePolicy` 档下成立（那是可复现基线档）。

产出对齐 §3.6：次生灾害概率矩阵（按降雨分档的触发率）、关键时间线（每轮四个角色各选一档，
再由 `aggregate_actions` 折成本轮生效预案）、预案对比（转移/封路/不行动）。
角色的票只决定"用表里哪一行系数"，不决定系数本身——这是"LLM 不得发明数字"能同时守住
"多角色真在推演"的位置。
两套口径要分清：概率矩阵是 K 个情景在**声明预案**下的抽样扫参（不逐情景跑角色，否则一次推演
要点 K×T×4 次模型），时间线才是角色票的落点；`playbook_comparison` 则是同一强度下的反事实对比。
"""

from __future__ import annotations

import math
import random
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from aegis.simulation.roles import ROLES_BY_KEY, LlmRolePolicy, RoleChooser, RulePolicy

__all__ = [
    "PLAYBOOK_MITIGATION",
    "ROLES",
    "DebrisFlowModel",
    "LakeOutburstModel",
    "RoleChooser",
    "RulePolicy",
    "Sandbox",
    "SimConfig",
    "SimulationReport",
    "aggregate_actions",
    "make_sandbox",
    "secondary_trigger_probability",
]

ROLES: tuple[str, ...] = tuple(ROLES_BY_KEY)
#: 预案对次生灾害/暴露的缓解系数（未实测初值，联调标定；仅演示相对效果，不声称现场数字）。
PLAYBOOK_MITIGATION: dict[str, float] = {"转移": 0.6, "封路": 0.4, "不行动": 0.0}


def aggregate_actions(actions: Mapping[str, str], *, fallback: str) -> str:
    """§3.6 的汇聚器：把一轮的角色票折成实际生效的那一档预案。

    没有这一步，四个角色就只是报告里四列装饰——选了什么不影响任何数字，"多角色推演"
    就成了包装。汇聚规则要能口述、能复算：
    - 多数票生效；
    - 平票取缓解系数更高的一档。写成"随机取一票"的话，同一颗种子换个角色顺序就换个
      概率矩阵，可复现这条就废了；
    - 白名单外的票直接丢弃（表里没有它的缓解系数，不能拿未定义的档位去推数字），
      全被丢弃时用 `fallback`（本轮声明的预案），因为沙盒不该因为一次坏输入把场景改掉。
    """
    votes: dict[str, int] = {}
    for action in actions.values():
        if action in PLAYBOOK_MITIGATION:
            votes[action] = votes.get(action, 0) + 1
    if not votes:
        return fallback
    ranked = sorted(votes.items(), key=lambda kv: (kv[1], PLAYBOOK_MITIGATION[kv[0]], kv[0]))
    return ranked[-1][0]


@dataclass(frozen=True, slots=True)
class DebrisFlowModel:
    """泥石流量化：泥位 = 基准 + 增益 × max(0, 降雨−阈值)。降雨↑ ⇒ 泥位单调↑。"""

    rain_threshold_mm: float = 20.0
    gain_cm_per_mm: float = 0.15

    def mudlevel_cm(self, rain_10min_mm: float, base_cm: float = 0.0) -> float:
        return round(base_cm + self.gain_cm_per_mm * max(0.0, rain_10min_mm - self.rain_threshold_mm), 3)


@dataclass(frozen=True, slots=True)
class LakeOutburstModel:
    """冰湖溃决流量衰减：Q(t) = 峰值 × exp(−t/τ)。时间↑ ⇒ 流量单调↓。"""

    tau_hours: float = 6.0

    def discharge_m3s(self, peak_q: float, hours: float) -> float:
        return round(peak_q * math.exp(-max(0.0, hours) / self.tau_hours), 3)


def secondary_trigger_probability(intensity: float, mitigation: float) -> float:
    """由泥位强度与预案缓解推出的次生灾害触发概率（单调：强度↑概率↑、缓解↑概率↓）。"""
    p = 0.05 + 0.05 * intensity * (1.0 - max(0.0, min(1.0, mitigation)))
    return round(max(0.0, min(1.0, p)), 4)


@dataclass(slots=True)
class SimConfig:
    scenarios: int = 8
    rounds: int = 5
    rain_base_mm: float = 40.0
    rain_spread_mm: float = 25.0
    playbook: str = "转移"
    roles: tuple[str, ...] = ROLES
    per_role_tokens: int = 800
    token_budget: int = 200_000
    seed: int = 20261011


@dataclass(slots=True)
class SimulationReport:
    effective_scenarios: int
    requested_scenarios: int
    downscaled: bool
    probability_matrix: list[dict[str, Any]]
    timeline: list[dict[str, Any]]
    playbook_comparison: list[dict[str, Any]]
    #: 角色档的事实：用哪种 policy、问了几次、几次回落。没有这一格，
    #: "四个角色都真选了"与"角色只是列表里四个字符串"在报告上看不出差别。
    roles: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "cost": {
                "requested_scenarios": self.requested_scenarios,
                "effective_scenarios": self.effective_scenarios,
                "downscaled": self.downscaled,
            },
            "probability_matrix": self.probability_matrix,
            "timeline": self.timeline,
            "playbook_comparison": self.playbook_comparison,
            "roles": dict(self.roles),
        }


class Sandbox:
    def __init__(self, model: DebrisFlowModel | None = None, policy: RoleChooser | None = None) -> None:
        self.model = model or DebrisFlowModel()
        self.policy = policy or RulePolicy()

    def _effective_scenarios(self, config: SimConfig) -> tuple[int, bool]:
        per_round = max(1, config.rounds) * max(1, len(config.roles)) * max(1, config.per_role_tokens)
        k = max(1, config.scenarios)
        if k * per_round > config.token_budget:
            capped = max(1, config.token_budget // per_round)
            return capped, capped < k
        return k, False

    async def run(self, config: SimConfig) -> SimulationReport:
        eff, downscaled = self._effective_scenarios(config)
        rng = random.Random(config.seed)
        matrix_rows = self._matrix(config, eff, rng)
        timeline = await self._timeline(config, seed=config.seed)
        comparison = [self._playbook_effect(config, pb) for pb in PLAYBOOK_MITIGATION]
        return SimulationReport(
            effective_scenarios=eff,
            requested_scenarios=config.scenarios,
            downscaled=downscaled,
            probability_matrix=matrix_rows,
            timeline=timeline,
            playbook_comparison=comparison,
            roles=self._roles_block(config, timeline),
        )

    def _roles_block(self, config: SimConfig, timeline: list[dict[str, Any]]) -> dict[str, Any]:
        """把角色档说清楚：谁在选、选了几次、其中几次其实是回落。

        `rounds_asked` 取"每轮 × 每角色"的应问次数，与 policy 自己记的 `calls` 对比，
        就能看出有没有角色整条腿没被问（装配漏接的表现正是"报告看起来完整"）。
        """
        asked = max(1, config.rounds) * len(config.roles)
        block: dict[str, Any] = {
            "policy": getattr(self.policy, "name", type(self.policy).__name__),
            "roles": [
                ROLES_BY_KEY[key].as_row() if key in ROLES_BY_KEY else {"key": key, "title": key, "concern": ""} for key in config.roles
            ],
            "rounds": max(1, config.rounds),
            "asked": asked,
            "calls": int(getattr(self.policy, "calls", asked)),
            "fallbacks": int(getattr(self.policy, "fallbacks", 0)),
            "distinct_actions": sorted({action for row in timeline for action in row["actions"].values()}),
        }
        if block["calls"] < asked:
            block["note"] = f"应问 {asked} 次、实问 {block['calls']} 次：有角色没被问到"
        return block

    def _scenario_rain(self, rng: random.Random, config: SimConfig) -> float:
        return round(config.rain_base_mm + rng.uniform(-config.rain_spread_mm, config.rain_spread_mm), 2)

    def _secondary_rate(self, rain_mm: float, mitigation: float, rounds: int) -> float:
        # 峰值强度用最大泥位近似；触发率 = T 轮里触发概率超过半的轮占比。
        peak = max(self.model.mudlevel_cm(rain_mm + step * 2) for step in range(max(1, rounds)))
        p = secondary_trigger_probability(peak, mitigation)
        return p

    def _matrix(self, config: SimConfig, effective: int, rng: random.Random) -> list[dict[str, Any]]:
        mitigation = PLAYBOOK_MITIGATION.get(config.playbook, 0.0)
        buckets: dict[int, list[float]] = {}
        for _ in range(effective):
            rain = self._scenario_rain(rng, config)
            rate = self._secondary_rate(rain, mitigation, config.rounds)
            buckets.setdefault(int(rain // 10) * 10, []).append(rate)
        return [
            {
                "rain_bucket_mm": f"{low}-{low + 10}",
                "scenarios": len(vals),
                "mean_secondary_rate": round(sum(vals) / len(vals), 4),
                "max_secondary_rate": round(max(vals), 4),
            }
            for low, vals in sorted(buckets.items())
        ]

    async def _timeline(self, config: SimConfig, *, seed: int) -> list[dict[str, Any]]:
        """每轮把在场角色都问一遍，再把四票汇聚成本轮实际生效的预案。

        §3.6 写的是"角色智能体 schema 约束行动 → 汇聚器聚合 → 数值模型推进"，所以这里带出的是
        `actions: {角色: 档位}` + `applied_playbook`，而不是单个动作——只有四个角色的选择都在台账上，
        报告页才可能解释"为什么这一轮转移、那一轮封路"。
        数字仍然只来自 `PLAYBOOK_MITIGATION` 这张冻结表和遥测推出来的强度：角色只能改"用哪一行表"，
        不能改表里的系数，也不能改强度。
        """
        rng = random.Random(seed)
        rain = self._scenario_rain(rng, config)
        rows: list[dict[str, Any]] = []
        for t in range(max(1, config.rounds)):
            mud = self.model.mudlevel_cm(rain + t * 2)
            actions: dict[str, str] = {}
            for role in config.roles:
                actions[role] = await self.policy.choose(role=role, round_index=t, intensity=mud, preferred=config.playbook)
            applied = aggregate_actions(actions, fallback=config.playbook)
            mitigation = PLAYBOOK_MITIGATION.get(applied, 0.0)
            rows.append(
                {
                    "round": t,
                    "rain_mm": rain,
                    "mudlevel_cm": mud,
                    "actions": actions,
                    "applied_playbook": applied,
                    "mitigation": mitigation,
                    "trigger_probability": secondary_trigger_probability(mud, mitigation),
                }
            )
        return rows

    def _playbook_effect(self, config: SimConfig, playbook: str) -> dict[str, Any]:
        mitigation = PLAYBOOK_MITIGATION.get(playbook, 0.0)
        # 暴露度用"未缓解的降雨强度代理 × (1−缓解系数)"，单调：缓解越强、暴露越低（演示口径，非现场数）。
        exposure = round((config.rain_base_mm + config.rain_spread_mm) * (1.0 - mitigation), 3)
        rate = self._secondary_rate(config.rain_base_mm, mitigation, config.rounds)
        return {"playbook": playbook, "mitigation": mitigation, "mean_secondary_rate": rate, "exposure_proxy": exposure}


def make_sandbox(settings: Any, *, llm_gateway: Any = None, trace_id: str = "") -> Sandbox:
    """按配置与 LLM 腿在位情况挑一档角色 policy，**每次调用都新建**。

    为什么要新建而不是共享一个 `Sandbox`：LLM 档的 policy 自带 `calls`/`fallbacks` 计数，
    跨请求复用会把上一推演的回落次数算到这一次头上——那一格就从事实变成了噪声。
    默认 `rule` 档：开 LLM 档要 `simulation_roles_llm=true` 且网关在位，
    报告里的 `roles.policy` 说的就是这一份报告实际用了哪一档。
    """
    if getattr(settings, "simulation_roles_llm", False) and llm_gateway is not None and getattr(llm_gateway, "available", False):
        return Sandbox(policy=LlmRolePolicy(llm_gateway, trace_id=trace_id))
    return Sandbox(policy=RulePolicy())
