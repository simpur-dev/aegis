"""
Simulation Agent 灾种仿真模型 —— 统一接口基类

【本文件角色】
  定义五类山地灾害仿真模型的统一接口 BaseHazardSimulationModel（ABC），
  以及情景配置基模型 ScenarioConfig（pydantic，仅模型接口层使用；
  State 内一律使用裸 dict，进模型前先 validate_input 归一化）。

【诚实声明 / 重要红线】
  1. 本层所有实现均为 MOCK 玩具模型：MODEL_VERSION 以 "MOCK-" 开头，
     is_real_prediction 恒为 False，disclaimer 强制存在、不得移除。
  2. 输出指标一律为 mock_ 前缀的 0~1 归一化合成值，
     【禁止】输出真实物理量（峰值流量 m3/s、滑距 m、受影响人数、影响面积等）——
     在没有专业模型与实测数据校验的情况下，这类数字属于虚构。
  3. 输入相同 → 输出必须完全相同（纯确定性、无随机数），
     保证情景对比与单元测试可复现。
  4. 不支持的输入组合必须显式报错（invalid_fields），不得静默编造输出。

【对外契约】
  提供给：models/ 下五个灾种模型（继承本基类）；
          tools/hazard_model_router.py（模型注册表）；
          nodes/simulation_execute_node.py（三步主流程调用）。
  不提供给：Data Agent / Risk Agent（互不依赖）。

【三步主流程】（对齐任务要求的统一接口）
  validate_input(scenario) -> 校验单情景输入（基类提供通用实现，由
                              required_inputs / param_constraints 驱动）
  run_simulation(scenario, context) -> 时间步进仿真（抽象方法，各灾种独立实现）
  collect_results(run_output, scenario) -> 组装统一结果 Schema（基类提供通用实现）
"""

from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel, Field

# ============================================================
# 公共约定（MOCK 演示约定，非科学参数）
# ============================================================

# 严重度乘子：情景 severity_level 对指标的统一放大系数（演示约定）
SEVERITY_MULTIPLIER: dict[str, float] = {
    "low": 0.6,
    "medium": 1.0,
    "high": 1.5,
    "extreme": 2.2,
}

VALID_SEVERITY_LEVELS = tuple(SEVERITY_MULTIPLIER.keys())

# 全局统一免责声明（每个模型的 disclaimer 默认引用它）
MOCK_DISCLAIMER = "MOCK 玩具模型输出，仅用于流程演示与测试，不是真实地质灾害预测，禁止用于实际预警决策"


def clip01(x: Any) -> float:
    """裁剪到 [0, 1] 区间。

    确定性工具函数，五个模型共用（属于公共基础设施，不属于任何灾种公式本体）。
    """
    return max(0.0, min(1.0, float(x)))


# ============================================================
# 情景配置基模型（pydantic，仅模型接口层使用）
# ============================================================


class ScenarioConfig(BaseModel):
    """情景配置基模型。

    约定：
      - State / 节点之间传递的情景是裸 dict（扁平结构：公共字段 + 灾种参数平铺）；
      - 进入模型后先经本模型校验归一化（extra="allow" 允许灾种专属参数平铺传入）；
      - test_hooks 是正式契约的一部分（不是测试后门）：
          {"fail_first_attempts": n}  前 n 次执行抛瞬态异常（测重试路径）；
          {"drop_metric": "指标键"}   collect_results 时剔除该指标（测输出完整性路径）。
    """

    model_config = {"extra": "allow"}  # 允许灾种专属参数平铺传入

    scenario_id: str = Field(description="情景唯一标识，如 SC-01")
    hazard_type: str = Field(description="规范灾种键（五个标准键之一）")
    severity_level: str = Field(default="medium", description="low/medium/high/extreme")
    trigger: str = Field(default="", description="触发因素描述，如 强降雨")
    trigger_source_tag: str = Field(
        default="assumption",
        description="触发依据来源标签: observed(真实观测)/analysis(已有分析)/assumption(情景假设)",
    )
    duration_hours: float = Field(default=6.0, gt=0, description="情景时间窗（小时）")
    notes: str = Field(default="", description="备注 / 设计依据")
    test_hooks: dict = Field(
        default_factory=dict,
        description="测试钩子（正式契约的一部分），见类 docstring",
    )


