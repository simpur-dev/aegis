"""
Simulation Agent 崩塌危岩仿真模型（MOCK）

【本文件角色】rockfall（崩塌危岩）灾种的玩具级确定性仿真模型。

【诚实声明 / 重要红线】
  本模型是 MOCK（MODEL_VERSION="MOCK-v0.1"）：所有输出都是 mock_ 前缀的
  0~1 归一化合成指标，由下方声明的确定性玩具规则计算。
  它【不是】任何有科学依据的落石运动学公式，输出不代表真实动能、
  弹跳高度、运动距离或到达时间，禁止用于实际预警决策。
  未来接入专业模型（扩展位：落石运动学弹跳-滚动-飞行模型 / 恢复系数法）时，
  必须重新核实输入数据需求、输出单位与模型验证，不能只替换计算公式。

【计算规则（MOCK 玩具规则，非科学公式）】
  1. 输入归一化（clip01 裁剪到 [0,1]）：
     v   = clip01(block_volume_m3 / 100)             # 危岩体量维度
     hgt = clip01(slope_height_m / 500)              # 坡高维度
     ang = clip01((slope_angle_deg - 30) / 60)       # 坡角维度（默认 60° → 0.5）
  2. 目标危险度（确定性加权合成，权重为演示约定）：
     target = clip01((0.20 + 0.40*v + 0.30*hgt + 0.10*ang) * SEV[severity_level])
  3. 时间步演化（线性爬升：前 3 步线性到达 target，之后保持不变）：
     h_t = target * min(1.0, (t + 1) / 3)
  4. 过程指标（静态过程描述量）：
     mock_rockfall_energy_index = clip01((0.30 + 0.45*v + 0.25*hgt) * SEV)
  5. 影响指标：
     mock_impact_index = clip01(0.55*h_final + 0.45*hgt)

【适用限制】参数合法范围、权重与演化速率均为演示用约定值，
  未经过任何实测数据或文献依据校验；severity 乘子是 MOCK 演示约定。
"""

import time

from aegis.agents_ext.simulation_agent.models.base_model import (
    MOCK_DISCLAIMER,
    SEVERITY_MULTIPLIER,
    BaseHazardSimulationModel,
    clip01,
)


class RockfallMockModel(BaseHazardSimulationModel):
    """崩塌危岩 MOCK 模型。指标：mock_hazard_index / mock_impact_index /
    mock_rockfall_energy_index，全部 0~1 归一化合成值。"""

    hazard_type = "rockfall"
    MODEL_VERSION = "MOCK-v0.1"
    simulation_mode = "mock"
    is_real_prediction = False
    disclaimer = MOCK_DISCLAIMER

    required_inputs = ["block_volume_m3", "slope_height_m"]
    optional_inputs = {"slope_angle_deg": 60.0}
    param_constraints = {
        "block_volume_m3": (0.01, 100.0, "危岩单体体积（立方米），演示用原始值"),
        "slope_height_m": (2.0, 500.0, "坡高（米）"),
        "slope_angle_deg": (30.0, 90.0, "坡度角（度，可选，默认 60）"),
    }
    required_metrics = ["mock_hazard_index", "mock_impact_index", "mock_rockfall_energy_index"]
    n_steps = 6
    real_model_note = "落石运动学（弹跳-滚动-飞行）/ 恢复系数法（接入前需核实数据需求与参数适用范围）"
    limitations_extra = ["崩塌玩具规则未考虑岩体结构面、降雨冻胀与防护工程等真实因素"]

    def run_simulation(self, scenario: dict, context: dict) -> dict:
        # ---- 测试钩子：前 n 次执行抛瞬态异常（正式契约的一部分）----
        hooks = scenario.get("test_hooks") or {}
        fail_n = int(hooks.get("fail_first_attempts", 0) or 0)
        if fail_n and int(context.get("attempt", 1)) <= fail_n:
            raise RuntimeError(f"模拟瞬态故障(MOCK test hook): 第 {context.get('attempt', 1)} 次执行被要求失败")

        t0 = time.perf_counter()
        sev = SEVERITY_MULTIPLIER.get(scenario.get("severity_level", "medium"), 1.0)

        # ---- 1. 输入归一化（规则见文件 docstring）----
        v = clip01(scenario["block_volume_m3"] / 100.0)
        hgt = clip01(scenario["slope_height_m"] / 500.0)
        ang = clip01((scenario.get("slope_angle_deg", 60.0) - 30.0) / 60.0)

        # ---- 2. 目标危险度 ----
        target = clip01((0.20 + 0.40 * v + 0.30 * hgt + 0.10 * ang) * sev)

        # ---- 3. 时间步演化：线性爬升 3 步后恒定 ----
        steps = []
        energy = clip01((0.30 + 0.45 * v + 0.25 * hgt) * sev)
        for t in range(self.n_steps):
            h = target * min(1.0, (t + 1) / 3.0)
            impact_t = clip01(0.55 * h + 0.45 * hgt)
            steps.append(
                {
                    "step": t,
                    "mock_hazard_index": round(h, 4),
                    "mock_rockfall_energy_index": round(energy, 4),
                    "mock_impact_index": round(impact_t, 4),
                }
            )

        # ---- 4. 终态指标 ----
        final = {
            "mock_hazard_index": round(target, 4),  # 线性爬升第 3 步起恒为 target
            "mock_rockfall_energy_index": round(energy, 4),
            "mock_impact_index": round(clip01(0.55 * target + 0.45 * hgt), 4),
        }
        return {
            "ok": True,
            "scenario_id": scenario.get("scenario_id", ""),
            "steps": steps,
            "final_indicators": final,
            "execution_info": {
                "steps_run": self.n_steps,
                "duration_sec": round(time.perf_counter() - t0, 6),
                "timed_out": False,
            },
        }
