"""
Data Agent 运行入口（面向西藏五类山地地质灾害）

演示如何调用 Data Agent 获取一个灾种的观测数据（当前为五灾种 Mock 数据源）。

【灾种显式必填】
  hazard_type 必须显式传入（标准键或明确中文名，如 "debris_flow"/"泥石流"），
  缺失或无法识别 → status=failed 并给出明确原因，绝不让 LLM 猜测灾种。
  灾种标准键与别名统一定义在根目录 hazard_registry.py。

【运行方式】
  cd C:\\Users\\lixin\\Desktop\\地政预警项目
  .venv\\Scripts\\python.exe -m agents.data_agent.main
"""

import json
import sys

from aegis.agents_ext.data_agent.graph import build_data_agent_graph
from aegis.agents_ext.data_agent.state import DataAgentState


def run_data_agent(task: str, hazard_type: str) -> dict:
    """运行 Data Agent，获取指定灾种的观测数据。

    参数:
        task: 任务描述（由 Manager Agent 传入）
        hazard_type: ★ 必填 ★ 灾种（标准键或明确中文名）

    返回:
        最终状态字典（全量 State；下游实际消费 processed_data / data_quality /
        missing_data / data_sufficiency / hazard_type / status / error_message）
    """

    # 1. 构建图
    agent_graph = build_data_agent_graph()

    # 2. 初始化 State
    initial_state: DataAgentState = {
        "task": task,
        "hazard_type": hazard_type,
        "data_requirement": {},
        "processing_plan": "",
        "raw_data": {},
        "processed_data": {},
        "data_quality": {},
        "missing_data": [],
        "data_sufficiency": {},
        "retry_count": 0,
        "next": "",
        "error_message": "",
        "status": "pending",
    }

    # 3. 执行图
    print("=" * 60)
    print(f"[Data Agent] 开始执行任务: {task}")
    print(f"[Data Agent] 灾种: {hazard_type}")
    print("=" * 60)

    final_state = agent_graph.invoke(initial_state)

    # 4. 输出结果
    print("\n" + "=" * 60)
    print("[Data Agent] 执行完成")
    print(f"  状态: {final_state.get('status')}")
    print(f"  灾种: {final_state.get('hazard_type')}")
    print(f"  数据质量: {final_state.get('data_quality', {}).get('validation_reason', 'N/A')}")
    print(f"  缺失数据: {final_state.get('missing_data', [])}")
    print(f"  数据充分性: {final_state.get('data_sufficiency', {})}")
    print(f"  重试次数: {final_state.get('retry_count', 0)}")
    print("=" * 60)

    return final_state


if __name__ == "__main__":
    # Windows 控制台中文输出保障
    # 【并入主线时的改动】`sys.stdout` 的静态类型是 TextIO，而 `reconfigure` 是 TextIOWrapper 的方法；
    # 直接调用过不了 mypy。用 getattr 取、取不到就跳过——这正是"没有这个方法时也不该炸"的语义。
    _reconfigure = getattr(sys.stdout, "reconfigure", None)
    if _reconfigure is not None:
        try:
            _reconfigure(encoding="utf-8")
        except Exception:
            pass

    # ---------- 用例1：滑坡正常获取 ----------
    r1 = run_data_agent("获取西藏林芝地区最近24小时滑坡相关观测数据", hazard_type="landslide")
    print("\n[结构化输出 - 供下游 Risk Agent 使用（用例1 landslide）]")
    print(
        json.dumps(
            {
                "status": r1.get("status"),
                "hazard_type": r1.get("hazard_type"),
                "data_quality": r1.get("data_quality"),
                "data_sufficiency": r1.get("data_sufficiency"),
                "missing_data": r1.get("missing_data"),
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    )

    # ---------- 用例2：中文别名归一化（"泥石流" → debris_flow） ----------
    r2 = run_data_agent("获取波密地区泥石流沟域观测数据", hazard_type="泥石流")
    print("\n【用例2 中文别名】status =", r2.get("status"), "| 归一化灾种 =", r2.get("hazard_type"))

    # ---------- 用例3：未知灾种 → failed，明确原因 ----------
    r3 = run_data_agent("获取观测数据", hazard_type="火山泥流")
    print("\n【用例3 未知灾种】status =", r3.get("status"), "| 原因 =", r3.get("error_message"))

    # ---------- 用例4：未提供灾种 → failed，明确原因 ----------
    r4 = run_data_agent("获取观测数据", hazard_type="")
    print("\n【用例4 缺少灾种】status =", r4.get("status"), "| 原因 =", r4.get("error_message"))
