"""
DataPlan 节点（面向西藏五类山地地质灾害）

职责：
  - 校验显式传入的 hazard_type（缺失/无法识别 → fail-fast 到输出，绝不猜灾种）
  - 从灾种数据注册表取数据需求与查询参数菜单
  - 使用 LLM 理解任务，但参数只能从菜单中选择（数值被 pydantic 模型约束在菜单范围内）
  - LLM 不可用时按菜单默认值兜底

设计思想（借鉴 AWS multi-agent）：
  LLM 负责"理解任务"，数据需求与字段清单来自注册表，LLM 不得发明参数。
"""

import json
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from aegis.agents.hazards.data_agent.state import DataAgentState
from aegis.agents.hazards.data_agent.tools.hazard_data_registry import (
    build_query_param_model,
    get_hazard_data_requirements,
)
from aegis.agents.hazards.hazard_registry import UnsupportedHazardError, require_hazard

# 项目根目录入 sys.path（hazard_registry 位于根目录）
from aegis.agents.hazards.llm import get_llm
from aegis.agents.hazards.prompts.data_prompt import PLAN_SYSTEM_PROMPT, build_plan_user_prompt

# ============================================================
# LLM 输出的任务理解（灾种无关的通用骨架；具体参数由动态菜单模型约束）
# ============================================================


class TaskPlan(BaseModel):
    """LLM 从任务描述中提取的通用信息（参数值仅作为兜底建议，实际以菜单模型校验为准）"""

    region: str = Field(default="西藏", description="查询区域，如 西藏/林芝/波密")
    time_range: str = Field(default="24h", description="时间范围，只允许 24h / 7d / 30d")
    processing_plan: str = Field(default="", description="简要处理计划描述")


# ============================================================
# 节点函数
# ============================================================


def data_plan_node(state: DataAgentState) -> dict[str, Any]:
    """数据计划节点：校验灾种 → 注册表取需求 → LLM 解析菜单内参数。"""

    task = state.get("task", "") or ""
    hazard_raw = state.get("hazard_type", "") or ""
    retry_count = state.get("retry_count", 0)

    # ========== 步骤1：灾种校验（显式必填，缺失/无法识别 → fail-fast）==========
    try:
        hazard_key = require_hazard(hazard_raw)
    except UnsupportedHazardError as e:
        print(f"[DataPlan] 灾种无效，fail-fast: {e}")
        return {
            "hazard_type": hazard_raw,  # 识别失败保持原值，由输出节点透传原因
            "data_requirement": {},  # 置空 → route_after_plan 直送 data_output
            "processing_plan": "",
            "error_message": str(e),
            "status": "failed",
            "next": "data_output",
        }

    # ========== 步骤2：注册表取数据需求与参数菜单 ==========
    req = get_hazard_data_requirements(hazard_key)
    menu = req["query_param_menu"]
    print(f"[DataPlan] 灾种={hazard_key}（{req['hazard_type_cn']}） 必需字段={req['required_fields']}")

    # ========== 步骤3：LLM 解析菜单内参数（失败按菜单默认值兜底）==========
    params = {}
    processing_plan = ""
    used_llm = False
    try:
        QueryParamModel = build_query_param_model(hazard_key)  # 菜单驱动的动态模型
        structured_llm = get_llm().with_structured_output(QueryParamModel)
        result = structured_llm.invoke(
            [
                SystemMessage(content=PLAN_SYSTEM_PROMPT),
                HumanMessage(
                    content=build_plan_user_prompt(
                        task=task,
                        hazard_type=hazard_key,
                        hazard_type_cn=req["hazard_type_cn"],
                        menu_str=json.dumps(menu, ensure_ascii=False, indent=1),
                        retry_count=retry_count,
                        error_message=state.get("error_message", ""),
                        missing_data=state.get("missing_data", []),
                    )
                ),
            ]
        )
        params = result.model_dump()
        used_llm = True
        processing_plan = (
            f"目标: 获取{params.get('region', '西藏')}地区、最近{params.get('time_range', '24h')}、"
            f"{req['hazard_type_cn']}观测数据（Mock）。"
            "步骤: 获取 → 清洗 → 标准化 → 质量验证 → 输出。"
        )
    except Exception as e:
        # 兜底：LLM 不可用时按菜单默认值组织参数，流程不崩
        print(f"[DataPlan] LLM 解析失败，按菜单默认值兜底: {e}")
        params = {p["name"]: p["default"] for p in menu}
        processing_plan = f"默认处理计划：获取{req['hazard_type_cn']}观测数据（Mock）→ 清洗 → 标准化 → 验证 → 输出"

    # 重试轮次的计划文案
    if retry_count > 0:
        processing_plan = (
            f"第 {retry_count + 1} 次重试: 扩大查询范围，重新获取{params.get('region', '西藏')}地区{req['hazard_type_cn']}观测数据。"
        )

    # ========== 步骤4：组装数据需求（下游 clean/validate/输出 全部依赖此结构）==========
    data_requirement = {
        "hazard_type": hazard_key,
        "hazard_type_cn": req["hazard_type_cn"],
        "data_type": f"{hazard_key}_observations",
        "required_fields": req["required_fields"],  # 公共必需 + 灾种必需观测
        "observation_fields": list(req["observation_fields"].keys()),
        "query_params": params,  # 仅菜单内参数
        "quality_threshold": {"min_completeness": 0.8, "max_anomaly_ratio": 0.1},
        "is_mock": True,  # 当前数据源为 Mock 构造器
    }

    print(f"[DataPlan] 解析结果:\n{json.dumps(data_requirement, ensure_ascii=False, indent=2)}\n[DataPlan] used_llm={used_llm}")

    return {
        "hazard_type": hazard_key,  # >>> 归一化标准键写回
        "data_requirement": data_requirement,
        "processing_plan": processing_plan,
        "error_message": "",  # 成功走到这里则清空历史错误
        "status": "processing",
        "next": "data_retrieve",
    }
