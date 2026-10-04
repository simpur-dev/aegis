"""工作流模型与静态校验。

设计取向（对应课题"柔性可视化工作流引擎"）：
- 定义与实例分离，定义带版本：灾中修订模板不影响在途实例（"柔性"的第一处体现）；
- 节点类型注册制：新增节点 = 注册一个 NodeSpec，不改引擎内核；
- 入站校验严格：环、重复 ID、悬空边、未知类型、必填参数缺失、非法 SLA 一律拒绝，
  宁可创建失败也不留下运行期才暴露的坏图。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_NODES = 64
MAX_EDGES = 256


def normalize_branch(value: str) -> str:
    """分支名与边条件的**同一套**归一规则。

    两边必须走这一个函数：`condition` 存的是归一后的值，而节点发出的分支名是原样的
    （`branch` 节点的 `then` 由人填、人工核签的 choice 也由人写）。只在存的一侧归一，
    "规则写 then=OK、边条件也写 OK"就会永远对不上——两条下游一起被级联跳过，
    而实例状态仍写着 succeeded（真机量到的正是这一件）。
    """
    return value.strip().lower()


class NodeState(StrEnum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    BYPASSED = "bypassed"
    DEGRADED = "degraded"
    AWAITING_HUMAN = "awaiting_human"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"


TERMINAL_STATES: frozenset[NodeState] = frozenset(
    {NodeState.SUCCEEDED, NodeState.FAILED, NodeState.SKIPPED, NodeState.BYPASSED, NodeState.DEGRADED, NodeState.CANCELLED}
)


class RetryPolicy(BaseModel):
    """max_attempts 为"重试次数"，不含首次执行；0 表示不重试。"""

    model_config = ConfigDict(extra="forbid")

    max_attempts: int = Field(default=1, ge=0, le=5)
    backoff_ms: int = Field(default=200, ge=0, le=60_000)


class NodeDef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,31}$")
    type: str = Field(pattern=r"^[a-z][a-z0-9_]{1,31}$")
    name: str = Field(default="", max_length=64)
    config: dict[str, Any] = Field(default_factory=dict)
    sla_ms: int = Field(default=5_000, ge=100, le=3_600_000)
    timeout_ms: int = Field(default=5_000, ge=100, le=3_600_000)
    on_failure: str = Field(default="retry", pattern=r"^(retry|degrade|escalate|skip|abort)$")
    retry: RetryPolicy = Field(default_factory=RetryPolicy)

    @model_validator(mode="after")
    def _timeout_within_sla(self) -> NodeDef:
        """超时是单次执行的硬上限，必须落在 SLA 预算内：否则 SLA 必然失守。"""
        if self.timeout_ms > self.sla_ms:
            raise ValueError(f"timeout_ms ({self.timeout_ms}) 不得大于 sla_ms ({self.sla_ms})")
        return self


class EdgeDef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,31}$")
    target: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,31}$")
    condition: str = Field(default="", max_length=64)

    @field_validator("condition")
    @classmethod
    def _normalise_condition(cls, value: str) -> str:
        return normalize_branch(value)


class WorkflowDef(BaseModel):
    """工作流定义（模板）。创建后不可变，修订产生新版本。"""

    model_config = ConfigDict(extra="forbid")

    workflow_id: str = Field(pattern=r"^wf_[0-9a-f]{12}$")
    name: str = Field(min_length=1, max_length=64)
    description: str = Field(default="", max_length=512)
    version: int = Field(default=1, ge=1)
    nodes: list[NodeDef] = Field(min_length=1, max_length=MAX_NODES)
    edges: list[EdgeDef] = Field(default_factory=list, max_length=MAX_EDGES)
    created_by: str = Field(default="platform.workflow", max_length=64)
    status: str = Field(default="active", pattern=r"^(active|archived)$")

    @model_validator(mode="after")
    def _graph_is_valid(self) -> WorkflowDef:
        ids = [node.node_id for node in self.nodes]
        if len(ids) != len(set(ids)):
            raise ValueError("节点 ID 重复")

        known = set(ids)
        for edge in self.edges:
            if edge.source not in known:
                raise ValueError(f"边的源节点不存在: {edge.source}")
            if edge.target not in known:
                raise ValueError(f"边的目标节点不存在: {edge.target}")
            if edge.source == edge.target:
                raise ValueError(f"不允许自环: {edge.source}")

        if (cycle := find_cycle(ids, self.edges)) is not None:
            raise ValueError(f"工作流图存在环: {' → '.join(cycle)}")

        return self

    def node(self, node_id: str) -> NodeDef | None:
        return next((n for n in self.nodes if n.node_id == node_id), None)

    def incoming(self, node_id: str) -> list[EdgeDef]:
        return [e for e in self.edges if e.target == node_id]

    def outgoing(self, node_id: str) -> list[EdgeDef]:
        return [e for e in self.edges if e.source == node_id]

    def roots(self) -> list[str]:
        targeted = {edge.target for edge in self.edges}
        return [node.node_id for node in self.nodes if node.node_id not in targeted]

    def signature(self) -> str:
        parts = [f"{node.node_id}:{node.type}" for node in sorted(self.nodes, key=lambda n: n.node_id)]
        links = [f"{edge.source}->{edge.target}:{edge.condition}" for edge in sorted(self.edges, key=lambda e: (e.source, e.target))]
        return "|".join([*parts, *links])


class NodeRun(BaseModel):
    """节点的一次执行记录（运行态）。"""

    model_config = ConfigDict(extra="forbid")

    node_id: str
    type: str
    state: NodeState = NodeState.PENDING
    attempts: int = 0
    started_mono: float | None = None
    ready_mono: float | None = None
    finished_mono: float | None = None
    schedule_latency_ms: float | None = None
    duration_ms: float | None = None
    output: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    notes: list[str] = Field(default_factory=list)


class InstanceStatus(StrEnum):
    RUNNING = "running"
    WAITING = "waiting"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    ABORTED = "aborted"


class WorkflowInstance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    instance_id: str = Field(pattern=r"^wfi_[0-9a-f]{12}$")
    workflow_id: str
    workflow_version: int
    trace_id: str
    status: InstanceStatus = InstanceStatus.RUNNING
    nodes: dict[str, NodeRun]
    payload: dict[str, Any] = Field(default_factory=dict)
    results: dict[str, Any] = Field(default_factory=dict)
    created_at: str
    finished_at: str | None = None
    error: str | None = None

    TERMINAL: ClassVar[frozenset[InstanceStatus]] = frozenset({InstanceStatus.SUCCEEDED, InstanceStatus.FAILED, InstanceStatus.ABORTED})

    def state_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for run in self.nodes.values():
            counts[run.state.value] = counts.get(run.state.value, 0) + 1
        return counts


def find_cycle(node_ids: list[str], edges: list[EdgeDef]) -> list[str] | None:
    """DFS 三色标记找环，返回环路径（便于报错定位）；无环返回 None。"""
    adjacency: dict[str, list[str]] = {node_id: [] for node_id in node_ids}
    for edge in edges:
        adjacency[edge.source].append(edge.target)

    white, grey, black = 0, 1, 2
    color: dict[str, int] = dict.fromkeys(node_ids, white)
    stack: list[str] = []

    def visit(node: str) -> list[str] | None:
        color[node] = grey
        stack.append(node)
        for child in adjacency.get(node, []):
            if color[child] == grey:
                start = stack.index(child)
                return [*stack[start:], child]
            if color[child] == white and (found := visit(child)) is not None:
                return found
        stack.pop()
        color[node] = black
        return None

    for node in node_ids:
        if color[node] == white and (found := visit(node)) is not None:
            return found
    return None


def topological_levels(node_ids: list[str], edges: list[EdgeDef]) -> list[list[str]]:
    """按层返回可并行执行的节点集合（Kahn 算法）。存在环时抛 ValueError。"""
    indegree = dict.fromkeys(node_ids, 0)
    adjacency: dict[str, list[str]] = {node_id: [] for node_id in node_ids}
    for edge in edges:
        adjacency[edge.source].append(edge.target)
        indegree[edge.target] += 1

    frontier = [node for node, degree in indegree.items() if degree == 0]
    levels: list[list[str]] = []
    seen = 0
    while frontier:
        levels.append(sorted(frontier))
        seen += len(frontier)
        nxt: list[str] = []
        for node in frontier:
            for child in adjacency[node]:
                indegree[child] -= 1
                if indegree[child] == 0:
                    nxt.append(child)
        frontier = nxt
    if seen != len(node_ids):
        raise ValueError("工作流图存在环，无法拓扑排序")
    return levels
