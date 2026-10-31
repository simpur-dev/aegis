"""柔性工作流引擎：DAG + 状态机 + 事件驱动调度 + 动态改图。

对应考核指标 2 的三条：
- "≥10 类节点自定义配置" → NodeRegistry 19 类节点（含 1 类内部原语，面板可拖 18 类），config 逐节点校验；
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
from datetime import UTC, datetime
from typing import Any

import orjson
from pydantic import ValidationError

from aegis.config import Settings, get_settings
from aegis.domain.messages import new_trace_id, now_iso, parse_iso
from aegis.errors import AegisError
from aegis.observability import instrumentation
from aegis.observability.tracer import Tracer
from aegis.workflow.documents import Position, WorkflowDocument, definition_from_document, document_from_definition
from aegis.workflow.errors import WorkflowAppError, WorkflowErrorCode
from aegis.workflow.model import (
    TERMINAL_STATES,
    EdgeDef,
    InstanceStatus,
    NodeDef,
    NodeRun,
    NodeState,
    WorkflowDef,
    WorkflowInstance,
    find_cycle,
    normalize_branch,
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
from aegis.workflow.persistence import InstanceCheckpoint, WorkflowPersistencePort, WorkflowRuntime
from aegis.workflow.preview import (
    MAX_PREVIEW_PAYLOAD_BYTES,
    InMemoryPreviewStore,
    PreviewLimitReached,
    dry_run_services,
    new_preview_id,
)
from aegis.workflow.store import DuplicateWorkflowName, WorkflowRepository, new_instance_id

log = logging.getLogger("aegis.workflow")

ACTIVE_SOURCE_STATES = frozenset({NodeState.SUCCEEDED, NodeState.DEGRADED, NodeState.BYPASSED})
TERMINAL_STATUSES = frozenset({InstanceStatus.SUCCEEDED, InstanceStatus.FAILED, InstanceStatus.ABORTED})
#: 启动恢复一次最多载入多少条未终态实例。开放实例是"真工单"，不该被驻留上界挤掉；
#: 这个数只防病态数据把启动拖死，命中时日志里会留下一条 warning。
OPEN_INSTANCE_RECOVERY_LIMIT = 1_000


class WorkflowValidationError(Exception):
    """定义期校验失败：未知节点类型、配置不合法、图有环等。"""


class WorkflowConflictError(WorkflowValidationError):
    """定义期冲突（重名/版本被占）：旧端点据此回 409 而不是 400。"""


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


def _elapsed_seconds_since(created_at: str) -> float:
    """计算实例已存活多久：恢复后的时长指标按真实墙钟算，而不是从本次进程启动开始。"""
    try:
        return max((datetime.now(UTC) - parse_iso(created_at)).total_seconds(), 0.0)
    except (TypeError, ValueError):
        return 0.0


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
    #: 这张图属于哪条 lineage（运行历史按 lineage 查，实例详情要自报家门）。
    lineage_id: str = ""
    created_at: str = field(default_factory=now_iso)
    finished_at: str | None = None
    #: 试运行实例：不进 `_instances`、不记指标账本、外部动作只给回执。
    dry_run: bool = False
    #: 试运行结果的取件号（`pvp_…`）；live 实例为 None。
    preview_id: str | None = None
    #: 画布位置快照（启动时取版本 layout，运行中插节点就地补一条）。
    positions: dict[str, dict[str, float]] = field(default_factory=dict)
    #: 正在执行的节点 handler：中止时逐个取消，避免"停了状态、停不住动作"。
    running_tasks: dict[str, asyncio.Future[Any]] = field(default_factory=dict, repr=False)
    #: 本次进程从持久层恢复出来的现场（读取面据此区分"一直在跑"与"重启后接上"）。
    recovered: bool = False
    #: 恢复时仍在 running、无法续跑的实例：被显式标成 FAILED 并留原因，不静默消失。
    interrupted: bool = False

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
            created_at=self.created_at,
            finished_at=self.finished_at,
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
        max_instances: int = 128,
        preview_store: InMemoryPreviewStore | None = None,
        persistence: WorkflowPersistencePort | None = None,
    ) -> None:
        if max_parallel < 1:
            raise ValueError("max_parallel 必须 ≥ 1")
        if max_instances < 8:
            raise ValueError("max_instances 不得小于 8：留底太少会把还在被人看的实例也挤掉")
        # 注意：WorkflowRepository / NodeRegistry 定义了 __len__，空实例是假值，
        # 用 `x or default` 会把调用方传入的空仓储/注册表静默替换掉（容量/测试注入失效），
        # 因此这里统一按 `is None` 判断是否使用默认值。
        self._persistence = persistence
        self._repository = repository if repository is not None else WorkflowRepository(persistence=persistence)
        self._registry = registry if registry is not None else default_registry()
        # 启动对账：目录是面板、变量选择器与发布校验的共同真源，坏一行就整表拒启。
        # 放在构造期而不是首次请求期——启动日志里第一条事实就该是"目录对上了"。
        self._registry.assert_consistent()
        self._services = services if services is not None else WorkflowServices()
        self._tracer = tracer if tracer is not None else Tracer()
        self._settings = settings if settings is not None else get_settings()
        self._semaphore = asyncio.Semaphore(max_parallel)
        self._instances: dict[str, _MutableInstance] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        #: 实例的**热态**在内存（调度循环要同步读），落库由 `_checkpoint` 写穿；
        #: 仍需上界：D7 之后每条预警都会起一个实例，终态实例不能无限累积。
        self._max_instances = int(max_instances)
        self.evicted_instances = 0
        #: 启动恢复的记账：恢复了几条定义、接回几条实例、其中多少条只能标中断。
        self.recovery_stats = {"definitions": 0, "instances": 0, "interrupted": 0}
        #: 试运行与正式实例分两本账：预览不进 `_instances`，也不参与淘汰/计数。
        self._previews = preview_store if preview_store is not None else InMemoryPreviewStore()
        self._dry_services = dry_run_services(self._services)

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

    @property
    def preview_store(self) -> InMemoryPreviewStore:
        """试运行结果表（只读用途：容量/在册数）。"""
        return self._previews

    @property
    def instance_stats(self) -> dict[str, int]:
        """在内存里留着几个实例、上界多大、累计被挤掉几个，以及启动恢复了多少。

        这些数只有摆在一起才读得懂：`evicted_instances` 一直在涨说明容量小了，
        而单独看它不知道上界在哪；`recovered_instances/interrupted_instances`
        说明当前账本里有多少是重启接回来的、其中多少只能收成失败。
        """
        return {
            "held": len(self._instances),
            "capacity": self._max_instances,
            "evicted": self.evicted_instances,
            "recovered_definitions": self.recovery_stats["definitions"],
            "recovered_instances": self.recovery_stats["instances"],
            "interrupted_instances": self.recovery_stats["interrupted"],
        }

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
        try:
            document = document_from_definition(definition)
        except ValidationError as exc:
            # 文档层新增了 Start/End 图不变量：注册 start/end 后，旧端点也可能送来
            # 违反它们的图。这里不收敛的话，Pydantic 异常会冒成 500——明明是定义错了。
            raise _as_workflow_error(exc) from exc
        try:
            if version is not None:
                return await self._repository.save(definition)
            # 版本号在仓储锁内从整条 lineage 取 max+1：旧端点的"建定义"与"修订"
            # 此前在锁外读 next_version，并发两路会撞同一个号并冒成 500。
            # 同名再建一份仍按旧语义追加新版本，所以走按名归族的 save_next_named。
            return await self._repository.save_next_named(definition, document=document)
        except DuplicateWorkflowName as exc:
            raise WorkflowConflictError(str(exc)) from exc

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
                # save_next 会在锁内改写版本号；这里只需要一个合法占位值。
                version=base.version + 1,
                lineage_id=base.lineage_id,
            )
        except ValidationError as exc:
            raise _as_workflow_error(exc) from exc
        self.validate_definition(revised)
        try:
            document = document_from_definition(revised)
        except ValidationError as exc:
            raise _as_workflow_error(exc) from exc
        try:
            return await self._repository.save_next(revised, document=document)
        except DuplicateWorkflowName as exc:
            raise WorkflowConflictError(str(exc)) from exc

    async def archive(self, workflow_id: str) -> WorkflowDef:
        base = self._repository.get(workflow_id)
        if base is None:
            raise WorkflowValidationError(f"工作流定义不存在: {workflow_id}")
        # 归档不改内容、不产生新版本：重建对象会把 created_at 改成"归档时刻"，版本列表里的
        # 创建时间就成了一句随操作变化的话。只翻转状态，其余字段原样保留。
        return await self._repository.save(base.model_copy(update={"status": "archived"}))

    async def restore(self, workflow_id: str) -> WorkflowDef:
        """取消归档：与 `archive()` 对称的撤销动作，同样不产生新版本、不动历史。

        只允许恢复"同名的最新版"。按名字取用剧本的路径（核签开单、`start_from_name`）
        走的都是 `latest(name)`，把一份旧版本签回启用中会让人以为恢复好了、实际仍指向
        那个归档的新版——与其静默无效，不如当场说清楚该撤哪一版。
        """
        base = self._repository.get(workflow_id)
        if base is None:
            raise WorkflowValidationError(f"工作流定义不存在: {workflow_id}")
        latest = self._repository.latest(base.name)
        if latest is not None and latest.version > base.version:
            raise WorkflowValidationError(f"同名已有更新版本 v{latest.version}，要恢复的是那一版而不是 v{base.version}")
        return await self._repository.save(base.model_copy(update={"status": "active"}))

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
        return self._detail(current)

    def preview_detail(self, preview_id: str) -> dict[str, Any] | None:
        """预览结果只会在这里取到：`GET /instances/{wfi_…}` 看不到它。"""
        return self._previews.get(preview_id)

    @staticmethod
    def _detail(current: _MutableInstance) -> dict[str, Any]:
        """实例与预览共用的详情形状：画布要能凭它重建整张图，而不只是节点状态。"""
        return {
            "instance_id": current.instance_id,
            "workflow_id": current.definition.workflow_id,
            "workflow_version": current.definition.version,
            "lineage_id": current.lineage_id or current.definition.lineage_id,
            "trace_id": current.trace_id,
            "status": current.status.value,
            "error": current.error,
            "preview": current.dry_run,
            "preview_id": current.preview_id,
            # 重启接回来的实例与"一直在跑"要能分开：前者可能缺执行现场，
            # 后者被标成 failed 说明它已经不可能再继续（见 `restore_persisted_state`）。
            "recovered": current.recovered,
            "interrupted": current.interrupted,
            "created_at": current.created_at,
            "finished_at": current.finished_at,
            # 核签工单要能让人看见"签的是什么"：上报文本、定级来源与转核签的原因都在 payload 里，
            # 不带上就等于把决策要依据的事实留在内存里不外显。
            "payload": current.payload,
            "results": {
                node_id: run.output for node_id, run in current.runs.items() if run.state in (NodeState.SUCCEEDED, NodeState.DEGRADED)
            },
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
            "graph": {
                "nodes": [node.model_dump(mode="json") for node in current.nodes.values()],
                "edges": [edge.model_dump(mode="json") for edge in current.edges],
                "positions": {node_id: dict(position) for node_id, position in current.positions.items()},
            },
        }

    # ---------- 持久化与重启恢复 ----------

    async def restore_persisted_state(self) -> dict[str, int]:
        """启动恢复：先定义后实例；未终态实例按"能续的续、不能续的显式中断"处置。

        调用方（容器）负责在持久层不可达时跳过或接住异常——引擎不把"库没连上"
        和"库里没有数据"混成同一种沉默。恢复是幂等的：同一进程重复调用不会重复登记。
        """
        stats = {"definitions": 0, "instances": 0, "interrupted": 0}
        if self._persistence is None:
            self.recovery_stats = stats
            return stats
        definitions = await self._persistence.load_definitions(limit=self._repository.capacity)
        stats["definitions"] = await self._repository.restore_many(definitions)
        checkpoints = await self._persistence.load_open_instances(limit=OPEN_INSTANCE_RECOVERY_LIMIT)
        if len(checkpoints) >= OPEN_INSTANCE_RECOVERY_LIMIT:
            log.warning("未终态实例达到恢复上限，剩余实例留在库中未载入", extra={"limit": OPEN_INSTANCE_RECOVERY_LIMIT})
        for checkpoint in checkpoints:
            instance = self._restore_instance(checkpoint)
            if instance is None:
                continue
            stats["instances"] += 1
            if instance.interrupted:
                stats["interrupted"] += 1
        self.recovery_stats = stats
        log.info("工作流持久态已恢复", extra=stats)
        return stats

    def _restore_instance(self, checkpoint: InstanceCheckpoint) -> _MutableInstance | None:
        snapshot = checkpoint.instance
        if snapshot.instance_id in self._instances:
            return None
        if snapshot.status not in (InstanceStatus.RUNNING, InstanceStatus.WAITING):
            return None
        runtime = checkpoint.runtime
        definition = runtime.definition
        runs = {
            node.node_id: (
                snapshot.nodes[node.node_id].model_copy(deep=True)
                if node.node_id in snapshot.nodes
                else NodeRun(node_id=node.node_id, type=node.type)
            )
            for node in definition.nodes
        }
        instance = _MutableInstance(
            instance_id=snapshot.instance_id,
            definition=definition,
            trace_id=snapshot.trace_id,
            payload=dict(snapshot.payload),
            nodes={node.node_id: node.model_copy(deep=True) for node in definition.nodes},
            edges=[edge.model_copy(deep=True) for edge in definition.edges],
            runs=runs,
            status=snapshot.status,
            error=snapshot.error,
            decisions={node_id: dict(decision) for node_id, decision in runtime.decisions.items()},
            pending_waits=dict(runtime.pending_waits),
            pending_options={node_id: tuple(options) for node_id, options in runtime.pending_options.items()},
            lineage_id=runtime.lineage_id or definition.lineage_id,
            created_at=snapshot.created_at,
            finished_at=snapshot.finished_at,
            positions={node_id: dict(position) for node_id, position in runtime.positions.items()},
            recovered=True,
        )
        # 重启后时延账本若按本次 perf_counter 起算，会凭空多出一条"跑了 0 秒"的样本；
        # 把起点回拨到真实创建时刻，恢复后的时长仍是这条实例的真实存活时长。
        instance.created_mono = time.perf_counter() - _elapsed_seconds_since(snapshot.created_at)

        if snapshot.status is InstanceStatus.WAITING:
            # 等待现场是双向账：每个挂起点都对应一个 awaiting_human 节点，反之亦然。
            # 只查单向会漏掉"状态是 waiting、账上却没有挂起点"的坏行——那种实例接回来也永远续不了。
            awaiting = {node_id for node_id, run in runs.items() if run.state is NodeState.AWAITING_HUMAN}
            declared = set(instance.pending_waits)
            if not declared or declared != awaiting:
                self._interrupt(
                    instance,
                    f"恢复时等待点与节点状态不一致（挂起账 {sorted(declared)}，等待节点 {sorted(awaiting)}）",
                )
            else:
                self._register_restored(instance)
                log.warning(
                    "已接回等待人工的实例",
                    extra={"instance_id": instance.instance_id, "trace_id": instance.trace_id, "waits": sorted(instance.pending_waits)},
                )
                return instance
        else:
            unfinished = sorted(node_id for node_id, run in runs.items() if run.state not in TERMINAL_STATES)
            self._interrupt(
                instance,
                f"服务重启时实例仍在运行：{len(unfinished)} 个节点未完成（{', '.join(unfinished[:5])}），执行现场已丢失，未自动续跑",
            )
        self._register_restored(instance)
        # 中断是终态事实：立刻回写，下一次重启就不会再把它当成"可恢复的 running"。
        self._checkpoint(instance)
        return instance

    def _register_restored(self, instance: _MutableInstance) -> None:
        self._instances[instance.instance_id] = instance
        self._locks.setdefault(instance.instance_id, asyncio.Lock())

    @staticmethod
    def _interrupt(instance: _MutableInstance, reason: str) -> None:
        instance.interrupted = True
        instance.status = InstanceStatus.FAILED
        instance.error = reason
        instance.finished_at = instance.finished_at or now_iso()
        for run in instance.runs.values():
            if run.state not in TERMINAL_STATES:
                run.state = NodeState.CANCELLED
                run.notes.append("服务重启：执行现场丢失，节点被标记取消")

    def _checkpoint(self, instance: _MutableInstance) -> None:
        """把当前现场投给持久层（写侧非阻塞）。只在生命周期里程碑调用，不在每次状态跳变时写。

        定义快照取自实例自己的 nodes/edges：运行中改图之后，定义仓储里的那一版已经
        不是这张图的真相，恢复必须按实例现场重建。
        """
        if self._persistence is None or instance.dry_run:
            return
        definition = instance.definition.model_copy(
            update={
                "nodes": [node.model_copy(deep=True) for node in instance.nodes.values()],
                "edges": [edge.model_copy(deep=True) for edge in instance.edges],
            }
        )
        runtime = WorkflowRuntime(
            definition=definition,
            lineage_id=instance.lineage_id,
            decisions={node_id: dict(decision) for node_id, decision in instance.decisions.items()},
            pending_waits=dict(instance.pending_waits),
            pending_options={node_id: tuple(options) for node_id, options in instance.pending_options.items()},
            positions={node_id: dict(position) for node_id, position in instance.positions.items()},
        )
        self._persistence.save_instance(InstanceCheckpoint(instance=instance.snapshot(), runtime=runtime))

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
        document = self._repository.document_of(workflow_id)
        instance = self._instantiate(
            definition,
            trace_id=trace_id,
            payload=payload or {},
            lineage_id=definition.lineage_id,
            positions=self._layout_positions(document),
        )
        await self._drive(instance)
        self._retire_kept_instances(keep=instance.instance_id)
        return self.instance_detail(instance.instance_id) or {}

    @staticmethod
    def _layout_positions(document: WorkflowDocument | None) -> dict[str, dict[str, float]]:
        if document is None:
            return {}
        return {node_id: {"x": position.x, "y": position.y} for node_id, position in document.layout.positions.items()}

    async def preview(
        self,
        *,
        document: WorkflowDocument,
        payload: dict[str, Any] | None = None,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        """在当前文档上跑一遍 dry-run，结果与实例详情同构，但不进任何账本。

        文档可能来自未保存的草稿，所以这里不碰仓储/草稿/实例字典；`_previews` 独立，
        上限与 TTL 由 `InMemoryPreviewStore` 管。先占位再执行：等 LLM 跑完才说"满了"
        是白花一次调用。
        """
        body = dict(payload or {})
        encoded = orjson.dumps(body)
        if len(encoded) > MAX_PREVIEW_PAYLOAD_BYTES:
            raise WorkflowAppError(
                WorkflowErrorCode.DOCUMENT_TOO_LARGE,
                f"试运行入参超过 {MAX_PREVIEW_PAYLOAD_BYTES // 1024} KB（实际 {len(encoded)} 字节）",
                details={"kind": "preview_payload", "bytes": len(encoded), "limit": MAX_PREVIEW_PAYLOAD_BYTES},
            )
        latest = self._repository.latest_by_lineage(document.lineage_id)
        definition = definition_from_document(
            document,
            workflow_id=document.lineage_id,
            version=latest.version if latest is not None else 1,
        )
        try:
            self.validate_definition(definition)
        except WorkflowValidationError as exc:
            raise WorkflowAppError(WorkflowErrorCode.VALIDATION_FAILED, str(exc)) from exc
        try:
            self._previews.ensure_room()
        except PreviewLimitReached as exc:
            raise WorkflowAppError(
                WorkflowErrorCode.PREVIEW_LIMIT,
                str(exc),
                details={"limit": self._previews.limit},
            ) from exc

        preview_id = new_preview_id()
        instance = self._instantiate(
            definition,
            trace_id=trace_id or new_trace_id(),
            payload=body,
            lineage_id=document.lineage_id,
            positions=self._layout_positions(document),
            dry_run=True,
            preview_id=preview_id,
            register=False,
        )
        try:
            await self._drive(instance)
        except BaseException:
            # 失败也留档：试运行的价值一半在"哪一步炸了"，结果表里没有就只剩一句错误。
            self._store_preview(preview_id, instance, raise_on_limit=False)
            raise
        self._store_preview(preview_id, instance)
        return self.preview_detail(preview_id) or {}

    def _store_preview(self, preview_id: str, instance: _MutableInstance, *, raise_on_limit: bool = True) -> None:
        try:
            self._previews.put(preview_id, self._detail(instance))
        except PreviewLimitReached as exc:
            if not raise_on_limit:
                log.warning("试运行结果未留存：结果表已满", extra={"preview_id": preview_id})
                return
            raise WorkflowAppError(
                WorkflowErrorCode.PREVIEW_LIMIT,
                str(exc),
                details={"limit": self._previews.limit},
            ) from exc

    def _retire_kept_instances(self, *, keep: str = "") -> int:
        """丢掉最老的**已终态**实例，把驻留数量压回上界内。

        只动 succeeded/failed/aborted：WAITING 是"等人签字"的现场工单， RUNNING 还在跑，
        两者被挤掉的表现都是值班员打不开自己刚起的图——那是比内存更贵的代价。
        `keep` 给刚刚跑完的那个实例一个免死名额：调用方紧接着还要读它的详情。

        只在 `start()` 里调：实例是 `_instantiate` 的产物，而它只有 `start()` 一个调用方，
        所以"每建一个就压一次上界"已经盖住了全部增长路径。`resume`/`bypass_node` 那两处
        也调 `_drive`，但它们不新建实例——在那里再压一次是把同一件事写两遍。
        """
        terminal = {InstanceStatus.SUCCEEDED, InstanceStatus.FAILED, InstanceStatus.ABORTED}
        overflow = len(self._instances) - self._max_instances
        if overflow <= 0:
            return 0
        dropped = 0
        for instance_id, instance in list(self._instances.items()):
            if dropped >= overflow:
                break
            if instance_id == keep or instance.status not in terminal:
                continue
            self._instances.pop(instance_id, None)
            self._locks.pop(instance_id, None)
            dropped += 1
        self.evicted_instances += dropped
        return dropped

    def _instantiate(
        self,
        definition: WorkflowDef,
        *,
        trace_id: str,
        payload: dict[str, Any],
        lineage_id: str = "",
        positions: dict[str, dict[str, float]] | None = None,
        dry_run: bool = False,
        preview_id: str | None = None,
        register: bool = True,
    ) -> _MutableInstance:
        instance = _MutableInstance(
            instance_id=new_instance_id(),
            definition=definition,
            trace_id=trace_id,
            payload=dict(payload),
            nodes={node.node_id: node.model_copy(deep=True) for node in definition.nodes},
            edges=[edge.model_copy(deep=True) for edge in definition.edges],
            runs={node.node_id: NodeRun(node_id=node.node_id, type=node.type) for node in definition.nodes},
            lineage_id=lineage_id or definition.lineage_id,
            positions=dict(positions or {}),
            dry_run=dry_run,
            preview_id=preview_id,
        )
        if register:
            self._instances[instance.instance_id] = instance
            self._locks.setdefault(instance.instance_id, asyncio.Lock())
            # 先落"这张图已经开跑"，再开始执行：进程在第一个节点里被杀时，
            # 库里至少有一条 running 现场，重启才能把它显式收成 interrupted 而不是当没发生过。
            self._checkpoint(instance)
        return instance

    async def _drive(self, instance: _MutableInstance) -> None:
        """同一实例同一时刻只允许一条驱动循环。

        `bypass_node` 在实例 RUNNING 时也会再起一条驱动；没有这把锁，两条循环可能同时
        `_collect_ready` 到同一批节点，把同一个 handler 执行两遍。锁建在 `_instantiate`，
        试运行实例不登记、也不需要并发驱动。

        驱动结束（无论收敛到终态还是挂起等待人工）落一次检查点：节点状态是这一轮
        执行的全部事实，写侧走持久层的缓冲，不阻塞调度。
        """
        lock = self._locks.get(instance.instance_id)
        if lock is None:
            await self._drive_once(instance)
            self._checkpoint(instance)
            return
        async with lock:
            if instance.status in TERMINAL_STATUSES:
                return
            await self._drive_once(instance)
            self._checkpoint(instance)

    async def _drive_once(self, instance: _MutableInstance) -> None:
        """主调度循环：每轮找出可执行节点并发执行，直到无可执行节点。

        事件驱动、无轮询等待：一轮 gather 只在"确有就绪节点"时挂起，就绪即执行。
        一旦实例进入 FAILED（abort 策略）、ABORTED（人工中止）或 WAITING（等待人工决策），
        立即停止调度，未触及的下游节点保持 pending —— 不得因为"暂时不可就绪"而被误判为级联跳过。

        链路结构：一次驱动 = 一条 `workflow_instance` 跨度，其 traceID 由实例的契约 trace_id
        确定性映射而来，本轮就绪执行的节点跨度自动成为它的子节点（见 observability/instrumentation.py）。
        人工决策后续跑会再产生一条同 traceID 的驱动跨度——这恰好是"等待人工"那一段的真实证据。

        试运行不建跨度、不记账本：预览不该让 SLA 指标与链路图多出一些"从未正式发生"的样本。
        """
        if instance.dry_run:
            await self._run_loop(instance)
            return
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
            await self._run_loop(instance)
            instrumentation.set_attributes(instance_span, span_attrs={"aegis.workflow.status": instance.status.value})
            if instance.status is InstanceStatus.FAILED:
                instrumentation.mark_error(instance_span, instance.error or "实例失败", code="workflow_failed")
            elif instance.status is InstanceStatus.WAITING:
                instrumentation.set_attributes(instance_span, span_attrs={"aegis.workflow.awaiting_human": True})

    async def _run_loop(self, instance: _MutableInstance) -> None:
        while True:
            if instance.status in (InstanceStatus.FAILED, InstanceStatus.WAITING, InstanceStatus.ABORTED):
                break
            ready = self._collect_ready(instance)
            if not ready:
                break
            await asyncio.gather(*(self._execute_node(instance, node_id) for node_id in ready))
        self._finalise(instance)

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
        # 两边都归一：条件存的就是 normalize_branch 后的值，分支名却是人写的原样
        return normalize_branch(str(source_run.output.get("branch", ""))) == edge.condition

    def _note_unmatched_branch(self, instance: _MutableInstance, node_id: str) -> None:
        """分支值没有任何出口可走时，把原因写在**发出分支的那个节点**上。

        下游只会得到一句"上游分支未命中，级联跳过"，看不出是哪个分支值没处去，
        而实例状态照样写成 succeeded——通知一条都没发出去，屏幕上却没有线索。
        """
        run = instance.runs[node_id]
        raw_branch = str(run.output.get("branch", ""))
        emitted = normalize_branch(raw_branch)
        outgoing = [edge for edge in instance.edges if edge.source == node_id]
        if not outgoing:
            return
        if any(not edge.condition or edge.condition == emitted for edge in outgoing):
            return
        labels = "、".join(sorted({edge.condition for edge in outgoing if edge.condition}))
        run.notes.append(f"分支 {raw_branch} 没有任何出口连线匹配（连线上写的分支名：{labels}）；下游会被级联跳过")

    async def _execute_node(self, instance: _MutableInstance, node_id: str) -> None:
        node = instance.nodes[node_id]
        run = instance.runs[node_id]
        spec = self._registry.require(node.type)

        # 节点可能在本轮 collect 之后、handler 启动之前被 abort 波及：先认终态，
        # 否则下面那句 `run.state = RUNNING` 会把已经 CANCELLED 的节点重新点亮。
        if instance.status is InstanceStatus.ABORTED:
            if run.state not in (NodeState.SUCCEEDED, NodeState.DEGRADED):
                run.state = NodeState.CANCELLED
                run.notes.append("实例已中止，节点未启动")
            return

        run.schedule_latency_ms = (time.perf_counter() - (run.ready_mono or time.perf_counter())) * 1000
        if not instance.dry_run:
            self._tracer.record(
                "workflow_schedule_ms",
                max(run.schedule_latency_ms, 0.0),
                trace_id=instance.trace_id,
            )

        # 节点跨度在"调度时延已落账"之后创建：建跨度的开销不会算进 ≤2s 的 workflow_schedule_ms 样本，
        # 跨度只承载结构证据（node_id / attempt / 终态），SLA 判定仍以时延账本为准。
        # 试运行拿无操作跨度占位：同一条 `_execute_node` 分支不必为"要不要记"分叉。
        node_span = (
            instrumentation.null_span()
            if instance.dry_run
            else instrumentation.start_span(
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
        )

        run.state = NodeState.RUNNING
        run.started_mono = time.perf_counter()
        first_failure_mono: float | None = None

        while True:
            if instance.status is InstanceStatus.ABORTED:
                self._cancel_node(instance, run, node_id, node_span, "实例已中止，节点未启动")
                return
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
                services=self._dry_services if instance.dry_run else self._services,
                attempt=run.attempts,
                decision=instance.decisions.get(node_id),
                dry_run=instance.dry_run,
            )
            try:
                async with self._semaphore:
                    if instance.status is InstanceStatus.ABORTED:
                        self._cancel_node(instance, run, node_id, node_span, "实例已中止，节点未启动")
                        return
                    # NodeHandler 的返回类型是 Awaitable，不保证是 Coroutine：ensure_future 都能接住。
                    handler_task = asyncio.ensure_future(spec.handler(context, node.config))
                    instance.running_tasks[node_id] = handler_task
                    try:
                        outcome: NodeOutcome = await asyncio.wait_for(handler_task, timeout=node.timeout_ms / 1000)
                    finally:
                        instance.running_tasks.pop(node_id, None)
            except asyncio.CancelledError:
                if instance.status is InstanceStatus.ABORTED:
                    self._cancel_node(instance, run, node_id, node_span, "实例已中止，执行被取消")
                    return
                raise
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
                # handler 已经跑完但中止先到：副作用可能已经发生，结果本身不能再算数。
                if instance.status is InstanceStatus.ABORTED:
                    self._cancel_node(instance, run, node_id, node_span, "实例已中止，执行结果被丢弃")
                    return
                run.output = dict(outcome.output)
                if outcome.branch:
                    run.output.setdefault("branch", outcome.branch)
                    self._note_unmatched_branch(instance, node_id)
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
        if instance.status is InstanceStatus.ABORTED:
            run.state = NodeState.CANCELLED
            return "stop"
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

    def _cancel_node(
        self,
        instance: _MutableInstance,
        run: NodeRun,
        node_id: str,
        node_span: Any,
        note: str,
    ) -> None:
        """中止收口：只记一次终态与跨度，重复调用直接返回。"""
        if run.finished_mono is not None:
            return
        run.state = NodeState.CANCELLED
        run.notes.append(note)
        self._record_node_latency(instance, run, node_id)
        instrumentation.set_attributes(node_span, span_attrs={"aegis.node.state": run.state.value, "aegis.node.cancelled": True})
        instrumentation.end_span(node_span)

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
            if not instance.dry_run:
                self._tracer.record("workflow_node_ms", run.duration_ms, trace_id=instance.trace_id)
        if first_failure_mono is not None and not instance.dry_run:
            reschedule_ms = (run.finished_mono - first_failure_mono) * 1000
            self._tracer.record("workflow_reschedule_ms", reschedule_ms, trace_id=instance.trace_id)
        log.debug(
            "节点完成",
            extra={"instance_id": instance.instance_id, "node_id": node_id, "state": run.state.value},
        )

    def _finalise(self, instance: _MutableInstance) -> None:
        if instance.status in (InstanceStatus.WAITING, InstanceStatus.ABORTED):
            return
        if instance.status is InstanceStatus.FAILED:
            instance.finished_at = instance.finished_at or now_iso()
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
        instance.finished_at = instance.finished_at or now_iso()
        if not instance.dry_run:
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
        self._checkpoint(instance)
        return {"node_id": node_id, "config": merged}

    async def insert_node(
        self,
        instance_id: str,
        *,
        after: str,
        node: dict[str, Any],
        position: dict[str, float] | None = None,
    ) -> dict[str, Any]:
        """在途实例插入节点：把 after 的原出边重接到新节点，保证图仍无环。"""
        instance = self._require_instance(instance_id)
        if after not in instance.nodes:
            raise WorkflowValidationError(f"锚点节点不存在: {after}")
        node_def = NodeDef.model_validate(node)
        if node_def.node_id in instance.nodes:
            raise WorkflowValidationError(f"节点 ID 已存在: {node_def.node_id}")
        spec = self._registry.require(node_def.type)
        spec.validate_config(node_def.config)

        anchor = instance.positions.get(after) or {"x": 0.0, "y": 0.0}
        raw = position if position is not None else {"x": anchor["x"] + 300, "y": anchor["y"]}
        try:
            placed = Position(**raw)
        except ValidationError as exc:
            raise WorkflowValidationError(f"插入节点位置不合法: {_as_workflow_error(exc)}") from exc

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
        # 只在图确认合法后落位置：否则被拒绝的插入会在画布上留下一枚无主坐标。
        instance.positions[node_def.node_id] = {"x": placed.x, "y": placed.y}
        self._checkpoint(instance)
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
        else:
            # 被旁路的实例若已终态（如失败/中止），`_drive` 不会跑，旁路事实仍需落库。
            self._checkpoint(instance)
        return self.instance_detail(instance_id) or {}

    async def abort(self, instance_id: str, *, reason: str = "") -> dict[str, Any]:
        instance = self._require_instance(instance_id)
        # 幂等：已中止的实例再次中止不重复处置、不重复上报时延指标。
        if instance.status is InstanceStatus.ABORTED:
            return self.instance_detail(instance_id) or {}
        # 先落状态再取消任务：handler 的 CancelledError 分支据此认出"这是中止，不是故障"。
        instance.status = InstanceStatus.ABORTED
        instance.error = reason or "人工中止"
        instance.finished_at = instance.finished_at or now_iso()
        for run in instance.runs.values():
            if run.state is NodeState.RUNNING:
                run.state = NodeState.CANCELLED
                run.notes.append("实例中止，执行被取消")
            elif run.state in (NodeState.PENDING, NodeState.READY, NodeState.AWAITING_HUMAN):
                run.state = NodeState.CANCELLED
        # 停止动作本身：状态先变，再逐个取消 handler；不取消就只是"账面中止"。
        for task in list(instance.running_tasks.values()):
            task.cancel()
        instance.running_tasks.clear()
        instance.pending_waits.clear()
        instance.pending_options.clear()
        if not instance.dry_run:
            self._tracer.record("workflow_instance_ms", (time.perf_counter() - instance.created_mono) * 1000, trace_id=instance.trace_id)
        self._checkpoint(instance)
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
