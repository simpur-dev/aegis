"""工作流图的性质测试：拓扑分层必须是"可并行执行"的合法调度，而不是一个看起来能跑的排序。

用 hypothesis 随机生成 DAG 来守四条不变式，比逐个写例子更能抓住编排逻辑的退化：
分层覆盖全部节点、边必须跨层向前、结果确定、成环必然被发现。
"""

from __future__ import annotations

import random

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from aegis.workflow.model import EdgeDef, NodeDef, WorkflowDef, topological_levels

NODE_ID_RE = r"^[a-z][a-z0-9_-]{0,31}$"


def node(index: int) -> NodeDef:
    return NodeDef(node_id=f"n{index}", type="assess_risk")


def edge(source: int, target: int) -> EdgeDef:
    return EdgeDef(source=f"n{source}", target=f"n{target}")


@st.composite
def acyclic_graph(draw: st.DrawMethod) -> tuple[list[str], list[EdgeDef]]:
    """只允许"小序号指向大序号"的边：构造性保证无环，让性质断言聚焦在分层语义上。"""
    size = draw(st.integers(min_value=1, max_value=18))
    ids = [f"n{i}" for i in range(size)]
    pairs = [(s, t) for s in range(size) for t in range(size) if s < t]
    chosen = draw(st.lists(st.sampled_from(pairs) if pairs else st.nothing(), max_size=min(len(pairs), 30), unique=True))
    return ids, [edge(s, t) for s, t in chosen]


class TestTopologicalLevels:
    @given(graph=acyclic_graph())
    @settings(max_examples=120, deadline=None)
    def test_levels_cover_every_node_exactly_once(self, graph: tuple[list[str], list[EdgeDef]]) -> None:
        ids, edges = graph
        levels = topological_levels(ids, edges)
        flattened = [node_id for level in levels for node_id in level]
        assert sorted(flattened) == sorted(ids)
        assert len(flattened) == len(set(flattened))

    @given(graph=acyclic_graph())
    @settings(max_examples=120, deadline=None)
    def test_every_edge_points_to_a_strictly_later_level(self, graph: tuple[list[str], list[EdgeDef]]) -> None:
        """边的方向必须体现在层级上：源严格早于目标，执行时才不可能"下游先于上游跑"。"""
        ids, edges = graph
        levels = topological_levels(ids, edges)
        depth = {node_id: index for index, level in enumerate(levels) for node_id in level}
        for link in edges:
            assert depth[link.source] < depth[link.target], link

    @given(graph=acyclic_graph(), seed=st.integers(min_value=0, max_value=1000))
    @settings(max_examples=60, deadline=None)
    def test_result_is_deterministic_under_input_order(self, graph: tuple[list[str], list[EdgeDef]], seed: int) -> None:
        """同一张图不论节点以什么顺序递交，分层必须一致——调度不能依赖注册顺序。"""
        ids, edges = graph
        reference = topological_levels(ids, edges)
        shuffled_ids = list(ids)
        shuffled_edges = list(edges)
        random.Random(seed).shuffle(shuffled_ids)
        random.Random(seed + 1).shuffle(shuffled_edges)
        assert topological_levels(shuffled_ids, shuffled_edges) == reference

    @given(graph=acyclic_graph())
    @settings(max_examples=60, deadline=None)
    def test_within_level_nodes_are_independent(self, graph: tuple[list[str], list[EdgeDef]]) -> None:
        """同层即"可并行"：层内任意两点之间不得有边，否则并行执行会读到未完成的上游。"""
        ids, edges = graph
        levels = topological_levels(ids, edges)
        for level in levels:
            members = set(level)
            assert all(not (link.source in members and link.target in members) for link in edges)

    def test_self_loop_is_detected(self) -> None:
        with pytest.raises(ValueError, match="环"):
            topological_levels(["a", "b"], [EdgeDef(source="a", target="b"), EdgeDef(source="b", target="b")])

    def test_two_node_cycle_is_detected(self) -> None:
        with pytest.raises(ValueError, match="环"):
            topological_levels(["a", "b"], [EdgeDef(source="a", target="b"), EdgeDef(source="b", target="a")])

    def test_longer_cycle_is_detected(self) -> None:
        ids = ["a", "b", "c", "d"]
        edges = [
            EdgeDef(source="a", target="b"),
            EdgeDef(source="b", target="c"),
            EdgeDef(source="c", target="d"),
            EdgeDef(source="d", target="a"),
        ]
        with pytest.raises(ValueError, match="环"):
            topological_levels(ids, edges)

    def test_cycle_downstream_of_an_acyclic_prefix_is_still_detected(self) -> None:
        """前段无环、尾段成环：Kahn 会正常收敛前半部分，必须靠"覆盖数不等于节点数"抓住残环。"""
        ids = ["a", "b", "c", "d"]
        edges = [
            EdgeDef(source="a", target="b"),
            EdgeDef(source="b", target="c"),
            EdgeDef(source="c", target="d"),
            EdgeDef(source="d", target="c"),
        ]
        with pytest.raises(ValueError, match="环"):
            topological_levels(ids, edges)

    def test_isolated_nodes_each_get_a_level(self) -> None:
        levels = topological_levels(["a", "b", "c"], [])
        assert levels == [["a", "b", "c"]]

    def test_linear_chain_produces_one_node_per_level(self) -> None:
        ids = ["a", "b", "c"]
        edges = [EdgeDef(source="a", target="b"), EdgeDef(source="b", target="c")]
        assert topological_levels(ids, edges) == [["a"], ["b"], ["c"]]

    def test_empty_graph_yields_no_levels(self) -> None:
        assert topological_levels([], []) == []


