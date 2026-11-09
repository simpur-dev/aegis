"""
SimulationExecute 节点 —— 仿真执行

【本文件角色】
  取路由确定的模型 → 逐情景执行"二道门校验（validate_input）→ 带超时执行
  （execute_scenario_with_timeout）→ 统一结果组装（collect_results）"。
  记录逐步中间状态（steps）与终态指标；任何情景异常计入失败并消耗重试预算。

【对外契约】
  读取：hazard_type（已归一化）/ validated_scenarios / risk_result / data_result /
        retry_count / max_retries / timeout_seconds
  写入：simulation_results / execution_result / retry_count / next
  路由：route_after_execute 读 execution_result.ok 与 retry_count
        （ok → 分析；异常且预算未耗尽 → 本节点重试；超限 → 直送输出）。

【重试语义（修正 Risk Agent 恒不自增的缺陷模式）】
  执行失败（ok=False）时在本节点同一次 return 里自增 retry_count，
  route_after_execute 读自增后的 retry_count < max_retries 判断回环。
  语义与 Data Agent 的循环一致：整个执行环节最多执行 max_retries 次。
"""

import time

from aegis.agents_ext.simulation_agent.tools.hazard_model_router import get_model
from aegis.agents_ext.simulation_agent.tools.simulation_executor import (
    execute_scenario_with_timeout,
)


def _short_data_summary(state: dict) -> str:
    """给模型 context 的数据摘要（压缩版，Mock 模型仅透传记录用）。"""
    data_result = state.get("data_result", {}) or {}
    processed = data_result.get("processed_data", {}) or {}
    return (
        f"status={data_result.get('status', 'unknown')}, "
        f"source={processed.get('data_source') or processed.get('source') or 'unknown'}, "
        f"events={len(processed.get('events', []) or [])}"
    )


def simulation_execute_node(state: dict) -> dict:
    """仿真执行节点。"""
    hazard = state.get("hazard_type", "") or ""
    scenarios = state.get("validated_scenarios", []) or []
    retry_count = state.get("retry_count", 0)
    max_retries = state.get("max_retries", 3)
    timeout_seconds = float(state.get("timeout_seconds", 60.0))

    t0 = time.perf_counter()
    attempt = retry_count + 1  # 第几次尝试（重试时自增，供 test_hooks 判定瞬态故障）
    context = {
        "attempt": attempt,
        "risk_result": state.get("risk_result", {}) or {},
        "data_summary": _short_data_summary(state),
    }

    results = []
    failures = []

    # ---------- 取模型（防御：路由节点已保证支持）----------
    model = None
    try:
        model = get_model(hazard)
    except Exception as e:
        failures.append({"scenario_id": "-", "error": f"模型获取失败: {e}"})

    # ---------- 逐情景执行 ----------
    if model is not None:
        if not scenarios:
            failures.append({"scenario_id": "-", "error": "无已校验情景（validated_scenarios 为空）"})
        for sc in scenarios:
            sid = (sc or {}).get("scenario_id", "?")
            # 二道门校验（校验节点已做一遍；防止状态被并发修改 / 直传脏数据）
            vr = model.validate_input(sc or {})
            if not vr.get("is_valid"):
                failures.append(
                    {
                        "scenario_id": sid,
                        "error": f"二道门校验未通过: missing={vr.get('missing_fields')} invalid={vr.get('invalid_fields')}",
                    }
                )
                continue
            try:
                run_output = execute_scenario_with_timeout(model, vr["normalized"], context, timeout_seconds)
                results.append(model.collect_results(run_output, vr["normalized"]))
            except Exception as e:
                # 超时 / 模型异常 / test_hooks 瞬态故障 都到这里
                failures.append({"scenario_id": sid, "error": str(e)})

    # ---------- 执行结果与重试预算 ----------
    ok = bool(scenarios) and (model is not None) and (not failures)
    elapsed = round(time.perf_counter() - t0, 4)
    new_retry_count = retry_count + 1 if not ok else retry_count  # >>> ★ 与 retry 判定原子绑定

    execution_result = {
        "ok": ok,
        "attempt": attempt,
        "error": (failures[0]["error"] if failures else ""),
        "failures": failures,
        "scenario_ok_count": len(results),
        "scenario_fail_count": len(failures),
        "duration_seconds": elapsed,
        "timeout_seconds": timeout_seconds,
    }

    print(
        f"[SimulationExecute] attempt={attempt} ok={ok} "
        f"成功情景={len(results)} 失败={len(failures)} "
        f"retry_count={new_retry_count}/{max_retries}"
    )

    return {
        "simulation_results": results,
        "execution_result": execution_result,
        "retry_count": new_retry_count,
        "next": "results_analysis" if ok else ("simulation_execute" if new_retry_count < max_retries else "simulation_output"),
    }
