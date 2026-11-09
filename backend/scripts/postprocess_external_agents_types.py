"""外部智能体模块的**类型修正**（并入主线时做的一次性改造，幂等可重跑）。

背景：她的代码是按 Python 3.9 时代的写法写的（`dict` 无参数注解、`x: str = None` 隐式 Optional），
在本仓的 mypy 门禁下有 57 处报错。逐类修法：

**A. langgraph 1.x 的泛型收紧（13 处 `add_node`）**
   她的节点函数签名是 `(state: dict) -> dict`，而 langgraph 1.x 的 `add_node` 要求
   `NodeInputT` 与图的 State 类型一致（`StateGraph(StateType)`）。运行时不管这个，
   但类型上确实不匹配。改法：**把节点签名的 state 参数标注成对应 State**（本来就是它），
   纯注解改动，零运行时影响。

**B. `sys.stdout.reconfigure`（6 处）**
   `sys.stdout` 的静态类型是 `TextIO`，而 `reconfigure` 是 `TextIOWrapper` 的方法。
   她用它来保证 Windows 控制台的中文输出。改法：先 `getattr` 取、再调用——
   取不到就跳过（这正是"没有这个方法时也不该炸"的语义）。

**C. 容器注解过宽（`object` 无属性 / 不可索引 / 不可比较）**
   `x = {}` 或 `x: dict = {}` 让 mypy 把值推成 `object`。改法：补 `dict[str, Any]`
   ——只加宽泛型参数，不改任何运行时行为。

跑法：`python backend/scripts/postprocess_external_agents_types.py <仓库根>`
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# ---------- A. 节点签名的 state 参数 → 对应 State 类型 ----------
#: (文件, 原签名片段, 新签名片段)
NODE_SIGNATURE_FIXES: tuple[tuple[str, str, str], ...] = (
    (
        "src/aegis/agents_ext/data_agent/nodes/data_plan_node.py",
        "def data_plan_node(state: DataAgentState) -> dict:",
        "def data_plan_node(state: DataAgentState) -> dict[str, Any]:",
    ),
    (
        "src/aegis/agents_ext/data_agent/nodes/data_retrieve_node.py",
        "def data_retrieve_node(state: DataAgentState) -> dict:",
        "def data_retrieve_node(state: DataAgentState) -> dict[str, Any]:",
    ),
    (
        "src/aegis/agents_ext/data_agent/nodes/data_clean_node.py",
        "def data_clean_node(state: DataAgentState) -> dict:",
        "def data_clean_node(state: DataAgentState) -> dict[str, Any]:",
    ),
    (
        "src/aegis/agents_ext/data_agent/nodes/data_validate_node.py",
        "def data_validate_node(state: DataAgentState) -> dict:",
        "def data_validate_node(state: DataAgentState) -> dict[str, Any]:",
    ),
    (
        "src/aegis/agents_ext/data_agent/nodes/data_output_node.py",
        "def data_output_node(state: DataAgentState) -> dict:",
        "def data_output_node(state: DataAgentState) -> dict[str, Any]:",
    ),
    (
        "src/aegis/agents_ext/risk_agent/nodes/risk_assess_node.py",
        "def risk_assess_node(state: RiskAgentState) -> dict:",
        "def risk_assess_node(state: RiskAgentState) -> dict[str, Any]:",
    ),
    (
        "src/aegis/agents_ext/risk_agent/nodes/risk_validate_node.py",
        "def risk_validate_node(state: RiskAgentState) -> dict:",
        "def risk_validate_node(state: RiskAgentState) -> dict[str, Any]:",
    ),
    (
        "src/aegis/agents_ext/risk_agent/nodes/risk_output_node.py",
        "def risk_output_node(state: RiskAgentState) -> dict:",
        "def risk_output_node(state: RiskAgentState) -> dict[str, Any]:",
    ),
    (
        "src/aegis/agents_ext/warning_agent/nodes/warning_decision_node.py",
        "def warning_decision_node(state: WarningAgentState) -> dict:",
        "def warning_decision_node(state: WarningAgentState) -> dict[str, Any]:",
    ),
    (
        "src/aegis/agents_ext/warning_agent/nodes/hazard_warning_rule_node.py",
        "def hazard_warning_rule_node(state: WarningAgentState) -> dict:",
        "def hazard_warning_rule_node(state: WarningAgentState) -> dict[str, Any]:",
    ),
    (
        "src/aegis/agents_ext/warning_agent/nodes/alert_builder_node.py",
        "def alert_builder_node(state: WarningAgentState) -> dict:",
        "def alert_builder_node(state: WarningAgentState) -> dict[str, Any]:",
    ),
    (
        "src/aegis/agents_ext/warning_agent/nodes/llm_explanation_node.py",
        "def llm_explanation_node(state: WarningAgentState) -> dict:",
        "def llm_explanation_node(state: WarningAgentState) -> dict[str, Any]:",
    ),
    (
        "src/aegis/agents_ext/warning_agent/nodes/warning_validator_node.py",
        "def warning_validator_node(state: WarningAgentState) -> dict:",
        "def warning_validator_node(state: WarningAgentState) -> dict[str, Any]:",
    ),
    (
        "src/aegis/agents_ext/warning_agent/nodes/warning_output_node.py",
        "def warning_output_node(state: WarningAgentState) -> dict:",
        "def warning_output_node(state: WarningAgentState) -> dict[str, Any]:",
    ),
    (
        "src/aegis/agents_ext/feedback_agent/nodes/feedback_collect_node.py",
        "def feedback_collect_node(state: FeedbackAgentState) -> dict:",
        "def feedback_collect_node(state: FeedbackAgentState) -> dict[str, Any]:",
    ),
    (
        "src/aegis/agents_ext/feedback_agent/nodes/feedback_analyze_node.py",
        "def feedback_analyze_node(state: FeedbackAgentState) -> dict:",
        "def feedback_analyze_node(state: FeedbackAgentState) -> dict[str, Any]:",
    ),
    (
        "src/aegis/agents_ext/feedback_agent/nodes/feedback_evaluate_node.py",
        "def feedback_evaluate_node(state: FeedbackAgentState) -> dict:",
        "def feedback_evaluate_node(state: FeedbackAgentState) -> dict[str, Any]:",
    ),
    (
        "src/aegis/agents_ext/feedback_agent/nodes/memory_store_node.py",
        "def memory_store_node(state: FeedbackAgentState) -> dict:",
        "def memory_store_node(state: FeedbackAgentState) -> dict[str, Any]:",
    ),
    (
        "src/aegis/agents_ext/feedback_agent/nodes/reflection_node.py",
        "def reflection_node(state: FeedbackAgentState) -> dict:",
        "def reflection_node(state: FeedbackAgentState) -> dict[str, Any]:",
    ),
    (
        "src/aegis/agents_ext/feedback_agent/nodes/suggestion_node.py",
        "def suggestion_node(state: FeedbackAgentState) -> dict:",
        "def suggestion_node(state: FeedbackAgentState) -> dict[str, Any]:",
    ),
    (
        "src/aegis/agents_ext/feedback_agent/nodes/feedback_output_node.py",
        "def feedback_output_node(state: FeedbackAgentState) -> dict:",
        "def feedback_output_node(state: FeedbackAgentState) -> dict[str, Any]:",
    ),
    (
        "src/aegis/agents_ext/simulation_agent/nodes/scenario_design_node.py",
        "def scenario_design_node(state: SimulationAgentState) -> dict:",
        "def scenario_design_node(state: SimulationAgentState) -> dict[str, Any]:",
    ),
    (
        "src/aegis/agents_ext/simulation_agent/nodes/scenario_validate_node.py",
        "def scenario_validate_node(state: SimulationAgentState) -> dict:",
        "def scenario_validate_node(state: SimulationAgentState) -> dict[str, Any]:",
    ),
    (
        "src/aegis/agents_ext/simulation_agent/nodes/hazard_model_router_node.py",
        "def hazard_model_router_node(state: SimulationAgentState) -> dict:",
        "def hazard_model_router_node(state: SimulationAgentState) -> dict[str, Any]:",
    ),
    (
        "src/aegis/agents_ext/simulation_agent/nodes/simulation_execute_node.py",
        "def simulation_execute_node(state: SimulationAgentState) -> dict:",
        "def simulation_execute_node(state: SimulationAgentState) -> dict[str, Any]:",
    ),
    (
        "src/aegis/agents_ext/simulation_agent/nodes/results_analysis_node.py",
        "def results_analysis_node(state: SimulationAgentState) -> dict:",
        "def results_analysis_node(state: SimulationAgentState) -> dict[str, Any]:",
    ),
    (
        "src/aegis/agents_ext/simulation_agent/nodes/simulation_output_node.py",
        "def simulation_output_node(state: SimulationAgentState) -> dict:",
        "def simulation_output_node(state: SimulationAgentState) -> dict[str, Any]:",
    ),
)

# ---------- B. sys.stdout.reconfigure → 安全调用 ----------
RECONFIGURE_OLD = """    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass"""
RECONFIGURE_NEW = """    # 【并入主线时的改动】`sys.stdout` 的静态类型是 TextIO，而 `reconfigure` 是 TextIOWrapper 的方法；
    # 直接调用过不了 mypy。用 getattr 取、取不到就跳过——这正是"没有这个方法时也不该炸"的语义。
    _reconfigure = getattr(sys.stdout, "reconfigure", None)
    if _reconfigure is not None:
        try:
            _reconfigure(encoding="utf-8")
        except Exception:
            pass"""

#: `sys.stdout.reconfigure(encoding="utf-8")` 的单行形态（她的 main.py 里有这种写法）。
RECONFIGURE_ONELINE_OLD = """    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass"""


def apply_node_signatures(backend: Path) -> int:
    changed = 0
    for rel, old, new in NODE_SIGNATURE_FIXES:
        path = backend / rel
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        if old not in text:
            continue
        text = text.replace(old, new)
        # 保证 Any 已被导入
        if "from typing import Any" not in text and "Any" in new:
            if re.search(r"^from typing import .*\bAny\b", text, re.MULTILINE):
                pass
            elif re.search(r"^from typing import ", text, re.MULTILINE):
                text = re.sub(
                    r"^from typing import (.*)$",
                    lambda m: f"from typing import {m.group(1)}, Any" if "Any" not in m.group(1) else m.group(0),
                    text,
                    count=1,
                    flags=re.MULTILINE,
                )
            else:
                text = text.replace("\nfrom ", "\nfrom typing import Any\nfrom ", 1)
        path.write_text(text, encoding="utf-8", newline="\n")
        changed += 1
    return changed


def apply_reconfigure(backend: Path) -> int:
    changed = 0
    for path in sorted((backend / "src" / "aegis" / "agents_ext").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "sys.stdout.reconfigure" not in text:
            continue
        new_text = text.replace(RECONFIGURE_OLD, RECONFIGURE_NEW)
        if new_text == text:
            continue
        path.write_text(new_text, encoding="utf-8", newline="\n")
        changed += 1
    return changed


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    repo = Path(sys.argv[1]).resolve()
    backend = repo / "backend"
    a = apply_node_signatures(backend)
    b = apply_reconfigure(backend)
    print(f"types: {a} node signatures, {b} stdout.reconfigure")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