class TestNodeDefConstraints:
    """节点定义约束：这些边界直接决定"≥10 类节点、≤2s 调度"能不能被平台表达。"""

    def test_node_id_and_type_patterns_are_enforced(self) -> None:
        with pytest.raises(ValueError):
            NodeDef(node_id="1abc", type="assess_risk")
        with pytest.raises(ValueError):
            NodeDef(node_id="abc", type="")

    @pytest.mark.parametrize("policy", ["retry", "degrade", "escalate", "skip", "abort"])
    def test_failure_policies_are_the_documented_five(self, policy: str) -> None:
        assert NodeDef(node_id="a", type="assess_risk", on_failure=policy).on_failure == policy

    def test_unknown_failure_policy_rejected(self) -> None:
        with pytest.raises(ValueError):
            NodeDef(node_id="a", type="assess_risk", on_failure="explode")

    def test_retry_bounds(self) -> None:
        assert node(1).retry.max_attempts == 1
        with pytest.raises(ValueError):
            NodeDef(node_id="a", type="assess_risk", retry={"max_attempts": 99})  # type: ignore[typeddict-item]

    def test_edge_condition_is_optional_but_bounded(self) -> None:
        assert EdgeDef(source="a", target="b").condition == ""
        with pytest.raises(ValueError):
            EdgeDef(source="a", target="b", condition="x" * 65)


class TestDefinitionHelpers:
    """`WorkflowDef.outgoing` 是引擎取下游的唯一入口，行为必须与图定义严格一致。"""

    def _definition(self) -> WorkflowDef:
        return WorkflowDef(
            workflow_id="wf_" + "a" * 12,
            name="泥石流处置",
            nodes=[node(i) for i in range(4)],
            edges=[edge(0, 1), edge(0, 2), edge(1, 3)],
        )

    def test_outgoing_returns_only_edges_from_that_node(self) -> None:
        definition = self._definition()
        assert [e.target for e in definition.outgoing("n0")] == ["n1", "n2"]
        assert [e.target for e in definition.outgoing("n3")] == []

    def test_outgoing_is_stable_across_calls(self) -> None:
        definition = self._definition()
        assert definition.outgoing("n0") == definition.outgoing("n0")

    def test_levels_match_a_hand_written_schedule(self) -> None:
        """一条可复现的期望：n0 并行起步，n1/n2 同层，n3 收尾。"""
        definition = self._definition()
        levels = topological_levels([n.node_id for n in definition.nodes], definition.edges)
        assert levels == [["n0"], ["n1", "n2"], ["n3"]]
