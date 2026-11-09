"""
Data Agent 的共享状态定义（面向西藏五类山地地质灾害）。

使用 TypedDict 保证类型安全，所有 Node 通过读写此 State 进行通信。
灾种标准键与别名统一定义在根目录 hazard_registry.py（本项目不研究地震）。
"""

from typing import TypedDict


class DataAgentState(TypedDict):
    """Data Agent 的全局状态，贯穿整个处理流程"""

    # === 输入 ===
    task: str  # 上游（Manager Agent）传入的任务描述
    hazard_type: str  # ★ 必填 ★ 灾种（标准键或明确中文名，如 "debris_flow"/"泥石流"）；
    # data_plan_node 归一化后写回标准键（无效灾种会 fail-fast 到输出）

    # === 计划阶段 ===
    data_requirement: dict  # 数据需求（按灾种注册表组装：hazard_type/required_fields/
    # query_params（仅菜单内参数）/quality_threshold/is_mock 等）
    processing_plan: str  # 数据处理计划（自然语言描述）

    # === 数据阶段 ===
    raw_data: dict  # 灾种数据源工具返回的原始观测数据（当前为五灾种 Mock 构造器）
    processed_data: dict  # 清洗 + 标准化后的数据（events 通用结构 + 灾种观测字段）

    # === 质量检查 ===
    data_quality: dict  # 数据质量检查结果（完整度/异常率，物理合理范围来自灾种注册表）
    missing_data: list  # 缺失的数据项列表
    data_sufficiency: dict  # 数据充分性评估 {sufficient: bool, reason: str}：
    # 当前数据是否足以支撑后续风险评估

    # === 控制流 ===
    retry_count: int  # 当前重试次数（防止无限循环）
    next: str  # 路由标识，供 Conditional Edge 使用
    error_message: str  # 错误信息（如果有）
    status: str  # 最终状态: "success" / "failed" / "partial" / "processing" / "pending"
