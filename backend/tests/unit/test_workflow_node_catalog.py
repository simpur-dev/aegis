"""节点目录（catalog）的对账测试：注册表自洽 + 面板出口的字段完整。

目录不只是"/node-types 返回一个列表"：它是面板分组、变量选择器（上游能给出什么）
与发布校验的共同真源。一条错的元数据在运行期才暴露，症状会被误读成"智能体没出结果"。
"""

from __future__ import annotations

from typing import Any

import pytest

from aegis.workflow.engine import WorkflowEngine
from aegis.workflow.nodes import (
    NODE_CATEGORIES,
    NodeContext,
    NodeOutcome,
    NodeRegistry,
    NodeSpec,
    default_registry,
)


async def _noop(ctx: NodeContext, config: dict[str, Any]) -> NodeOutcome:
    return NodeOutcome(output={"ok": True})


def _registry(*specs: NodeSpec) -> NodeRegistry:
    registry = NodeRegistry()
    for spec in specs:
        registry.register(spec)
    return registry


def test_默认注册表通过启动对账且每个面板节点都声明了产出() -> None:
    registry = default_registry()
    assert registry.audit() == []
    panel = registry.panel_specs()
    assert len(panel) == 18
    for spec in panel:
        assert spec.category in NODE_CATEGORIES, spec.type_name
        assert spec.outputs, f"{spec.type_name} 没有产出键，变量选择器列不出上游能给什么"
    # 内部原语不进面板，但仍在引擎注册表里（链路段模板建在它上面）
    assert "chain_stage" in registry.names()
    assert "chain_stage" not in {spec.type_name for spec in panel}


@pytest.mark.parametrize(
    ("spec", "needle"),
    [
        (NodeSpec("bad", "", _noop), "描述为空"),
        (NodeSpec("bad", "说明", _noop, ("x",), ("x",)), "必填与可选重复"),
        (NodeSpec("bad", "说明", _noop, ("x",), min_config={"y": 1}), "下限声明了未注册的参数"),
        (NodeSpec("bad", "说明", _noop, outputs=("x", "x")), "产出键重复"),
        (NodeSpec("bad", "说明", _noop), "画布节点必须声明至少一个产出键"),
        (NodeSpec("bad", "说明", _noop, ("x",), outputs=("X",)), "产出键名不合法"),
    ],
)
def test_对账能点名每一类坏元数据(spec: NodeSpec, needle: str) -> None:
    problems = _registry(spec).audit()
    assert any(needle in row for row in problems), f"坏目录没有报出 {needle!r}：{problems}"


def test_未知类别与空面板同样拒收() -> None:
    bad_category = NodeSpec("bad", "说明", _noop, outputs=("ok",), category="nope")  # type: ignore[arg-type]
    assert any("未知类别" in row for row in _registry(bad_category).audit())
    assert any("没有任何可上面板的节点" in row for row in _registry().audit())


def test_引擎构造即对账_坏注册表不许带病启动() -> None:
    """启动期而不是首次请求期：进程起来的那一刻目录就该是对的。"""
    bad = _registry(NodeSpec("bad", "", _noop, outputs=("ok",)))
    with pytest.raises(ValueError, match="节点注册表不自洽"):
        WorkflowEngine(registry=bad)
