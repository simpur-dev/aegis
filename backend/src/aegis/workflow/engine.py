"""柔性工作流引擎：DAG + 状态机 + 事件驱动调度 + 动态改图。

对应考核指标 2 的三条：
- "≥10 类节点自定义配置" → NodeRegistry 16 类节点，config 逐节点校验；
- "常规任务调度响应时延 ≤2s" → 节点就绪到开始执行的 workflow_schedule_ms（事件驱动，无轮询）；
- "异常工况识别与重调度 ≤10s" → 失败/超时被立即分类并按 on_failure 处置，
  workflow_reschedule_ms 记录"首次失败→终态"的实际时长。

柔性三处：定义与实例版本分离（修订模板不影响在途实例）、运行中改参数/插节点/旁路、异常三段处置。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from aegis.config import Settings, get_settings
from aegis.domain.messages import now_iso
from aegis.errors import AegisError
from aegis.observability import instrumentation
from aegis.observability.tracer import Tracer
from aegis.workflow.model import (
    EdgeDef,
    InstanceStatus,
    NodeDef,
    NodeRun,
    NodeState,
    WorkflowDef,
    WorkflowInstance,
    find_cycle,
)
from aegis.workflow.nodes import (
    HumanRequired,
    NodeContext,
    NodeError,
    NodeOutcome,
    NodeRegistry,
    WorkflowServices,
    default_registry,
)
from aegis.workflow.store import WorkflowRepository, new_instance_id

log = logging.getLogger("aegis.workflow")

ACTIVE_SOURCE_STATES = frozenset({NodeState.SUCCEEDED, NodeState.DEGRADED, NodeState.BYPASSED})


class WorkflowValidationError(Exception):
    """定义期校验失败：未知节点类型、配置不合法、图有环等。"""


def _as_config_list(value: Any) -> list[str]:
    """`upstream` 允许写单个节点 id 或列表：画布两种形态都出现过，校验口径必须一致。"""
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    return [str(item) for item in items]


def _as_workflow_error(exc: ValidationError) -> WorkflowValidationError:
    """把 pydantic 的模型校验异常收敛为单条 WorkflowValidationError 信息。

    图级不变量（重复 ID、悬空边、自环、环）在 WorkflowDef 的 model_validator 里抛 ValueError，
    经 pydantic 包装后是 ValidationError；API 层只识别 WorkflowValidationError（映射 400），
    不能让 ValidationError 直接外泄。
    """
    errors = exc.errors()
    reason = str(errors[0].get("msg")) if errors else str(exc)
    return WorkflowValidationError(reason)


@dataclass(slots=True)
class _MutableInstance:
    """运行态实例：节点定义快照 + 执行状态。快照保证模板修订不影响在途实例。"""

    instance_id: str
    definition: WorkflowDef
    trace_id: str
    payload: dict[str, Any]
    nodes: dict[str, NodeDef]
    edges: list[EdgeDef]
    runs: dict[str, NodeRun]
    status: InstanceStatus = InstanceStatus.RUNNING
    error: str | None = None
    created_mono: float = field(default_factory=time.perf_counter)
    decisions: dict[str, dict[str, Any]] = field(default_factory=dict)
    pending_waits: dict[str, str] = field(default_factory=dict)
    # 等待人工决策节点可选的取值（来自节点抛出的 HumanRequired.options），供 resume 前置校验。
    pending_options: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def incoming(self, node_id: str) -> list[EdgeDef]:
        return [edge for edge in self.edges if edge.target == node_id]

    def outgoing(self, node_id: str) -> list[EdgeDef]:
        return [edge for edge in self.edges if edge.source == node_id]

    def snapshot(self) -> WorkflowInstance:
        return WorkflowInstance(
            instance_id=self.instance_id,
            workflow_id=self.definition.workflow_id,
            workflow_version=self.definition.version,
            trace_id=self.trace_id,
            status=self.status,
            nodes={node_id: run.model_copy(deep=True) for node_id, run in self.runs.items()},
            payload=dict(self.payload),
            results={node_id: run.output for node_id, run in self.runs.items() if run.state in (NodeState.SUCCEEDED, NodeState.DEGRADED)},
            created_at=now_iso(),
            error=self.error,
        )


class WorkflowEngine:
    def __init__(
        self,
        *,
        repository: WorkflowRepository | None = None,
        registry: NodeRegistry | None = None,
        services: WorkflowServices | None = None,
        tracer: Tracer | None = None,
        settings: Settings | None = None,
        max_parallel: int = 32,
    ) -> None:
        if max_parallel < 1:
            raise ValueError("max_parallel 必须 ≥ 1")
        # 注意：WorkflowRepository / NodeRegistry 定义了 __len__，空实例是假值，
        # 用 `x or default` 会把调用方传入的空仓储/注册表静默替换掉（容量/测试注入失效），
        # 因此这里统一按 `is None` 判断是否使用默认值。
        self._repository = repository if repository is not None else WorkflowRepository()
        self._registry = registry if registry is not None else default_registry()
        self._services = services if services is not None else WorkflowServices()
        self._tracer = tracer if tracer is not None else Tracer()
        self._settings = settings if settings is not None else get_settings()
        self._semaphore = asyncio.Semaphore(max_parallel)
        self._instances: dict[str, _MutableInstance] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    # ---------- 定义管理 ----------

    @property
    def repository(self) -> WorkflowRepository:
        return self._repository

    @property
    def node_types(self) -> list[str]:
        return self._registry.names()

    @property
    def node_registry(self) -> NodeRegistry:
        """画布节点面板需要读取节点元数据，提供只读访问而非私有属性。"""
        return self._registry

    @property
    def services(self) -> WorkflowServices:
        """装配给节点的外部能力：状态面与用例据此核对"某类节点在这套装配里到底能不能跑"。

        这条腿此前只能从 `_services` 猜——审计就是靠它发现 `http_call` 从未被装配。
        """
        return self._services

    def validate_definition(self, definition: WorkflowDef) -> None:
        """静态校验：节点类型已注册、配置合法、图无环、边引用存在。

        定义期错误统一为 WorkflowValidationError，便于 API 层映射为 400。
        """
        for node in definition.nodes:
            try:
                spec = self._registry.require(node.type)
                spec.validate_config(node.config)
            except AegisError as exc:
                raise WorkflowValidationError(exc.message) from exc
            self._validate_wired_upstreams(definition, node)

    @staticmethod
    def _validate_wired_upstreams(definition: WorkflowDef, node: NodeDef) -> None:
        """配置里点名的 `upstream` 必须是这个节点的入边——画布上没连线就不算上游。

        运行时 `ctx.inputs` 只带直接入边的结果。少了这道检查时，"join 指了一个没连线的节点"
        会在实例跑到那一步时才炸成节点失败（现场表现为"流程莫名卡住"），而这是定义期
        就能判定的错误：API 层能直接回 400，编排的人当场就知道要补那条线。
        """
        wired = {edge.source for edge in definition.incoming(node.node_id)}
        for value in _as_config_list(node.config.get("upstream")):
            if value not in wired:
                raise WorkflowValidationError(
                    f"节点 {node.node_id} 的 upstream={value!r} 不是它的入边"
                    f"（画布上缺少 {value} → {node.node_id} 这条连线；当前入边：{sorted(wired) or '无'}）"
                )

    async def create_definition(
        self,
        *,
        name: str,
        nodes: list[dict[str, Any]],
        edges: list[dict[str, Any]],
        description: str = "",
        version: int | None = None,
    ) -> WorkflowDef:
        from aegis.workflow.store import build_definition, new_workflow_id

        try:
            definition = build_definition(
                name=name,
                nodes=nodes,
                edges=edges,
                description=description,
                workflow_id=new_workflow_id(),
                version=version or self._repository.next_version(name),
            )
        except ValidationError as exc:
            raise _as_workflow_error(exc) from exc
        self.validate_definition(definition)
        return await self._repository.save(definition)

    async def revise_definition(
        self,
        workflow_id: str,
        *,
        nodes: list[dict[str, Any]] | None = None,
        edges: list[dict[str, Any]] | None = None,
        description: str | None = None,
    ) -> WorkflowDef:
        """修订产生新版本；在途实例仍绑定旧版本快照。"""
        base = self._repository.get(workflow_id)
        if base is None:
            raise WorkflowValidationError(f"工作流定义不存在: {workflow_id}")
        from aegis.workflow.store import build_definition

        try:
            revised = build_definition(
                name=base.name,
                nodes=nodes if nodes is not None else [node.model_dump() for node in base.nodes],
                edges=edges if edges is not None else [edge.model_dump() for edge in base.edges],
                description=base.description if description is None else description,
                version=self._repository.next_version(base.name),
            )
        except ValidationError as exc:
            raise _as_workflow_error(exc) from exc
        self.validate_definition(revised)
        return await self._repository.save(revised)

    async def archive(self, workflow_id: str) -> WorkflowDef:
        from aegis.workflow.store import build_definition

        base = self._repository.get(workflow_id)
        if base is None:
            raise WorkflowValidationError(f"工作流定义不存在: {workflow_id}")
        archived = build_definition(
            name=base.name,
            nodes=[node.model_dump() for node in base.nodes],
            edges=[edge.model_dump() for edge in base.edges],
            description=base.description,
            workflow_id=base.workflow_id,
            version=base.version,
        )
        archived.status = "archived"
        return await self._repository.save(archived)

    def latest_definition(self, name: str) -> WorkflowDef | None:
        return self._repository.latest(name)

    def definition_by_id(self, workflow_id: str) -> WorkflowDef | None:
        """按 workflow_id 取一份完整定义（含 nodes/edges）。

        存在的理由：`GET /definitions` 只报计数，画布要把一份已存的定义**重新打开来编辑**
        时拿不到节点与连线——于是这一页能存、能归档，却打不开自己存过的东西。
        归档态也返回：只读查看旧版本是正当需求，是不是要编辑由界面决定，不在这里拦。
        """
        return self._repository.get(workflow_id)

    # ---------- 实例执行 ----------

    def instance(self, instance_id: str) -> WorkflowInstance | None:
        current = self._instances.get(instance_id)
        return current.snapshot() if current else None

    def instance_detail(self, instance_id: str) -> dict[str, Any] | None:
        current = self._instances.get(instance_id)
        if current is None:
            return None
        return {
            "instance_id": current.instance_id,
            "workflow_id": current.definition.workflow_id,
            "workflow_version": current.definition.version,
            "trace_id": current.trace_id,
            "status": current.status.value,
            "error": current.error,
            # 核签工单要能让人看见"签的是什么"：上报文本、定级来源与转核签的原因都在 payload 里，
            # 不带上就等于把决策要依据的事实留在内存里不外显。
            "payload": current.payload,
            "nodes": [
                {
                    "node_id": node_id,
                    "type": run.type,
                    "state": run.state.value,
                    "attempts": run.attempts,
                    "schedule_latency_ms": run.schedule_latency_ms,
                    "duration_ms": run.duration_ms,
                    "output": run.output,
                    "error": run.error,
                    "notes": run.notes,
                }
                for node_id, run in current.runs.items()
            ],
        }

    async def start(
        self,
        workflow_id: str,
        *,
        trace_id: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        definition = self._repository.get(workflow_id)
        if definition is None:
            raise WorkflowValidationError(f"工作流定义不存在: {workflow_id}")
        instance = self._instantiate(definition, trace_id=trace_id, payload=payload or {})
        await self._drive(instance)
        return self.instance_detail(instance.instance_id) or {}

    def _instantiate(self, definition: WorkflowDef, *, trace_id: str, payload: dict[str, Any]) -> _MutableInstance:
        instance = _MutableInstance(
            instance_id=new_instance_id(),
            definition=definition,
            trace_id=trace_id,
            payload=dict(payload),
            nodes={node.node_id: node.model_copy(deep=True) for node in definition.nodes},
            edges=[edge.model_copy(deep=True) for edge in definition.edges],
            runs={node.node_id: NodeRun(node_id=node.node_id, type=node.type) for node in definition.nodes},
        )
        self._instances[instance.instance_id] = instance
        self._locks.setdefault(instance.instance_id, asyncio.Lock())
        return instance

    async def _drive(self, instance: _MutableInstance) -> None:
        """主调度循环：每轮找出可执行节点并发执行，直到无可执行节点。

        事件驱动、无轮询等待：一轮 gather 只在"确有就绪节点"时挂起，就绪即执行。
        一旦实例进入 FAILED（abort 策略）或 WAITING（等待人工决策），立即停止调度，
        未触及的下游节点保持 pending —— 不得因为"暂时不可就绪"而被误判为级联跳过。

        链路结构：一次驱动 = 一条 `workflow_instance` 跨度，其 traceID 由实例的契约 trace_id
        确定性映射而来，本轮就绪执行的节点跨度自动成为它的子节点（见 observability/instrumentation.py）。
        人工决策后续跑会再产生一条同 traceID 的驱动跨度——这恰好是"等待人工"那一段的真实证据。
        """
        async with instrumentation.span(
            "workflow_instance",
            trace_id=instance.trace_id,
            kind="server",
            span_attrs={
                "aegis.workflow.id": instance.definition.workflow_id,
                "aegis.workflow.version": instance.definition.version,
                "aegis.workflow.instance_id": instance.instance_id,
                "aegis.workflow.node_count": len(instance.nodes),
            },
        ) as instance_span:
            while True:
                if instance.status in (InstanceStatus.FAILED, InstanceStatus.WAITING):
                    break
                ready = self._collect_ready(instance)
                if not ready:
                    break
                await asyncio.gather(*(self._execute_node(instance, node_id) for node_id in ready))
            self._finalise(instance)
            instrumentation.set_attributes(instance_span, span_attrs={"aegis.workflow.status": instance.status.value})
            if instance.status is InstanceStatus.FAILED:
                instrumentation.mark_error(instance_span, instance.error or "实例失败", code="workflow_failed")
            elif instance.status is InstanceStatus.WAITING:
                instrumentation.set_attributes(instance_span, span_attrs={"aegis.workflow.awaiting_human": True})

    def _collect_ready(self, instance: _MutableInstance) -> list[str]:
        ready: list[str] = []
        for node_id, run in instance.runs.items():
            if run.state not in (NodeState.PENDING, NodeState.READY):
                continue
            incoming = instance.incoming(node_id)
            if not incoming:
                run.state = NodeState.READY
                run.ready_mono = time.perf_counter()
                ready.append(node_id)
                continue

            upstream_states = [instance.runs[edge.source].state for edge in incoming]
            if any(state in (NodeState.PENDING, NodeState.READY, NodeState.RUNNING, NodeState.AWAITING_HUMAN) for state in upstream_states):
                continue  # 仍有上游未终态（含等待人工决策，此时下游必须等待而非被跳过）

            active = any(
                instance.runs[edge.source].state in ACTIVE_SOURCE_STATES and self._edge_active(instance, edge) for edge in incoming
            )
            if not active:
                # 所有入边都来自未命中分支或失败旁路：本节点级联跳过
                run.state = NodeState.SKIPPED
                run.notes.append("上游分支未命中，级联跳过")
                continue
            if all(
                state in (NodeState.SUCCEEDED, NodeState.DEGRADED, NodeState.BYPASSED, NodeState.SKIPPED, NodeState.FAILED)
                for state in upstream_states
            ):
                run.state = NodeState.READY
                run.ready_mono = time.perf_counter()
                ready.append(node_id)
        return ready

    @staticmethod
    def _edge_active(instance: _MutableInstance, edge: EdgeDef) -> bool:
        source_run = instance.runs[edge.source]
        if source_run.state not in ACTIVE_SOURCE_STATES:
            return False
        if not edge.condition:
            return True
        return str(source_run.output.get("branch", "")) == edge.condition

    async def _execute_node(self, instance: _MutableInstance, node_id: str) -> None:
        node = instance.nodes[node_id]
        run = instance.runs[node_id]
        spec = self._registry.require(node.type)

        run.schedule_latency_ms = (time.perf_counter() - (run.ready_mono or time.perf_counter())) * 1000
        self._tracer.record(
            "workflow_schedule_ms",
            max(run.schedule_latency_ms, 0.0),
            trace_id=instance.trace_id,
        )

        # 节点跨度在"调度时延已落账"之后创建：建跨度的开销不会算进 ≤2s 的 workflow_schedule_ms 样本，
        # 跨度只承载结构证据（node_id / attempt / 终态），SLA 判定仍以时延账本为准。
        node_span = instrumentation.start_span(
            "workflow_node",
            trace_id=instance.trace_id,
            kind="internal",
            span_attrs={
                "aegis.workflow.instance_id": instance.instance_id,
                "aegis.node.id": node_id,
                "aegis.node.type": node.type,
                "aegis.node.timeout_ms": node.timeout_ms,
                "aegis.node.on_failure": node.on_failure,
                "aegis.workflow.schedule_ms": round(max(run.schedule_latency_ms, 0.0), 3),
            },
        )

        run.state = NodeState.RUNNING
        run.started_mono = time.perf_counter()
        first_failure_mono: float | None = None

        while True:
            run.attempts += 1
            instrumentation.set_attributes(node_span, span_attrs={"aegis.node.attempt": run.attempts})
            context = NodeContext(
                payload=dict(instance.payload),
                inputs={
                    edge.source: instance.runs[edge.source].output
                    for edge in instance.incoming(node_id)
                    if instance.runs[edge.source].state in ACTIVE_SOURCE_STATES
                },
                all_outputs={
                    other_id: other.output
                    for other_id, other in instance.runs.items()
                    if other.state in ACTIVE_SOURCE_STATES and other.output
                },
                services=self._services,
                attempt=run.attempts,
                decision=instance.decisions.get(node_id),
            )
            try:
                async with self._semaphore:
                    outcome: NodeOutcome = await asyncio.wait_for(spec.handler(context, node.config), timeout=node.timeout_ms / 1000)
            except HumanRequired as wait:
                run.state = NodeState.AWAITING_HUMAN
                run.notes.append(f"等待人工决策: {wait.prompt}")
                instance.status = InstanceStatus.WAITING
                instance.pending_waits[node_id] = wait.prompt
                instance.pending_options[node_id] = wait.options
                self._record_node_latency(instance, run, node_id)
                # 等人不等于失败：状态留空（中性），只把等待事实与尝试次数落到跨度上
                instrumentation.set_attributes(
                    node_span,
                    span_attrs={
                        "aegis.node.attempt": run.attempts,
                        "aegis.node.state": run.state.value,
                        "aegis.node.awaiting_human": True,
                    },
                )
                instrumentation.add_event(node_span, "aegis.node.awaiting_human", prompt=str(wait.prompt)[:300])
                instrumentation.end_span(node_span)
                return
            except TimeoutError:
                error: Exception = NodeError(f"节点超时 (>{node.timeout_ms}ms)", detail={"node_id": node_id}, retryable=True)
            except AegisError as exc:
                # 类型化错误（含 NodeError/NodeConfigError）自带可读原因，不许被压成"节点执行异常: 类名"。
                # 外呼被拒就是"主机不在白名单：x"——留在 error 里运维才知道该改哪一格配置。
                error = exc
            except ValidationError as exc:  # handler 内对上游载荷的模型校验失败
                errors = exc.errors()
                first_type = errors[0].get("type", "validation") if errors else "validation"
                first_loc = errors[0].get("loc", []) if errors else []
                error = NodeError(
                    f"节点入参不合法: {first_type} @{','.join(map(str, first_loc))}",
                    detail={"node_id": node_id, "errors": errors[:3]},
                )
            except Exception as exc:  # 未预期异常：归一为节点错误，避免整条链路崩
                error = NodeError(f"节点执行异常: {exc.__class__.__name__}", detail={"reason": str(exc)[:200]})
            else:
                run.output = dict(outcome.output)
                if outcome.branch:
                    run.output.setdefault("branch", outcome.branch)
                if outcome.note:
                    run.notes.append(outcome.note)
                run.state = NodeState.SUCCEEDED
                run.error = None
                self._record_node_latency(instance, run, node_id, first_failure_mono)
                instrumentation.set_attributes(
                    node_span,
                    span_attrs={
                        "aegis.node.attempt": run.attempts,
                        "aegis.node.state": run.state.value,
                        "aegis.node.duration_ms": round(run.duration_ms or 0.0, 3),
                    },
                )
                instrumentation.mark_ok(node_span)
                instrumentation.end_span(node_span)
                return

            first_failure_mono = first_failure_mono or time.perf_counter()
            run.error = getattr(error, "message", str(error))
            run.notes.append(f"第 {run.attempts} 次失败: {run.error}")
            retryable = getattr(error, "retryable", True)
            # 单次尝试失败只记事件：终态才决定跨度状态（重试后成功不该把节点染成故障）
            instrumentation.add_event(
                node_span, "aegis.node.attempt_failed", attempt=run.attempts, error=str(run.error)[:300], retryable=retryable
            )

            if node.on_failure == "retry" and retryable and run.attempts <= node.retry.max_attempts:
                await asyncio.sleep(node.retry.backoff_ms / 1000)
                continue

            handled = self._apply_failure_policy(instance, node_id, node, run, error)
            if handled == "continue":
                continue
            if handled == "degraded":
                run.state = NodeState.DEGRADED
            self._record_node_latency(instance, run, node_id, first_failure_mono)
            instrumentation.set_attributes(
                node_span,
                span_attrs={
                    "aegis.node.attempt": run.attempts,
                    "aegis.node.state": run.state.value,
                    "aegis.node.duration_ms": round(run.duration_ms or 0.0, 3),
                    "aegis.node.rescheduled": first_failure_mono is not None,
                },
            )
            if run.state is NodeState.FAILED:
                instrumentation.record_error(node_span, error)
            elif run.state in (NodeState.DEGRADED, NodeState.SKIPPED):
                # 降级/级联跳过：链路仍按策略继续，不按故障着色（与账本 outcome 口径一致）
                instrumentation.mark_degraded(node_span, str(run.error or "节点按策略降级/跳过"))
            instrumentation.end_span(node_span)
            return

    def _apply_failure_policy(
        self,
        instance: _MutableInstance,
        node_id: str,
        node: NodeDef,
        run: NodeRun,
        error: Exception,
    ) -> str:
        policy = node.on_failure
        if policy == "skip":
            run.state = NodeState.SKIPPED
            run.notes.append("按 skip 策略跳过")
            return "stop"
        if policy == "abort":
            run.state = NodeState.FAILED
            instance.status = InstanceStatus.FAILED
            instance.error = f"节点 {node_id} 失败触发中止: {getattr(error, 'message', error)}"
            return "stop"
        if policy == "escalate":
            run.state = NodeState.AWAITING_HUMAN
            instance.status = InstanceStatus.WAITING
            instance.pending_waits[node_id] = f"节点 {node_id} 失败，需人工接管"
            return "stop"
        if policy == "degrade":
            run.state = NodeState.DEGRADED
            run.output = {"degraded": True, "reason": getattr(error, "message", str(error))}
            run.notes.append("按 degrade 策略以降级值继续")
            return "stop"
        # retry 用尽
        run.state = NodeState.FAILED
        return "stop"

    def _record_node_latency(
        self,
        instance: _MutableInstance,
        run: NodeRun,
        node_id: str,
        first_failure_mono: float | None = None,
    ) -> None:
        run.finished_mono = time.perf_counter()
        if run.started_mono is not None:
            run.duration_ms = (run.finished_mono - run.started_mono) * 1000
            self._tracer.record("workflow_node_ms", run.duration_ms, trace_id=instance.trace_id)
        if first_failure_mono is not None:
            reschedule_ms = (run.finished_mono - first_failure_mono) * 1000
            self._tracer.record("workflow_reschedule_ms", reschedule_ms, trace_id=instance.trace_id)
        log.debug(
            "节点完成",
            extra={"instance_id": instance.instance_id, "node_id": node_id, "state": run.state.value},
        )

    def _finalise(self, instance: _MutableInstance) -> None:
        if instance.status in (InstanceStatus.WAITING, InstanceStatus.FAILED):
            return
        states = {run.state for run in instance.runs.values()}
        if states & {NodeState.PENDING, NodeState.READY, NodeState.RUNNING}:
            instance.status = InstanceStatus.RUNNING
            return
        if NodeState.FAILED in states:
            instance.status = InstanceStatus.FAILED
            instance.error = instance.error or "存在失败节点"
        else:
            instance.status = InstanceStatus.SUCCEEDED
        self._tracer.record(
            "workflow_instance_ms",
            (time.perf_counter() - instance.created_mono) * 1000,
            trace_id=instance.trace_id,
        )

    # ---------- 运行中柔性操作 ----------

    async def resume(self, instance_id: str, *, node_id: str, decision: dict[str, Any]) -> dict[str, Any]:
        instance = self._require_instance(instance_id)
        if node_id not in instance.runs:
            raise WorkflowValidationError(f"实例中不存在节点: {node_id}")
        run = instance.runs[node_id]
        if run.state is not NodeState.AWAITING_HUMAN:
            raise WorkflowValidationError(f"节点 {node_id} 未在等待人工决策（当前 {run.state.value}）")
        # 决策值前置校验：非法/未知选项属于调用方错误（400），不应进入节点执行再被判为可重试失败。
        options = instance.pending_options.get(node_id)
        if options is not None:
            choice = str(decision.get("choice", "")).lower()
            if choice not in options:
                raise WorkflowValidationError(f"人工决策值非法: {choice!r}，允许 {list(options)}")
        instance.decisions[node_id] = decision
        instance.pending_waits.pop(node_id, None)
        instance.pending_options.pop(node_id, None)
        run.state = NodeState.READY
        run.ready_mono = time.perf_counter()
        run.attempts = 0
        instance.status = InstanceStatus.RUNNING
        await self._drive(instance)
        return self.instance_detail(instance_id) or {}

    # 下面两个"在途实例改图"的口子是本引擎的立项理由，也是 ADR-0004 里唯一的外部对照对象：
    # Conductor OSS 以 `PUT /api/workflow/{workflowId}/skiptask/{taskReferenceName}`（官方 Workflow API 页）
    # 与其 SDK 的 `SkipTaskFromWorkflow` 操作做到同类效果，但它要 Java server + PG + Redis 三件套中心服务，
    # 直接破坏边缘自治，所以只借它的 API 形状、不引它的运行时。出处与核对日期见 docs/adr/0004。
    async def update_node_config(self, instance_id: str, node_id: str, config: dict[str, Any]) -> dict[str, Any]:
        """在途实例改节点参数：仅允许未执行的节点，且必须通过该节点类型的配置校验。"""
        instance = self._require_instance(instance_id)
        node = instance.nodes.get(node_id)
        if node is None:
            raise WorkflowValidationError(f"实例中不存在节点: {node_id}")
        if instance.runs[node_id].state not in (NodeState.PENDING, NodeState.READY):
            raise WorkflowValidationError(f"节点 {node_id} 已开始执行，不可改参")
        merged = {**node.config, **config}
        self._registry.require(node.type).validate_config(merged)
        node.config = merged
        instance.runs[node_id].notes.append(f"运行中改参: {sorted(config)}")
        return {"node_id": node_id, "config": merged}

    async def insert_node(self, instance_id: str, *, after: str, node: dict[str, Any]) -> dict[str, Any]:
        """在途实例插入节点：把 after 的原出边重接到新节点，保证图仍无环。"""
        instance = self._require_instance(instance_id)
        if after not in instance.nodes:
            raise WorkflowValidationError(f"锚点节点不存在: {after}")
        node_def = NodeDef.model_validate(node)
        if node_def.node_id in instance.nodes:
            raise WorkflowValidationError(f"节点 ID 已存在: {node_def.node_id}")
        spec = self._registry.require(node_def.type)
        spec.validate_config(node_def.config)

        moved = [edge for edge in instance.edges if edge.source == after]
        instance.edges = [edge for edge in instance.edges if edge.source != after]
        instance.nodes[node_def.node_id] = node_def
        instance.runs[node_def.node_id] = NodeRun(node_id=node_def.node_id, type=node_def.type)
        for source in {edge.source for edge in moved} | {after}:
            instance.edges.append(EdgeDef(source=source, target=node_def.node_id, condition=""))
        for edge in moved:
            instance.edges.append(EdgeDef(source=node_def.node_id, target=edge.target, condition=edge.condition))

        if find_cycle(list(instance.nodes), instance.edges) is not None:
            raise WorkflowValidationError("插入节点后出现环，已拒绝")
        return {"instance_id": instance_id, "node_id": node_def.node_id, "after": after}

    async def bypass_node(self, instance_id: str, node_id: str, *, reason: str = "") -> dict[str, Any]:
        """旁路节点：未执行/等待中的节点被人工放行，其下游按已满足继续。

        用独立终态 BYPASSED 而非 SKIPPED：SKIPPED 表示"分支未命中被级联跳过"，
        其下游同样要跳过；BYPASSED 表示"人工放行、放行下游继续"，二者不可混用，
        否则旁路一个等待人工的节点会把整条下游误判为跳过。
        """
        instance = self._require_instance(instance_id)
        run = instance.runs.get(node_id)
        if run is None:
            raise WorkflowValidationError(f"实例中不存在节点: {node_id}")
        if run.state not in (NodeState.PENDING, NodeState.READY, NodeState.AWAITING_HUMAN):
            raise WorkflowValidationError(f"节点 {node_id} 状态 {run.state.value} 不可旁路")
        run.state = NodeState.BYPASSED
        run.notes.append(f"人工旁路: {reason}" if reason else "人工旁路")
        instance.pending_waits.pop(node_id, None)
        instance.pending_options.pop(node_id, None)
        # 仅当本节点是唯一的挂起原因时，旁路才把 WAITING 解除为 RUNNING；终态实例（失败/中止/成功）不复活。
        if instance.status is InstanceStatus.WAITING and not instance.pending_waits:
            instance.status = InstanceStatus.RUNNING
        if instance.status is InstanceStatus.RUNNING:
            await self._drive(instance)
        return self.instance_detail(instance_id) or {}

    async def abort(self, instance_id: str, *, reason: str = "") -> dict[str, Any]:
        instance = self._require_instance(instance_id)
        # 幂等：已中止的实例再次中止不重复处置、不重复上报时延指标。
        if instance.status is InstanceStatus.ABORTED:
            return self.instance_detail(instance_id) or {}
        for run in instance.runs.values():
            if run.state in (NodeState.PENDING, NodeState.READY, NodeState.AWAITING_HUMAN):
                run.state = NodeState.CANCELLED
        instance.status = InstanceStatus.ABORTED
        instance.error = reason or "人工中止"
        instance.pending_waits.clear()
        instance.pending_options.clear()
        self._tracer.record("workflow_instance_ms", (time.perf_counter() - instance.created_mono) * 1000, trace_id=instance.trace_id)
        return self.instance_detail(instance_id) or {}

    def _require_instance(self, instance_id: str) -> _MutableInstance:
        instance = self._instances.get(instance_id)
        if instance is None:
            raise WorkflowValidationError(f"实例不存在: {instance_id}")
        return instance

    async def start_from_name(self, name: str, *, trace_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        definition = self._repository.latest(name)
        if definition is None:
            raise WorkflowValidationError(f"未找到工作流模板: {name}")
        return await self.start(definition.workflow_id, trace_id=trace_id, payload=payload)

    def instances(self) -> list[dict[str, Any]]:
        return [detail for detail in (self.instance_detail(key) for key in self._instances) if detail]
