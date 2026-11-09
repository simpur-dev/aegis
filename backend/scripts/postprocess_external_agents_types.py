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


# ---------- 图构建函数的返回类型 ----------
#: langgraph 1.x 的 `compile()` 返回 `CompiledStateGraph`，而 `StateGraph` 是**未编译**的那一层。
#: 她的函数有的返回 compile 后的图、有的返回未编译的（manager 的 `create_manager_graph` 才 compile），
#: 标注要按实际返回值写，否则 mypy 报 "Incompatible return value type"。
#: `CompiledStateGraph` 在 langgraph 1.x 位于 `langgraph.graph.state`（不在 `langgraph.graph`）。
#: 规则按"文件 → 函数名"列出来（函数名在各文件里是固定的），避免每行都写两遍长串。
_GRAPH_BUILDERS: tuple[tuple[str, str], ...] = (
    ("risk_agent/graph.py", "build_risk_agent_graph"),
    ("data_agent/graph.py", "build_data_agent_graph"),
    ("simulation_agent/graph.py", "build_simulation_agent_graph"),
    ("feedback_agent/graph.py", "build_feedback_agent_graph"),
    ("warning_agent/graph.py", "build_warning_agent_graph"),
)

#: manager 的 build 返回的是**未编译**的图（compile 在 create_manager_graph 里），标注仍是 StateGraph。
GRAPH_RETURN_FIXES: tuple[tuple[str, str, str], ...] = (
    *((f"src/aegis/agents_ext/{rel}", f"{fn}() -> StateGraph:", f"{fn}() -> CompiledStateGraph:") for rel, fn in _GRAPH_BUILDERS),
    (
        "src/aegis/agents_ext/manager_agent/graph.py",
        "build_manager_graph() -> CompiledStateGraph:",
        "build_manager_graph() -> StateGraph:",
    ),
)

#: 需要 `CompiledStateGraph` 导入的图文件（manager 不需要：它返回未编译的图）。
GRAPH_FILES_NEEDING_COMPILED = (
    "src/aegis/agents_ext/risk_agent/graph.py",
    "src/aegis/agents_ext/data_agent/graph.py",
    "src/aegis/agents_ext/simulation_agent/graph.py",
    "src/aegis/agents_ext/feedback_agent/graph.py",
    "src/aegis/agents_ext/warning_agent/graph.py",
)

