"""
Simulation Agent 输出完整性检查工具

【本文件角色】
  确定性检查仿真结果与分析产物是否完整（不调 LLM），判定能否通过"分析重试"修复：
  - 指标层缺失（情景缺 required_metrics / 缺 steps 时间步）→ 【不可修复】
    （重跑分析补不回模型剔除的指标，只能带明确状态出图）
  - 分析层缺失（对比表 / 排序 / 关键发现 / 叙述为空）→ 【可修复】
    （重跑 results_analysis（LLM 或兜底）有机会补上）

1. **指标层缺失（仿真模型输出缺东西） → fixable = False，不可修复**
指标、时间步`steps`是**仿真模型跑出来的数据**。

>
> 就算反复重新执行`results_analysis`分析节点，也凭空造不出模型没有输出的指标。
> 这种情况不能重试分析，只能带着`uncertain`状态直接输出最终结果。
2. **分析层缺失（对比表、排序、关键发现、总结文本为空） → fixable = True，可修复**
对比表、ranking、key_findings、narrative_summary 是**分析节点组装 / LLM 生成的文本**。

>
> 模型指标数据本身是完整的，只是分析产出物没生成好。**重跑一次 results_analysis 节点就有机会补全**，所以路由可以走重试分支。
3. 没有任何缺失项：`is_complete=True`，`fixable=False`（不需要修复）


【对外契约】
  提供给：nodes/results_analysis_node.py（其结果写 comparison_result.completeness，
          驱动 route_after_analysis 的修复重试判定）。
"""


def check_results_completeness_tool(simulation_results: list[dict], comparison_result: dict, required_metrics: list[str]) -> dict:
    """检查仿真结果与分析产物的完整性。

    参数:
        simulation_results: 统一结果列表（可能为空）
        comparison_result: 结果分析节点已组装的分析 dict（可能不完整）
        required_metrics: 该灾种模型声明的必需指标键

    返回:
        {"is_complete": bool, "missing_items": [str], "fixable": bool}
        fixable 语义：仅当"所有缺失项都是分析层字段"（重跑分析可能修复）为 True；
        指标层缺失 → False；无缺失 → False（无需修复）。
    """
    missing: list[str] = []
    metric_missing = False

    # ---------- 1) 仿真结果层 ----------
    if not simulation_results:
        missing.append("simulation_results: 无任何成功情景结果")
        metric_missing = True
    else:
        for r in simulation_results:
            sid = r.get("scenario_id", "?")
            indicators = r.get("indicators", {}) or {}
            for m in required_metrics or []:
                if m not in indicators:
                    missing.append(f"indicators[{sid}]: 缺少必需指标 {m}")
                    metric_missing = True
            if not (r.get("steps", []) or []):
                missing.append(f"steps[{sid}]: 缺少时间步中间状态")
                metric_missing = True

    # ---------- 2) 分析层（可修复）----------
    comp = comparison_result or {}
    if not comp.get("comparison_table"):
        missing.append("analysis:comparison_table 为空")
    if not comp.get("ranking"):
        missing.append("analysis:ranking 为空")
    if not comp.get("key_findings"):
        missing.append("analysis:key_findings 为空")
    if not comp.get("narrative_summary"):
        missing.append("analysis:narrative_summary 为空")

    analysis_missing = any(item.startswith("analysis:") for item in missing)
    fixable = bool(missing) and analysis_missing and not metric_missing

    return {
        "is_complete": not missing,
        "missing_items": missing,
        "fixable": fixable,
    }
