"""
Simulation Agent 冰岩雪崩仿真模型（MOCK）

【本文件角色】avalanche（冰岩雪崩）灾种的玩具级确定性仿真模型。

【诚实声明 / 重要红线】
  本模型是 MOCK（MODEL_VERSION="MOCK-v0.1"）：所有输出都是 mock_ 前缀的
  0~1 归一化合成指标，由下方声明的确定性玩具规则计算。
  它【不是】任何有科学依据的雪崩动力学公式，输出不代表真实速度、
  冲击压力、运动距离或到达时间，禁止用于实际预警决策。
  未来接入专业模型（扩展位：α 系数能量线法 / Voellmy-Salm 模型）时，
  必须重新核实输入数据需求、输出单位与模型验证，不能只替换计算公式。

【计算规则（MOCK 玩具规则，非科学公式）】
  1. 输入归一化（clip01 裁剪到 [0,1]）：
     vol  = clip01((log10(release_volume_m3) - 1) / 5)    # 释放体量维度（对数尺度，10→0，1e6→1）
     drop = clip01(vertical_drop_m / 3000)                # 落差维度
     ang  = clip01((slope_angle_deg - 20) / 40)           # 坡角维度（默认 35° → 0.375）
  2. 目标危险度（确定性加权合成，权重为演示约定）：
     target = clip01((0.20 + 0.40*vol + 0.30*drop + 0.10*ang) * SEV[severity_level])
  3. 时间步演化（先快后慢的幂次饱和）：
     h_t = target * (1 - (1 - (t + 1)/n_steps) ** 2)
  4. 过程指标（静态过程描述量）：
     mock_avalanche_power_index = clip01((0.25 + 0.45*vol + 0.30*drop) * SEV)
  5. 影响指标：
     mock_impact_index = clip01(0.55*h_final + 0.25*drop + 0.20*vol)

【适用限制】参数合法范围、权重与演化速率均为演示用约定值，
  未经过任何实测数据或文献依据校验；severity 乘子是 MOCK 演示约定。
"""

import math
import time

from aegis.agents.hazards.hazard_registry import ICE_ROCK_SNOW_AVALANCHE
from aegis.agents.hazards.simulation_agent.models.base_model import (
    MOCK_DISCLAIMER,
    SEVERITY_MULTIPLIER,
    BaseHazardSimulationModel,
    clip01,
)


class AvalancheMockModel(BaseHazardSimulationModel):
    """冰岩雪崩 MOCK 模型。指标：mock_hazard_index / mock_impact_index /
    mock_avalanche_power_index，全部 0~1 归一化合成值。"""

    hazard_type = ICE_ROCK_SNOW_AVALANCHE
    MODEL_VERSION = "MOCK-v0.1"
    simulation_mode = "mock"
    is_real_prediction = False
    disclaimer = MOCK_DISCLAIMER

    required_inputs = ["release_volume_m3", "vertical_drop_m"]
    optional_inputs = {"slope_angle_deg": 35.0}
    param_constraints = {
        "release_volume_m3": (10.0, 1000000.0, "雪/冰/岩释放体量（立方米），演示用原始值"),
        "vertical_drop_m": (50.0, 3000.0, "垂直落差（米）"),
        "slope_angle_deg": (20.0, 60.0, "坡度角（度，可选，默认 35）"),
    }
    required_metrics = ["mock_hazard_index", "mock_impact_index", "mock_avalanche_power_index"]
    n_steps = 6
    real_model_note = "α 系数能量线法 / Voellmy-Salm 模型（接入前需核实数据需求与参数适用范围）"
    limitations_extra = ["雪崩玩具规则未考虑雪层结构、地形制约与路径摩擦等真实因素"]

    def run_simulation(self, scenario: dict, context: dict) -> dict:
        # ---- 测试钩子：前 n 次执行抛瞬态异常（正式契约的一部分）----
        hooks = scenario.get("test_hooks") or {}
        fail_n = int(hooks.get("fail_first_attempts", 0) or 0)
        if fail_n and int(context.get("attempt", 1)) <= fail_n:
            raise RuntimeError(f"模拟瞬态故障(MOCK test hook): 第 {context.get('attempt', 1)} 次执行被要求失败")

        t0 = time.perf_counter()
        sev = SEVERITY_MULTIPLIER.get(scenario.get("severity_level", "medium"), 1.0)

        # ---- 1. 输入归一化（规则见文件 docstring，对数尺度处理体量）----
        vol = clip01((math.log10(scenario["release_volume_m3"]) - 1.0) / 5.0)
        drop = clip01(scenario["vertical_drop_m"] / 3000.0)
        ang = clip01((scenario.get("slope_angle_deg", 35.0) - 20.0) / 40.0)

        # ---- 2. 目标危险度 ----
        target = clip01((0.20 + 0.40 * vol + 0.30 * drop + 0.10 * ang) * sev)

        # ---- 3. 时间步演化：先快后慢的幂次饱和 ----
        steps = []
        power = clip01((0.25 + 0.45 * vol + 0.30 * drop) * sev)
        n = self.n_steps
        for t in range(n):
            h = target * (1.0 - (1.0 - (t + 1) / n) ** 2)
            impact_t = clip01(0.55 * h + 0.25 * drop + 0.20 * vol)
            steps.append(
                {
                    "step": t,
                    "mock_hazard_index": round(h, 4),
                    "mock_avalanche_power_index": round(power, 4),
                    "mock_impact_index": round(impact_t, 4),
                }
            )

        # ---- 4. 终态指标（取最后一步，保证与 steps 一致）----
        last = steps[-1]
        final = {
            "mock_hazard_index": last["mock_hazard_index"],
            "mock_avalanche_power_index": round(power, 4),
            "mock_impact_index": last["mock_impact_index"],
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
