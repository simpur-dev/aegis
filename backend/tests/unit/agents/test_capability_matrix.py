"""能力矩阵门禁：注册、派发、路由三方一致性的**机器守卫**。

这张表（`agents/capabilities.py`）是智能体层的单一真源，三条纪律在这里钉死：

1. **每项能力恰好一个主人**——两个本体声明同一项 `(agent_type, capability)`，网关的
   `pick()` 会按负载在两者之间取舍，"谁应答"就变成不可复现的随机数；
2. **每项 `dispatched=True` 的能力都有真实派发点**——注册了却没人派的能力就是
   "接了但接不了"，灾种模块作者当年正是因此拒绝注册另外五个本体；这条把教训变成
   机器门禁；
3. **每项 `dispatched=False` 的能力都是闭环内腿**——它的输入只有闭环才产得齐，
   不许悄悄出现在平台热路径上，也不许悬空没人调。

派发点的判定是**源码扫描**（stages.py 的 `capability=` 关键字实参 + API 路由层），
与矩阵对不上就红——改矩阵必须同步改派发点，反之亦然。
"""

from __future__ import annotations

import ast
import pathlib

from aegis.agents.capabilities import CAPABILITY_MATRIX, CAPABILITY_SPECS
from aegis.domain.enums import AgentType

BACKEND = pathlib.Path(__file__).resolve().parents[3]

#: 派发点扫描的文件：平台热路径（stages）与闭环入口（API 路由）。
DISPATCH_SOURCES = [
    BACKEND / "src" / "aegis" / "pipeline" / "stages.py",
    BACKEND / "src" / "aegis" / "api" / "routers" / "hazards.py",
]

#: 闭环内腿的注册表（闭环五步 → 能力）。`dispatched=False` 的能力必须出现在这里。
BUS_LEGS_SOURCE = BACKEND / "src" / "aegis" / "agents" / "hazards" / "manager_agent" / "dispatcher.py"


def _capability_literals_in(path: pathlib.Path) -> set[str]:
    """从源码里扫出所有传给 `capability=` 的**字面量**实参。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if keyword.arg == "capability" and isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, str):
                found.add(keyword.value.value)
    return found


def test_每项能力恰好一个主人() -> None:
    seen: dict[tuple[str, str], str] = {}
    for spec in CAPABILITY_MATRIX:
        key = (spec.agent_type.value, spec.capability)
        assert key not in seen, f"能力 {spec.capability} 有两个主人：{seen.get(key)} 与 {spec.owner}"
        seen[key] = spec.owner


def test_owner_id符合平台命名() -> None:
    """`<agent_type>.<instance>`：与 `CapabilityRegistration.agent_id` 的正则口径一致。"""
    for spec in CAPABILITY_MATRIX:
        assert spec.owner.startswith(f"{spec.agent_type.value}."), (
            f"能力 {spec.capability} 的 owner {spec.owner} 与其 agent_type（{spec.agent_type.value}）不一致"
        )


def test_派发的能力都有真实派发点() -> None:
    """防"接了但接不了"：dispatched=True 的能力必须能在派发点源码里找到。"""
    dispatched_literals: set[str] = set()
    for source in DISPATCH_SOURCES:
        dispatched_literals |= _capability_literals_in(source)
    missing = [spec.capability for spec in CAPABILITY_MATRIX if spec.dispatched and spec.capability not in dispatched_literals]
    assert missing == [], f"这些能力登记了却没有派发点（接了但接不了）：{missing}"


def test_派发点的每项能力都在册() -> None:
    """防反向漂移：派发点里出现矩阵没有的能力，路由照样生效但没人管它的一致性。"""
    dispatched_literals: set[str] = set()
    for source in DISPATCH_SOURCES:
        dispatched_literals |= _capability_literals_in(source)
    unknown = dispatched_literals - set(CAPABILITY_SPECS)
    assert not unknown, f"派发点用到了矩阵之外的能力：{sorted(unknown)}——先在 capabilities.py 登记"


def test_非派发腿要么在闭环要么是本体自发事件() -> None:
    """dispatched=False 的能力必须可归属：闭环内腿（BUS_LEGS）或本体自发事件腿。

    `trigger_recognize` 是事件腿：perceive 本体自己 `emit_event` 上报命中，
    平台不派单——它的合法性判据是动作在**本体的源码里被发出**，而不是出现在
    闭环派单表里。
    """
    bus_legs = BUS_LEGS_SOURCE.read_text(encoding="utf-8")
    unaccounted = []
    for spec in CAPABILITY_MATRIX:
        if spec.dispatched:
            continue
        in_bus_legs = f'"{spec.capability}"' in bus_legs
        # 事件腿判据：动作枚举成员被某个本体**实际使用**（emit 处引用
        # `Action.<MEMBER>`——纯文档字符串里的提及不算，AST 只看代码节点）。
        emitted = False
        if not in_bus_legs:
            for path in (BACKEND / "src" / "aegis" / "agents").rglob("*.py"):
                tree = ast.parse(path.read_text(encoding="utf-8"))
                if any(
                    isinstance(node, ast.Attribute)
                    and isinstance(node.value, ast.Name)
                    and node.value.id == "Action"
                    and node.attr == spec.action.name
                    for node in ast.walk(tree)
                ):
                    emitted = True
                    break
        if not (in_bus_legs or emitted):
            unaccounted.append(spec.capability)
    assert unaccounted == [], f"这些非派发能力既不在闭环派单表、也没有事件腿出处：{unaccounted}"
    # 平台派发点不得派它们
    dispatched_literals: set[str] = set()
    for source in DISPATCH_SOURCES:
        dispatched_literals |= _capability_literals_in(source)
    leaked = [spec.capability for spec in CAPABILITY_MATRIX if not spec.dispatched and spec.capability in dispatched_literals]
    assert not leaked, f"闭环内腿/事件腿泄漏到了平台热路径：{leaked}"


def test_每个流程阶段都有能力覆盖() -> None:
    """五段（perceive/assess/plan/execute/feedback）每段至少一项能力——能力矩阵读起来就是链路。"""
    covered = {spec.agent_type for spec in CAPABILITY_MATRIX}
    assert covered == set(AgentType), f"缺阶段的覆盖：{set(AgentType) - covered}"
