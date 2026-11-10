"""
Simulation Agent 泥石流仿真模型（MOCK）

【本文件角色】debris_flow（泥石流）灾种的玩具级确定性仿真模型。

【诚实声明 / 重要红线】
  本模型是 MOCK（MODEL_VERSION="MOCK-v0.1"）：所有输出都是 mock_ 前缀的
  0~1 归一化合成指标，由下方声明的确定性玩具规则计算。
  它【不是】任何有科学依据的泥石流公式，输出不代表真实峰值流量、
  流速、堆积厚度、到达时间或淹没范围，禁止用于实际预警决策。
  未来接入专业模型（扩展位：雨强-历时(I-D)阈值触发判别 + 运动/堆积数值模拟）时，
  必须重新核实输入数据需求、输出单位与模型验证，不能只替换计算公式。

【计算规则（MOCK 玩具规则，非科学公式）】
  1. 输入归一化（clip01 裁剪到 [0,1]）：
     r   = clip01(rainfall_mm_h / 150)                    # 雨强维度
     a   = clip01((catchment_area_km2 / 500) ** 0.5)      # 流域面积维度（开方压缩）
     g   = clip01((channel_gradient - 0.01) / 0.99)       # 沟道纵比降维度
     sed = clip01(sediment_supply_index)                  # 物源补给维度
  2. 目标危险度（确定性加权合成，权重为演示约定）：
     target = clip01((0.30 + 0.35*r + 0.15*a + 0.15*g + 0.05*sed) * SEV[severity_level])
  3. 时间步演化（S 形 logistic 爬升：前慢、中快、后饱和）：
     w_t = 1 / (1 + exp(-2 * (t - n_steps/2 + 1)))；h_t = target * w_t
  4. 过程指标（静态过程描述量）：
     mock_flow_intensity_index = clip01((0.25 + 0.40*r + 0.20*g + 0.15*sed) * SEV)
  5. 影响指标：
     mock_impact_index = clip01(0.50*h_final + 0.30*a + 0.20*g)

【适用限制】参数合法范围、权重与演化速率均为演示用约定值，
  未经过任何实测数据或文献依据校验；severity 乘子是 MOCK 演示约定。
"""

import math
import time

from aegis.agents.hazards.simulation_agent.models.base_model import (
    MOCK_DISCLAIMER,
    SEVERITY_MULTIPLIER,
    BaseHazardSimulationModel,
    clip01,
)


class DebrisFlowMockModel(BaseHazardSimulationModel):
    """泥石流 MOCK 模型。指标：mock_hazard_index / mock_impact_index /
    mock_flow_intensity_index，全部 0~1 归一化合成值。"""

    hazard_type = "debris_flow"
    MODEL_VERSION = "MOCK-v0.1"
    simulation_mode = "mock"
    is_real_prediction = False
    disclaimer = MOCK_DISCLAIMER

    required_inputs = ["rainfall_mm_h", "catchment_area_km2", "channel_gradient"]
    optional_inputs = {"sediment_supply_index": 0.5}
    param_constraints = {
        "rainfall_mm_h": (0.0, 300.0, "小时降雨量（毫米），演示用原始值"),
        "catchment_area_km2": (0.01, 500.0, "流域面积（平方公里）"),
        "channel_gradient": (0.01, 1.0, "沟道纵比降（无量纲）"),
        "sediment_supply_index": (0.0, 1.0, "物源补给指数（可选，默认 0.5）"),
    }
    required_metrics = ["mock_hazard_index", "mock_impact_index", "mock_flow_intensity_index"]
    n_steps = 6
    real_model_note = "雨强-历时(I-D)阈值触发判别 + 运动/堆积数值模拟（接入前需核实数据需求与参数适用范围）"
    limitations_extra = ["泥石流玩具规则未考虑沟道堵塞、溃决型起动与堆积扇形态等真实因素"]

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
        a = clip01((scenario["catchment_area_km2"] / 500.0) ** 0.5)
        g = clip01((scenario["channel_gradient"] - 0.01) / 0.99)
        sed = clip01(scenario.get("sediment_supply_index", 0.5))

        # ---- 2. 目标危险度 ----
        target = clip01((0.30 + 0.35 * r + 0.15 * a + 0.15 * g + 0.05 * sed) * sev)

        # ---- 3. 时间步演化：S 形 logistic 爬升 ----
        steps = []
        intensity = clip01((0.25 + 0.40 * r + 0.20 * g + 0.15 * sed) * sev)
        n = self.n_steps
        for t in range(n):
            w = 1.0 / (1.0 + math.exp(-2.0 * (t - n / 2.0 + 1.0)))
            h = target * w
            impact_t = clip01(0.50 * h + 0.30 * a + 0.20 * g)
            steps.append(
                {
                    "step": t,
                    "mock_hazard_index": round(h, 4),
                    "mock_flow_intensity_index": round(intensity, 4),
                    "mock_impact_index": round(impact_t, 4),
                }
            )

        # ---- 4. 终态指标（取最后一步，保证与 steps 一致）----
        last = steps[-1]
        final = {
            "mock_hazard_index": last["mock_hazard_index"],
            "mock_flow_intensity_index": round(intensity, 4),
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
