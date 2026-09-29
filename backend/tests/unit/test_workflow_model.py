"""工作流模型静态校验测试：坏图必须在创建期被拒绝，而不是运行期才暴露。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from aegis.workflow.model import EdgeDef, NodeDef, WorkflowDef, find_cycle, topological_levels
from aegis.workflow.store import build_definition, new_workflow_id


def node(node_id: str, type_name: str = "delay", **overrides: object) -> dict:
    payload: dict = {"node_id": node_id, "type": type_name, "config": {"seconds": 0}}
    payload.update(overrides)
    return payload


def edge(source: str, target: str, condition: str = "") -> dict:
    return {"source": source, "target": target, "condition": condition}


class TestNodeDef:
    def test_id_pattern_enforced(self) -> None:
        with pytest.raises(ValidationError):
            NodeDef.model_validate(node("Bad_ID"))

    def test_timeout_must_fit_within_sla(self) -> None:
        with pytest.raises(ValidationError, match="timeout_ms"):
            NodeDef.model_validate({"node_id": "n1", "type": "delay", "sla_ms": 1_000, "timeout_ms": 5_000})

    def test_timeout_shorter_than_sla_allowed(self) -> None:
        """快速失败是合理配置：超时上限小于 SLA 预算。"""
        valid = NodeDef.model_validate({"node_id": "n1", "type": "delay", "sla_ms": 5_000, "timeout_ms": 1_000})
        assert valid.timeout_ms == 1_000

    def test_timeout_equal_to_sla_allowed(self) -> None:
        """边界：timeout_ms == sla_ms 合法（不变量只禁止 timeout 大于 sla）。"""
        valid = NodeDef.model_validate({"node_id": "n1", "type": "delay", "sla_ms": 3_000, "timeout_ms": 3_000})
        assert (valid.sla_ms, valid.timeout_ms) == (3_000, 3_000)

    def test_zero_retry_policy_allowed(self) -> None:
        """边界：max_attempts=0 表示不重试，必须能通过校验。"""
        valid = NodeDef.model_validate({"node_id": "n1", "type": "delay", "retry": {"max_attempts": 0, "backoff_ms": 0}})
        assert valid.retry.max_attempts == 0

    def test_sla_bounds(self) -> None:
        with pytest.raises(ValueError):
            NodeDef.model_validate({"node_id": "n1", "type": "delay", "sla_ms": 0})

    def test_extra_field_rejected(self) -> None:
        with pytest.raises(ValueError):
            NodeDef.model_validate({"node_id": "n1", "type": "delay", "config": {}, "mystery": 1})

    def test_on_failure_enum(self) -> None:
        with pytest.raises(ValueError):
            NodeDef.model_validate({"node_id": "n1", "type": "delay", "on_failure": "pray"})


class TestGraphValidation:
    def test_valid_chain(self) -> None:
        definition = build_definition(name="链", nodes=[node("a"), node("b")], edges=[edge("a", "b")])
        assert [n.node_id for n in definition.nodes] == ["a", "b"]
        assert definition.roots() == ["a"]

    def test_duplicate_node_id_rejected(self) -> None:
        with pytest.raises(ValueError, match="重复"):
            build_definition(name="重复", nodes=[node("a"), node("a")], edges=[])

    def test_cycle_rejected(self) -> None:
        with pytest.raises(ValueError, match="环"):
            build_definition(
                name="环",
                nodes=[node("a"), node("b"), node("c")],
                edges=[edge("a", "b"), edge("b", "c"), edge("c", "a")],
            )

    def test_self_loop_rejected(self) -> None:
        with pytest.raises(ValueError, match="自环"):
            build_definition(name="自环", nodes=[node("a")], edges=[edge("a", "a")])

    def test_dangling_edge_rejected(self) -> None:
        with pytest.raises(ValueError, match="源节点不存在"):
            build_definition(name="悬空", nodes=[node("a")], edges=[edge("ghost", "a")])
        with pytest.raises(ValueError, match="目标节点不存在"):
            build_definition(name="悬空2", nodes=[node("a")], edges=[edge("a", "ghost")])

    def test_empty_nodes_rejected(self) -> None:
        with pytest.raises(ValueError):
            build_definition(name="空", nodes=[], edges=[])

    def test_workflow_id_pattern(self) -> None:
        with pytest.raises(ValueError):
            WorkflowDef.model_validate({"workflow_id": "wf_short", "name": "n", "nodes": [node("a")], "edges": [], "version": 1})


class TestGraphHelpers:
    def test_find_cycle_returns_path(self) -> None:
        nodes = ["a", "b", "c"]
        edges = [EdgeDef(source="a", target="b"), EdgeDef(source="b", target="c"), EdgeDef(source="c", target="a")]
        cycle = find_cycle(nodes, edges)
        assert cycle is not None
        assert cycle[0] == cycle[-1]

    def test_find_cycle_none_for_dag(self) -> None:
        edges = [EdgeDef(source="a", target="b"), EdgeDef(source="b", target="c")]
        assert find_cycle(["a", "b", "c"], edges) is None

    def test_topological_levels_group_parallel_nodes(self) -> None:
        edges = [
            EdgeDef(source="root", target="left"),
            EdgeDef(source="root", target="right"),
            EdgeDef(source="left", target="join"),
            EdgeDef(source="right", target="join"),
        ]
        levels = topological_levels(["root", "left", "right", "join"], edges)
        assert levels == [["root"], ["left", "right"], ["join"]]

    def test_topological_levels_rejects_cycle(self) -> None:
        with pytest.raises(ValueError, match="环"):
            topological_levels(["a", "b"], [EdgeDef(source="a", target="b"), EdgeDef(source="b", target="a")])


class TestRepositoryShape:
    def test_signature_stable_regardless_of_order(self) -> None:
        first = build_definition(name="甲", nodes=[node("a"), node("b")], edges=[edge("a", "b")])
        second = build_definition(name="甲", nodes=[node("b"), node("a")], edges=[edge("a", "b")])
        assert first.signature() == second.signature()

    def test_ids_unique(self) -> None:
        assert len({new_workflow_id() for _ in range(200)}) == 200
