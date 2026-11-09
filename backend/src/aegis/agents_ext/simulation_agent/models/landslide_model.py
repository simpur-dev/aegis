"""
Simulation Agent 滑坡仿真模型（MOCK）

【本文件角色】landslide（滑坡）灾种的玩具级确定性仿真模型。

【诚实声明 / 重要红线】
  本模型是 MOCK（MODEL_VERSION="MOCK-v0.1"）：所有输出都是 mock_ 前缀的
  0~1 归一化合成指标，由下方声明的确定性玩具规则计算。
  它【不是】任何有科学依据的滑坡公式，输出不代表真实稳定系数、
  滑距、方量或影响范围，禁止用于实际预警决策。
  未来接入专业模型（扩展位：极限平衡法 / Newmark 位移法 / 滑距经验统计）时，
  必须重新核实输入数据需求、输出单位与模型验证，不能只替换计算公式。

【计算规则（MOCK 玩具规则，非科学公式）】
  1. 输入归一化（clip01 裁剪到 [0,1]）：
     r = clip01(rainfall_mm_h / 150)                 # 雨强维度
     s = clip01((slope_angle_deg - 5) / 60)          # 坡度维度（5°→0，65°→1）
     p = clip01((soil_porosity - 0.1) / 0.6)         # 孔隙率维度
  2. 目标危险度（确定性加权合成，权重为演示约定）：
     target = clip01((0.25 + 0.35*r + 0.30*s + 0.10*p) * SEV[severity_level])
  3. 时间步演化（指数趋近：每步走掉当前差距的 35%，从 0.10*target 起步）：
     h_0 = 0.10 * target；h_{t+1} = h_t + 0.35 * (target - h_t)
  4. 过程指标（静态过程描述量）：
     mock_slope_instability_index = clip01((0.20 + 0.50*s + 0.30*r) * SEV)
  5. 影响指标：
     mock_impact_index = clip01(0.60*h_final + 0.40*s)

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


class LandslideMockModel(BaseHazardSimulationModel):
    """滑坡 MOCK 模型。指标：mock_hazard_index / mock_impact_index /
    mock_slope_instability_index，全部 0~1 归一化合成值。"""

    hazard_type = "landslide"
    MODEL_VERSION = "MOCK-v0.1"
    simulation_mode = "mock"
    is_real_prediction = False
    disclaimer = MOCK_DISCLAIMER

    required_inputs = ["rainfall_mm_h", "slope_angle_deg"]
    optional_inputs = {"soil_porosity": 0.4}
    param_constraints = {
        "rainfall_mm_h": (0.0, 300.0, "小时降雨量（毫米），演示用原始值"),
        "slope_angle_deg": (5.0, 80.0, "坡度角（度）"),
        "soil_porosity": (0.1, 0.7, "土壤孔隙率（可选，默认 0.4）"),
    }
    required_metrics = ["mock_hazard_index", "mock_impact_index", "mock_slope_instability_index"]
    n_steps = 6
    real_model_note = "极限平衡法 / Newmark 位移法 / 滑距经验统计（接入前需核实数据需求与参数适用范围）"
    limitations_extra = ["滑坡玩具规则未考虑岩土体结构、地下水与人类工程活动等真实因素"]

    def run_simulation(self, scenario: dict, context: dict) -> dict:
        # ---- 测试钩子：前 n 次执行抛瞬态异常（正式契约的一部分）----
        hooks = scenario.get("test_hooks") or {}
        fail_n = int(hooks.get("fail_first_attempts", 0) or 0)
        if fail_n and int(context.get("attempt", 1)) <= fail_n:
            raise RuntimeError(f"模拟瞬态故障(MOCK test hook): 第 {context.get('attempt', 1)} 次执行被要求失败")

        t0 = time.perf_counter()
        sev = SEVERITY_MULTIPLIER.get(scenario.get("severity_level", "medium"), 1.0)

        # ---- 1. 输入归一化（规则见文件 docstring）----
        r = clip01(scenario["rainfall_mm_h"] / 150.0)
        s = clip01((scenario["slope_angle_deg"] - 5.0) / 60.0)
        p = clip01((scenario.get("soil_porosity", 0.4) - 0.1) / 0.6)

        # ---- 2. 目标危险度 ----
        target = clip01((0.25 + 0.35 * r + 0.30 * s + 0.10 * p) * sev)

        # ---- 3. 时间步演化：指数趋近 ----
        steps = []
        h = 0.10 * target
        instability = clip01((0.20 + 0.50 * s + 0.30 * r) * sev)
        for t in range(self.n_steps):
            h = h + 0.35 * (target - h)
            impact_t = clip01(0.60 * h + 0.40 * s)
            steps.append(
                {
                    "step": t,
                    "mock_hazard_index": round(h, 4),
                    "mock_slope_instability_index": round(instability, 4),
                    "mock_impact_index": round(impact_t, 4),
                }
            )

        # ---- 4. 终态指标 ----
        final = {
            "mock_hazard_index": round(h, 4),
            "mock_slope_instability_index": round(instability, 4),
            "mock_impact_index": round(clip01(0.60 * h + 0.40 * s), 4),
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