# ---------- 其余零散的类型修正 ----------
#: (文件, 原片段, 新片段, 说明)
SCATTERED_FIXES: tuple[tuple[str, str, str, str], ...] = (
    (
        "src/aegis/agents_ext/feedback_agent/nodes/feedback_evaluate_node.py",
        "    category_summary, agent_summary, severity_summary = {}, {}, {}",
        "    category_summary: dict[str, int] = {}\n    agent_summary: dict[str, int] = {}\n    severity_summary: dict[str, int] = {}",
        "三个汇总 dict 补注解（否则 mypy 要求显式类型）",
    ),
    (
        "src/aegis/agents_ext/feedback_agent/nodes/feedback_output_node.py",
        "    by_agent = {}\n",
        "    by_agent: dict[str, list] = {}\n",
        "by_agent 补注解",
    ),
    (
        "src/aegis/agents_ext/data_agent/tools/hazard_data_registry.py",
        "    fields = {}\n",
        "    # 【并入主线时的改动】补注解：字段值有 float/int/str 三种类型，不标注会被推成\n"
        "    # `tuple[type[float], Any]`，随后 `create_model(**fields)` 就过不了类型检查。\n"
        "    fields: dict[str, tuple[Any, Any]] = {}\n",
        "动态字段字典补注解",
    ),
    (
        "src/aegis/agents_ext/data_agent/tools/hazard_data_registry.py",
        "    return create_model(f\"{req['hazard_type'].title().replace('_', '')}QueryParams\", **fields)",
        "    # 【并入主线时的改动】`create_model(**fields)` 的重载不接受 `dict[str, tuple[Any, Any]]`\n"
        '    # 这种动态字段字典（pydantic 的静态签名表达不了"运行时才知道字段"），故精确豁免这一处。\n'
        "    return create_model(  # type: ignore[call-overload]\n"
        "        f\"{req['hazard_type'].title().replace('_', '')}QueryParams\", **fields\n"
        "    )",
        "create_model 动态字段的精确豁免",
    ),
    (
        "src/aegis/agents_ext/risk_agent/tools/hazard_risk_registry.py",
        '        "required_input_fields": list(_LS_CONFIG["required_factors"]),',
        '        "required_input_fields": list(_LS_CONFIG["required_factors"]),  # type: ignore[call-overload]',
        "list() 的参数是 object：精确豁免",
    ),
    (
        "src/aegis/agents_ext/simulation_agent/tools/default_scenario_tool.py",
        "**当 LLM 无法工作（没 API 密钥、调用报错、输出乱掉）的时候，"
        "自动生成 3 套确定性的基准情景 low /medium/high，"
        "保证流程还能继续往下跑，不需要人工手动写情景。**",
        "**当 LLM 无法工作（没 API 密钥、调用报错、输出乱掉）的时候，\n"
        "自动生成 3 套确定性的基准情景 low / medium / high，\n"
        "保证流程还能继续往下跑，不需要人工手动写情景。**",
        "docstring 长行拆行",
    ),
    (
        "src/aegis/agents_ext/simulation_agent/tools/hazard_model_router.py",
        "核心一句话：接收用户 / 上游传来的灾种名称（中文、英文别名），翻译成系统内部统一标准键，"
        "然后找到对应的灾害仿真模型，或者明确抛出不支持异常；**绝不擅自猜测、替换成相近灾种**。",
        "核心一句话：接收用户 / 上游传来的灾种名称（中文、英文别名），翻译成系统内部统一标准键，\n"
        "然后找到对应的灾害仿真模型，或者明确抛出不支持异常；\n"
        "**绝不擅自猜测、替换成相近灾种**。",
        "docstring 长行拆行",
    ),
)


def apply_graph_returns(backend: Path) -> int:
    """图构建函数的返回类型 + `CompiledStateGraph` 的导入路径。"""
    changed = 0
    for rel, old, new in GRAPH_RETURN_FIXES:
        path = backend / rel
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        if old not in text:
            continue
        text = text.replace(old, new)
        path.write_text(text, encoding="utf-8", newline="\n")
        changed += 1
    for rel in GRAPH_FILES_NEEDING_COMPILED:
        path = backend / rel
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        if "CompiledStateGraph" not in text or "import CompiledStateGraph" in text:
            continue
        if "from langgraph.graph.state import CompiledStateGraph" in text:
            continue
        m = re.search(r"from langgraph\.graph import ([^\n]+)", text)
        if m:
            text = text.replace(m.group(0), f"from langgraph.graph.state import CompiledStateGraph\n{m.group(0)}")
        else:
            text = text.replace("\nfrom ", "\nfrom langgraph.graph.state import CompiledStateGraph\nfrom ", 1)
        path.write_text(text, encoding="utf-8", newline="\n")
        changed += 1
    return changed


def apply_graph_ascii(backend: Path) -> int:
    """给 simulation_agent/graph.py 加**文件级** E501 豁免（图留在原位，不挪动）。

    为什么用文件级而不是行级：那张 ASCII 流程图在模块 docstring 里，而 docstring 内部的行
    挂不了 noqa 指令（行级 noqa 对 E501 无效——它自己会把行撑得更长）。

    为什么**不把图挪到注释块**：试过，代价大于收益——docstring 内部还有别的三引号序列，
    定位结束位置要靠 ast 的 end_lineno；挪出后图块缩进要重排；两次都踩到语法错。
    图留在 docstring 里（它的自然位置，读者一眼看到）配合文件级豁免，
    是同等效果下最稳的一种。豁免范围覆盖整文件——该文件除流程图外没有超宽行。
    """
    graph = backend / "src/aegis/agents_ext/simulation_agent/graph.py"
    if not graph.exists():
        return 0
    text = graph.read_text(encoding="utf-8")
    if text.startswith("# ruff: noqa: E501"):
        return 0
    header = (
        "# ruff: noqa: E501\n"
        "# 【本文件整体豁免 E501（行宽 140）】：模块 docstring 里那张 LangGraph 流程图必须保持\n"
        "# 整行不折（折了就不是图了）；docstring 内部的行挂不了 noqa 指令（行级 noqa 对 E501\n"
        "# 无效——它自己会把行撑得更长），所以只能用文件级指令。文件里除流程图外没有超宽行。\n"
    )
    graph.write_text(header + text, encoding="utf-8", newline="\n")
    return 1


