"""
Simulation Agent 冰湖溃决仿真模型（MOCK）

【本文件角色】lake_outburst（冰湖溃决）灾种的玩具级确定性仿真模型。

【诚实声明 / 重要红线】
  本模型是 MOCK（MODEL_VERSION="MOCK-v0.1"）：所有输出都是 mock_ 前缀的
  0~1 归一化合成指标，由下方声明的确定性玩具规则计算。
  它【不是】任何有科学依据的溃坝洪水公式，输出不代表真实溃口发育过程、
  峰值流量、洪水演进时间或淹没范围，禁止用于实际预警决策。
  未来接入专业模型（扩展位：溃口发育 + 峰值流量经验式 + 洪水演进）时，
  必须重新核实输入数据需求、输出单位与模型验证，不能只替换计算公式。

【计算规则（MOCK 玩具规则，非科学公式）】
  1. 输入归一化（clip01 裁剪到 [0,1]）：
     vol  = clip01((log10(lake_volume_m3) - 3) / 5)       # 湖库容维度（对数尺度，1e3→0，1e8→1）
     dam  = clip01(dam_height_m / 300)                    # 坝高维度
     dist = clip01(downstream_distance_km / 100)          # 下游距离维度（默认 5km → 0.05）
  2. 目标危险度（确定性加权合成，权重为演示约定）：
     target = clip01((0.30 + 0.45*vol + 0.25*dam) * SEV[severity_level])
  3. 时间步演化（两阶段：溃口快速爬升 2 步 + 洪水传播缓慢趋近）：
     t < 2:  h_t = min(target, target * 0.45 * (t + 1))
     t >= 2: h_t = h_{t-1} + 0.25 * (target - h_{t-1})
  4. 过程指标（静态过程描述量）：
     mock_flood_wave_index = clip01((0.30 + 0.40*vol + 0.30*dam) * SEV)
  5. 影响指标（下游越近影响越大）：
     mock_impact_index = clip01(0.55*h_final + 0.25*dam + 0.20*(1 - dist))

【适用限制】参数合法范围、权重与演化速率均为演示用约定值，
  未经过任何实测数据或文献依据校验；severity 乘子是 MOCK 演示约定。
"""

import math
import time

from aegis.agents.hazards.hazard_registry import GLACIAL_LAKE_OUTBURST
from aegis.agents.hazards.simulation_agent.models.base_model import (
    MOCK_DISCLAIMER,
    SEVERITY_MULTIPLIER,
    BaseHazardSimulationModel,
    clip01,
)


class GlacialLakeOutburstMockModel(BaseHazardSimulationModel):
    """冰湖溃决 MOCK 模型。指标：mock_hazard_index / mock_impact_index /
    mock_flood_wave_index，全部 0~1 归一化合成值。"""

    hazard_type = GLACIAL_LAKE_OUTBURST
    MODEL_VERSION = "MOCK-v0.1"
    simulation_mode = "mock"
    is_real_prediction = False
    disclaimer = MOCK_DISCLAIMER

    required_inputs = ["lake_volume_m3", "dam_height_m"]
    optional_inputs = {"downstream_distance_km": 5.0}
    param_constraints = {
        "lake_volume_m3": (1000.0, 100000000.0, "冰湖库容（立方米），演示用原始值"),
        "dam_height_m": (5.0, 300.0, "冰碛坝坝高（米）"),
        "downstream_distance_km": (0.1, 100.0, "下游关注点距离（公里，可选，默认 5）"),
    }
    required_metrics = ["mock_hazard_index", "mock_impact_index", "mock_flood_wave_index"]
    n_steps = 6
    real_model_note = "溃口发育 + 峰值流量经验式 + 洪水演进（接入前需核实数据需求与参数适用范围）"
    limitations_extra = ["冰湖溃决玩具规则未考虑坝体物质组成、涌浪触发与河谷地形等真实因素"]

    def run_simulation(self, scenario: dict, context: dict) -> dict:
        # ---- 测试钩子：前 n 次执行抛瞬态异常（正式契约的一部分）----
        hooks = scenario.get("test_hooks") or {}
        fail_n = int(hooks.get("fail_first_attempts", 0) or 0)
        if fail_n and int(context.get("attempt", 1)) <= fail_n:
            raise RuntimeError(f"模拟瞬态故障(MOCK test hook): 第 {context.get('attempt', 1)} 次执行被要求失败")

        t0 = time.perf_counter()
        sev = SEVERITY_MULTIPLIER.get(scenario.get("severity_level", "medium"), 1.0)

        # ---- 1. 输入归一化（规则见文件 docstring，对数尺度处理库容）----
        vol = clip01((math.log10(scenario["lake_volume_m3"]) - 3.0) / 5.0)
        dam = clip01(scenario["dam_height_m"] / 300.0)
        dist = clip01(scenario.get("downstream_distance_km", 5.0) / 100.0)

        # ---- 2. 目标危险度 ----
        target = clip01((0.30 + 0.45 * vol + 0.25 * dam) * sev)

        # ---- 3. 时间步演化：两阶段（溃口快速爬升 + 传播缓慢趋近）----
        steps = []
        wave = clip01((0.30 + 0.40 * vol + 0.30 * dam) * sev)
        prev = 0.0
        for t in range(self.n_steps):
            h = min(target, target * 0.45 * (t + 1)) if t < 2 else prev + 0.25 * (target - prev)
            prev = h
            impact_t = clip01(0.55 * h + 0.25 * dam + 0.20 * (1.0 - dist))
            steps.append(
                {
                    "step": t,
                    "mock_hazard_index": round(h, 4),
                    "mock_flood_wave_index": round(wave, 4),
                    "mock_impact_index": round(impact_t, 4),
                }
            )

        # ---- 4. 终态指标（取最后一步，保证与 steps 一致）----
        last = steps[-1]
        final = {
            "mock_hazard_index": last["mock_hazard_index"],
            "mock_flood_wave_index": round(wave, 4),
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
