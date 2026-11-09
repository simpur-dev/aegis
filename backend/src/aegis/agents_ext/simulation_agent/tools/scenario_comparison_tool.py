"""
Simulation Agent 情景对比工具

【本文件角色】
  确定性跨情景指标对比（不调 LLM、不做任何二次推算）：
  - comparison_table：逐情景 × 逐指标对比表
  - ranking：按 mock_hazard_index 降序（并列时按 mock_impact_index）排序
  - metric_extremes：每个指标的 max/min 及其归属情景
  - scenario_differences：各情景相对基线情景的逐指标差异（scenario_difference）

【诚实声明】
  本工具只做算术汇总，输出数字全部来自 simulation_results 的原始指标，
  保留 4 位小数；LLM 叙述必须逐字引用这些数字，不得改写。

【对外契约】
  提供给：nodes/results_analysis_node.py（其结果写 comparison_result）。
"""


def compare_scenarios_tool(simulation_results: list[dict]) -> dict:
    """对比多个情景的仿真指标。
    过滤成功的情景
    参数:
        simulation_results: collect_results 产出的统一结果列表（只含成功情景）

    返回:
        {
          "comparison_table": [ {scenario_id, severity_level, <指标>: 值, ...} ],
          "ranking": [ {scenario_id, severity_level, mock_hazard_index, mock_impact_index} ]  # 降序
          "metric_extremes": { 指标: {"max": {...}, "min": {...}} },
          "scenario_differences": [ {metric, scenario_id, baseline_scenario_id,
                                     baseline_value, value, delta, relative_change} ],
          "baseline_scenario_id": str,   # 基线 = 列表中第一个成功情景
          "note": str,                   # 空结果时的说明，正常为 ""
        }
    """
    ok_results = [r for r in (simulation_results or []) if isinstance(r, dict) and r.get("indicators")]
    if not ok_results:
        return {
            "comparison_table": [],
            "ranking": [],
            "metric_extremes": {},
            "scenario_differences": [],
            "baseline_scenario_id": "",
            "note": "无成功情景结果，无法对比",
        }

    # 汇总全部出现过的指标键（保持首次出现顺序）
    metrics: list[str] = []
    for r in ok_results:
        for k in r["indicators"]:
            if k not in metrics:
                metrics.append(k)

    # ---------- 对比表 ----------
    comparison_table = [
        {
            "scenario_id": r.get("scenario_id", ""),
            "severity_level": r.get("severity_level", ""),
            **{m: r["indicators"].get(m) for m in metrics},
        }
        for r in ok_results
    ]

    # ---------- 排序（危险度降序，并列时按影响度）----------
    ranking = sorted(
        (
            {
                "scenario_id": r.get("scenario_id", ""),
                "severity_level": r.get("severity_level", ""),
                "mock_hazard_index": r["indicators"].get("mock_hazard_index", 0.0),
                "mock_impact_index": r["indicators"].get("mock_impact_index", 0.0),
            }
            for r in ok_results
        ),
        key=lambda x: (x["mock_hazard_index"], x["mock_impact_index"]),
        reverse=True,
    )

    # ---------- 极值 ----------
    metric_extremes = {}
    for m in metrics:
        values = [
            (r.get("scenario_id", ""), r["indicators"].get(m)) for r in ok_results if isinstance(r["indicators"].get(m), (int, float))
        ]
        if not values:
            continue
        vmax = max(values, key=lambda x: x[1])
        vmin = min(values, key=lambda x: x[1])
        metric_extremes[m] = {
            "max": {"scenario_id": vmax[0], "value": round(vmax[1], 4)},
            "min": {"scenario_id": vmin[0], "value": round(vmin[1], 4)},
        }

    # ---------- 与基线情景的逐指标差异 ----------
    baseline = ok_results[0]
    baseline_id = baseline.get("scenario_id", "")
    baseline_ind = baseline["indicators"]
    scenario_differences = []
    for r in ok_results[1:]:
        for m in metrics:
            bv = baseline_ind.get(m)
            v = r["indicators"].get(m)
            if not (isinstance(bv, (int, float)) and isinstance(v, (int, float))):
                continue  # 基线或本情景缺该指标（如 drop_metric 钩子）→ 不参与差异计算
            scenario_differences.append(
                {
                    "metric": m,
                    "scenario_id": r.get("scenario_id", ""),
                    "baseline_scenario_id": baseline_id,
                    "baseline_value": round(bv, 4),
                    "value": round(v, 4),
                    "delta": round(v - bv, 4),
                    "relative_change": round((v - bv) / bv, 4) if bv else None,
                }
            )

    return {
        "comparison_table": comparison_table,
        "ranking": ranking,
        "metric_extremes": metric_extremes,
        "scenario_differences": scenario_differences,
        "baseline_scenario_id": baseline_id,
        "note": "",
    }