# ============================================================
# 统一模型接口（ABC）
# ============================================================


class BaseHazardSimulationModel(ABC):
    """五类山地灾害仿真模型的统一接口。

    子类必须：
      1. 覆写公共类属性：hazard_type / MODEL_VERSION（MOCK- 前缀）/ disclaimer /
         required_inputs / optional_inputs / param_constraints / required_metrics /
         real_model_note；
      2. 独立实现 run_simulation（各灾种演化规则与指标合成规则【不得共用】，
         规则必须写在该模型文件 docstring 的【计算规则（MOCK）】小节）；
      3. 无状态：get_model 每次返回新实例，多情景依次串行调用；
      4. 输入相同 → 输出完全相同（纯确定性，无随机数）。
    """

    # ---- 公共类属性（每个子类必须覆写）----
    hazard_type: str = ""  # 规范灾种键（与 tools/hazard_model_router.py 常量一致）
    MODEL_VERSION: str = "MOCK-v0.1"  # 模型版本标识
    simulation_mode: str = "mock"  # 仿真模式标识
    is_real_prediction: bool = False  # ★ 恒为 False：不是真实预测
    disclaimer: str = MOCK_DISCLAIMER  # 强制免责声明，随每份结果透传
    required_inputs: list[str] = []  # 情景必需参数键（缺任一 → validate_input 判 False）
    optional_inputs: dict[str, Any] = {}  # 可选参数 → 默认值（default_scenario_tool 据此生成兜底情景）
    param_constraints: dict[str, tuple] = {}  # 参数 → (最小值, 最大值, 中文说明)
    required_metrics: list[str] = []  # collect_results 必含指标键（completeness_check_tool 判据）
    n_steps: int = 6  # 时间步数（中间状态记录条数）
    real_model_note: str = ""  # 真实专业模型扩展位说明
    limitations_extra: list[str] = []  # 各模型的额外局限说明

    # --------------------------------------------------------
    # 第一步：validate_input（基类通用实现，由类属性驱动）
    # --------------------------------------------------------
    def validate_input(self, scenario: dict) -> dict:
        """校验单情景输入合法性。

        参数:
            scenario: 裸 dict（State 内的情景配置，扁平结构）

        返回:
            {
              "is_valid": bool,
              "missing_fields": [str],                                # required_inputs 中缺失的键
              "invalid_fields": [{"field": str, "value": Any, "reason": str}],
              "normalized": Optional[dict],                           # 校验通过时 ScenarioConfig 归一化结果
            }
        """
        missing: list[str] = []
        invalid: list[dict] = []
        scenario = scenario if isinstance(scenario, dict) else {}

        # 1) 必需参数齐备性
        for key in self.required_inputs:
            if scenario.get(key) is None:
                missing.append(key)

        # 2) severity_level 枚举
        sev = scenario.get("severity_level", "medium")
        if sev not in VALID_SEVERITY_LEVELS:
            invalid.append(
                {
                    "field": "severity_level",
                    "value": sev,
                    "reason": f"必须为 {VALID_SEVERITY_LEVELS} 之一",
                }
            )

        # 3) 参数数值类型与范围（param_constraints 驱动）
        for key, (lo, hi, desc) in self.param_constraints.items():
            if scenario.get(key) is None:
                continue  # 缺失已归入 missing_fields，不重复报
            v = scenario[key]
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                invalid.append({"field": key, "value": v, "reason": "必须为数值"})
                continue
            if not (lo <= v <= hi):
                invalid.append(
                    {
                        "field": key,
                        "value": v,
                        "reason": f"超出合法范围 [{lo}, {hi}]（{desc}）",
                    }
                )

        # 4) 情景结构归一化（pydantic）
        is_valid = (not missing) and (not invalid)
        normalized: dict | None = None
        if is_valid:
            try:
                normalized = ScenarioConfig(**scenario).model_dump()
            except Exception as e:
                is_valid = False
                invalid.append(
                    {
                        "field": "scenario",
                        "value": None,
                        "reason": f"情景结构校验失败: {e}",
                    }
                )
                normalized = None

        return {
            "is_valid": is_valid,
            "missing_fields": missing,
            "invalid_fields": invalid,
            "normalized": normalized,
        }

    # --------------------------------------------------------
    # 第二步：run_simulation（抽象方法 —— 各灾种独立实现计算逻辑）
    # --------------------------------------------------------
    @abstractmethod
    def run_simulation(self, scenario: dict, context: dict) -> dict:
        """运行单情景时间步进仿真（Mock：确定性玩具规则）。

        参数:
            scenario: validate_input 通过的 normalized dict
            context: {
                "attempt": int,          # 该情景第几次执行（重试时自增，供 test_hooks 判定）
                "risk_result": dict,     # Risk Agent 结果（仿真上下文）
                "data_summary": str,     # 数据摘要（仿真上下文）
            }

        返回:
            {
              "ok": True,
              "scenario_id": str,
              "steps": [ 逐步指标快照（n_steps 条，记录中间状态） ],
              "final_indicators": { mock_ 前缀归一化指标 },
              "execution_info": {"steps_run": int, "duration_sec": float, "timed_out": False},
            }

        约定:
            - test_hooks.fail_first_attempts：attempt <= n 时抛 RuntimeError（瞬态故障模拟）；
            - 超时由 tools/simulation_executor.py 统一包裹，此处只管计算；
            - 任何不支持的输入组合抛异常，不得静默编造输出。
        """
        raise NotImplementedError

    # --------------------------------------------------------
    # 第三步：collect_results（基类通用实现 —— 统一 Schema 组装）
    # --------------------------------------------------------
    def collect_results(self, run_output: dict, scenario: dict) -> dict:
        """把 run_simulation 的原始输出组装为统一结果 Schema。

        test_hooks.drop_metric 在此剔除指定指标（用于测试"输出不完整"分支）。
        """
        indicators = dict(run_output.get("final_indicators", {}))
        dropped = None
        hooks = scenario.get("test_hooks") or {}
        drop_key = hooks.get("drop_metric")
        if drop_key and drop_key in indicators:
            indicators.pop(drop_key)
            dropped = drop_key

        limitations = [
            self.disclaimer,
            "所有指标为 mock_ 前缀的 0~1 归一化合成值，非真实物理量，无实测数据校验",
        ]
        if self.real_model_note:
            limitations.append(f"真实模型扩展位：{self.real_model_note}")
        limitations.extend(self.limitations_extra)

        return {
            "scenario_id": scenario.get("scenario_id", ""),
            "severity_level": scenario.get("severity_level", "medium"),
            "trigger": scenario.get("trigger", ""),
            "trigger_source_tag": scenario.get("trigger_source_tag", "assumption"),
            "hazard_type": self.hazard_type,
            "model_version": self.MODEL_VERSION,
            "simulation_mode": self.simulation_mode,
            "is_real_prediction": self.is_real_prediction,
            "disclaimer": self.disclaimer,
            "input_params": {k: scenario.get(k) for k in list(self.required_inputs) + list(self.optional_inputs) if k in scenario},
            "steps": run_output.get("steps", []),
            "indicators": indicators,
            "assumptions": list(scenario.get("assumptions", []) or []),
            "limitations": limitations,
            "execution_info": run_output.get("execution_info", {}),
            "dropped_metric_for_test": dropped,  # 测试断言用；正常流程恒为 None
        }
