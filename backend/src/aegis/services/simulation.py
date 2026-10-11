"""推演沙盒（§3.6 / §4.4，D6）：多角色 × K情景 × T轮 → 次生灾害概率矩阵 + 预案对比。

平台 L4 服务，不是第六总线智能体（ephemeral 角色不注册总线、不进 SLA 热路径）。诚实边界：
- **数字冻结自遥测、由确定性数值模型推进**——LLM 不得发明数字；角色"行动"用可注入 policy，
  默认 `RulePolicy`（确定性、离线可复算），生产换成 LLM 内环不改变数值口径。
- 次生灾害概率、暴露度、预案增益系数为**未实测初值**（演示/专利素材，§3.6 验收=矩阵正确性抽查、非 SLA）；
  只保证模型单调性与可复现，不声称任何现场达标。
- 成本上限：K×T×角色×token 预算超限→自动降 K 并如实标 `downscaled`（§3.6 约束）。

产出对齐 §3.6：次生灾害概率矩阵（按降雨分档的触发率）、关键时间线、预案对比（转移/封路/不行动）。
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

ROLES: tuple[str, ...] = ("commander", "rescue", "residents", "monitor")
#: 预案对次生灾害/暴露的缓解系数（未实测初值，联调标定；仅演示相对效果，不声称现场数字）。
PLAYBOOK_MITIGATION: dict[str, float] = {"转移": 0.6, "封路": 0.4, "不行动": 0.0}


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


@runtime_checkable
class RolePolicy(Protocol):
    """角色行动面：给定环境与轮次，返回该轮采取的预案分支名。默认规则策略确定性可复算。"""

    def choose(self, *, role: str, round_index: int, intensity: float, preferred: str) -> str: ...


class RulePolicy:
    """默认薄基线策略：统一按给定 preferred 预案行动（离线可复算）。LLM 内环可替换之而不改数值口径。"""

    def choose(self, *, role: str, round_index: int, intensity: float, preferred: str) -> str:
        return preferred


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
class RoundState:
    round_index: int
    rain_mm: float
    mudlevel_cm: float
    mitigation: float
    trigger_probability: float


@dataclass(slots=True)
class SimulationReport:
    effective_scenarios: int
    requested_scenarios: int
    downscaled: bool
    probability_matrix: list[dict[str, Any]]
    timeline: list[dict[str, Any]]
    playbook_comparison: list[dict[str, Any]]

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
        }


class Sandbox:
    def __init__(self, model: DebrisFlowModel | None = None, policy: RolePolicy | None = None) -> None:
        self.model = model or DebrisFlowModel()
        self.policy = policy or RulePolicy()

    def _effective_scenarios(self, config: SimConfig) -> tuple[int, bool]:
        per_round = max(1, config.rounds) * max(1, len(config.roles)) * max(1, config.per_role_tokens)
        k = max(1, config.scenarios)
        if k * per_round > config.token_budget:
            capped = max(1, config.token_budget // per_round)
            return capped, capped < k
        return k, False

    def run(self, config: SimConfig) -> SimulationReport:
        eff, downscaled = self._effective_scenarios(config)
        rng = random.Random(config.seed)
        matrix_rows = self._matrix(config, eff, rng)
        timeline = self._timeline(config, seed=config.seed)
        comparison = [self._playbook_effect(config, pb) for pb in PLAYBOOK_MITIGATION]
        return SimulationReport(
            effective_scenarios=eff,
            requested_scenarios=config.scenarios,
            downscaled=downscaled,
            probability_matrix=matrix_rows,
            timeline=timeline,
            playbook_comparison=comparison,
        )

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

    def _timeline(self, config: SimConfig, *, seed: int) -> list[dict[str, Any]]:
        mitigation = PLAYBOOK_MITIGATION.get(config.playbook, 0.0)
        rng = random.Random(seed)
        rain = self._scenario_rain(rng, config)
        rows: list[dict[str, Any]] = []
        for t in range(max(1, config.rounds)):
            mud = self.model.mudlevel_cm(rain + t * 2)
            action = self.policy.choose(role=config.roles[0], round_index=t, intensity=mud, preferred=config.playbook)
            rows.append(
                {
                    "round": t,
                    "rain_mm": rain,
                    "mudlevel_cm": mud,
                    "action": action,
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
