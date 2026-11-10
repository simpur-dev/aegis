"""
Simulation Agent 仿真执行器
把**单个情景的仿真计算 `model.run_simulation()`
**放到独立线程里跑，增加**超时保护**；捕获所有计算异常，统一封装成两类自定义异常，方便上层节点做重试计数与故障处理。

【本文件角色】
  用线程 + 超时包裹单次 model.run_simulation 调用，统一异常类型：
  - 模型自身抛出的异常（含 test_hooks 瞬态故障）→ SimulationExecutionError
  - 超时 → SimulationTimeoutError（SimulationExecutionError 的子类）
  超时/异常都由 simulation_execute_node 捕获后计入重试预算。

【诚实声明 / 重要红线】
  Python 线程无法被强制杀死：shutdown(wait=False) 只是放弃等待结果，
  若模型真的卡死，其线程会残留到进程结束。Mock 模型毫秒级完成不受影响；
  未来接入真实专业模型（可能长时间计算）时应升级为子进程级超时。

【对外契约】
  提供给：nodes/simulation_execute_node.py。
  不提供给：其他节点 / 其他 Agent。
"""

from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError


class SimulationExecutionError(RuntimeError):
    """单情景仿真执行失败（模型抛出异常的统一包装）。"""


class SimulationTimeoutError(SimulationExecutionError):
    """单情景仿真超时（计入可重试异常）。"""


def execute_scenario_with_timeout(model, scenario: dict, context: dict, timeout_seconds: float = 60.0) -> dict:
    """带超时保护地执行单情景仿真。

    参数:
        model: BaseHazardSimulationModel 实例
        scenario: validate_input 通过的 normalized 情景 dict
        context: {"attempt": int, "risk_result": dict, "data_summary": str}
        timeout_seconds: 单情景超时（秒），来自 State 的 timeout_seconds

    返回:
        model.run_simulation 的原始返回 dict

    异常:
        SimulationTimeoutError: 超时
        SimulationExecutionError: 模型内部异常（保留原异常链）
    """
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sim-exec")
    try:
        future = executor.submit(model.run_simulation, scenario, context)
        try:
            return future.result(timeout=timeout_seconds)
        except FuturesTimeoutError as exc:
            # 【并入主线时的改动】补 `from exc`：except 块里的 raise 不带 from 会丢掉异常链，
            # 上层拿到的是"凭空产生"的超时错误，看不到底下的真实原因。
            raise SimulationTimeoutError(f"单情景仿真超时（>{timeout_seconds}s）：scenario={scenario.get('scenario_id', '?')}") from exc
        except SimulationExecutionError:
            raise
        except Exception as e:  # 模型内部异常统一包装，保留异常链
            raise SimulationExecutionError(f"模型执行异常: {e}") from e
    finally:
        # 不等待残留线程（见文件头红线说明）；Mock 模型不会走到这里
        executor.shutdown(wait=False, cancel_futures=True)