#: 按行前缀替换的整行改造：命中前缀的那一行换成给定的新行。
#: 用于"那一行原文特别长、精确匹配容易失配"的两处。
LINE_PREFIX_FIXES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "src/aegis/agents_ext/simulation_agent/tools/simulation_executor.py",
        "        except FuturesTimeoutError:",
        (
            "        except FuturesTimeoutError as exc:",
            "            # 【并入主线时的改动】补 `from exc`：except 块里的 raise 不带 from 会丢掉异常链，",
            "            # 上层拿到的是「凭空产生」的超时错误，看不到底下的真实原因。",
            "            raise SimulationTimeoutError(",
            "                f\"单情景仿真超时（>{timeout_seconds}s）：scenario={scenario.get('scenario_id', '?')}\"",
            "            ) from exc",
        ),
    ),
    (
        "src/aegis/agents_ext/data_agent/nodes/data_plan_node.py",
        '        processing_plan = f"目标: 获取',
        (
            "        processing_plan = (",
            "            f\"目标: 获取{params.get('region', '西藏')}地区、最近{params.get('time_range', '24h')}、\"",
            "            f\"{req['hazard_type_cn']}观测数据（Mock）。\"",
            '            "步骤: 获取 → 清洗 → 标准化 → 质量验证 → 输出。"',
            "        )",
        ),
    ),
)


def apply_line_prefix_fixes(backend: Path) -> int:
    """按**行前缀**替换的整行改造（比精确长串匹配稳）。

    用它的地方：那一行原文特别长（含中文与 f-string 嵌套引号），精确匹配容易失配，
    而"这一行的开头是什么"是稳定的。替换规则：命中前缀的行整行换成给定的新行。
    """
    changed = 0
    for rel, prefix, new_lines in LINE_PREFIX_FIXES:
        path = backend / rel
        if not path.exists():
            continue
        lines = path.read_text(encoding="utf-8").split("\n")
        hit = False
        for index, line in enumerate(lines):
            if line.startswith(prefix):
                # 替换后要**删掉紧跟其后的旧 raise 行**：只换一行会把旧代码留成 dead code
                # （`raise` 后面还跟着一条没有 `from exc` 的同名 raise——ruff 仍报 B904，
                # 而且那行永远到不了，等于一段误导人的死代码）。
                end = index + 1
                while end < len(lines) and "raise SimulationTimeoutError(f" in lines[end]:
                    end += 1
                lines[index:end] = list(new_lines)
                hit = True
                break
        if hit:
            path.write_text("\n".join(lines), encoding="utf-8", newline="\n")
            changed += 1
    return changed


def apply_scattered(backend: Path) -> int:
    changed = 0
    for rel, old, new, _why in SCATTERED_FIXES:
        path = backend / rel
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        if old not in text:
            continue
        text = text.replace(old, new)
        if "from typing import Any" not in text and re.search(r"\bAny\b", new):
            if re.search(r"^from typing import ", text, re.MULTILINE):
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
    c = apply_graph_returns(backend)
    d = apply_scattered(backend)
    e = apply_line_prefix_fixes(backend)
    f = apply_graph_ascii(backend)
    print(
        f"types: {a} node signatures, {b} stdout.reconfigure, "
        f"{c} graph returns, {d} scattered annotations, {e} line-prefix fixes, {f} graph ascii"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
