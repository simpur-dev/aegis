"""知识提供者的对外契约：Protocol + 召回结果 + 预算与降级。

架构约束（P0 硬约束，有测试守着）：Graphiti 的一次 `add_episode` 会触发 4—7 次 LLM 调用，
而预警生成（≤3min 指标）与预案生成走的都是**读**路径。因此：
1. 写路径（`learn`）只在案例入库/复盘时调用，绝不在召回链路里出现；
2. 读路径（`recall`）在结构上不可能访问 LLM 客户端——见 `graphiti_store.GraphitiKnowledgeProvider`
   的双实例设计（读实例注入的是"一旦被调用就抛错"的 LLM 守卫）。

与 `aegis.retrieval`（并行建设中）的接缝：本模块只认 `KnowledgeProvider` 协议。
向量/稀疏检索若后续落到 retrieval，实现同一协议的类可直接作为 `primary` 注入
`FallbackKnowledgeProvider`，knowledge 包不新增 import 依赖。
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from aegis.config import Settings, get_settings
from aegis.domain.messages import now_iso
from aegis.errors import AegisError, DeadlineExceededError
from aegis.knowledge.cases import HazardCase

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from aegis.observability.tracer import Tracer

T = TypeVar("T")

DEFAULT_RECALL_BUDGET_MS = 1_200.0
RECALL_LATENCY_METRIC = "knowledge_recall_ms"
RecallSource = Literal["graphiti", "in_memory"]


class KnowledgeConfigError(AegisError):
    """知识层装配错误（配置缺失，或读路径被要求调用 LLM 这类设计违例）。"""


class CaseMatch(BaseModel):
    """一条召回命中：案例本体 + 命中理由 + 图谱侧的双时态证据。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str
    score: float = Field(ge=0.0)
    source: RecallSource
    matched_on: list[str] = Field(default_factory=list)
    case: HazardCase | None = None
    graph_facts: list[str] = Field(default_factory=list, max_length=12)
    valid_at: str | None = None
    invalid_at: str | None = None
    degraded: bool = False
    note: str = ""

    @property
    def title(self) -> str:
        return self.case.title if self.case else self.case_id

    @property
    def hazard_types(self) -> list[str]:
        return list(self.case.hazard_types) if self.case else []

    @property
    def confidence(self) -> float:
        return self.case.confidence if self.case else 0.0

    @property
    def estimated_delay_hours(self) -> float:
        return self.case.estimated_delay_hours if self.case else 0.0

    @property
    def usable(self) -> bool:
        """只有带回案例本体的命中才能直接被预案编排消费。"""
        return self.case is not None

    @classmethod
    def from_case(
        cls,
        case: HazardCase,
        *,
        score: float,
        source: RecallSource,
        matched_on: Sequence[str] = (),
        degraded: bool = False,
        note: str = "",
    ) -> CaseMatch:
        return cls(
            case_id=case.case_id,
            score=round(max(score, 0.0), 4),
            source=source,
            matched_on=list(matched_on),
            case=case,
            valid_at=case.observed_at,
            degraded=degraded,
            note=note,
        )


@dataclass(frozen=True, slots=True)
class DegradationRecord:
    """一次降级的事实记录：供链路台账、指标导出与取证复用，不做字符串拼接。"""

    reason: str
    detail: str
    primary: str
    fallback: str
    returned: int
    budget_ms: float | None
    at: str


@runtime_checkable
class KnowledgeProvider(Protocol):
    """案例知识提供者：读召回（LLM-free）与写学习（ingest）严格分离。"""

    async def recall(
        self,
        query: str,
        *,
        hazard_type: str | None = None,
        region_code: str | None = None,
        limit: int = 5,
        budget_ms: float | None = None,
    ) -> list[CaseMatch]: ...

    async def learn(self, case: HazardCase) -> None: ...


async def run_with_budget(awaitable: Awaitable[T], budget_ms: float | None, *, label: str) -> T:
    """预算口径统一在此：超限即 DeadlineExceededError，由上层决定是否降级，不静默吞掉。"""
    if budget_ms is None:
        return await awaitable
    if budget_ms <= 0:
        raise KnowledgeConfigError(f"召回预算必须为正: {label}", detail={"budget_ms": budget_ms})
    try:
        return await asyncio.wait_for(awaitable, timeout=budget_ms / 1000.0)
    except TimeoutError as exc:
        raise DeadlineExceededError(
            f"知识召回超出预算: {label}",
            detail={"budget_ms": budget_ms, "label": label},
        ) from exc


class FallbackKnowledgeProvider:
    """智能体/图谱优先、平台降级：主召回超时、报错或空结果时无缝切到内存案例库。

    这是"降级"的**唯一**发生点：任何主实现（Graphiti、后续 retrieval 的混合检索）都不必
    认识兜底实现，装配关系留在本类，便于 `container` 一处接线。
    """

    name = "fallback"

    def __init__(
        self,
        *,
        primary: KnowledgeProvider,
        fallback: KnowledgeProvider,
        default_budget_ms: float = DEFAULT_RECALL_BUDGET_MS,
        on_degradation: Callable[[DegradationRecord], None] | None = None,
        tracer: Tracer | None = None,
        history_size: int = 64,
    ) -> None:
        self._primary = primary
        self._fallback = fallback
        self._default_budget_ms = default_budget_ms
        self._on_degradation = on_degradation
        self._tracer = tracer
        self._history: deque[DegradationRecord] = deque(maxlen=history_size)

    @property
    def primary(self) -> KnowledgeProvider:
        return self._primary

    @property
    def fallback(self) -> KnowledgeProvider:
        return self._fallback

    @property
    def degradations(self) -> tuple[DegradationRecord, ...]:
        return tuple(self._history)

    async def recall(
        self,
        query: str,
        *,
        hazard_type: str | None = None,
        region_code: str | None = None,
        limit: int = 5,
        budget_ms: float | None = None,
    ) -> list[CaseMatch]:
        budget = budget_ms if budget_ms is not None else self._default_budget_ms
        started = time.perf_counter()
        outcome = "ok"
        try:
            # 预算既告知主实现（可据此收紧数据库侧超时），也在外层强制一次（不认预算的实现不会挂死）
            matches: list[CaseMatch] = await run_with_budget(
                self._primary.recall(query, hazard_type=hazard_type, region_code=region_code, limit=limit, budget_ms=budget),
                budget,
                label="knowledge_recall",
            )
            if not matches:
                outcome = "empty"
                matches = await self._degrade(
                    query,
                    reason="primary_empty",
                    detail="主召回无命中",
                    hazard_type=hazard_type,
                    region_code=region_code,
                    limit=limit,
                    budget_ms=budget,
                )
        except (DeadlineExceededError, TimeoutError) as exc:
            outcome = "timeout"
            matches = await self._degrade(
                query,
                reason="primary_timeout",
                detail=str(exc),
                hazard_type=hazard_type,
                region_code=region_code,
                limit=limit,
                budget_ms=budget,
            )
        except AegisError as exc:
            outcome = "primary_error"
            matches = await self._degrade(
                query,
                reason="primary_error",
                detail=exc.message,
                hazard_type=hazard_type,
                region_code=region_code,
                limit=limit,
                budget_ms=budget,
            )
        self._record_latency((time.perf_counter() - started) * 1000, budget, outcome)
        return matches

    async def learn(self, case: HazardCase) -> None:
        """学习走尽力而为：图谱不可用时案例仍进入进程内库，且降级被记账。"""
        try:
            await self._primary.learn(case)
        except (AegisError, TimeoutError) as exc:
            await self._fallback.learn(case)
            self._note(reason="learn_failed", detail=str(exc), returned=1)

    async def _degrade(
        self,
        query: str,
        *,
        reason: str,
        detail: str,
        hazard_type: str | None,
        region_code: str | None,
        limit: int,
        budget_ms: float,
    ) -> list[CaseMatch]:
        # 兜底调用不带预算：内存打分不能被同一个预算再砍一次
        matches = await self._fallback.recall(query, hazard_type=hazard_type, region_code=region_code, limit=limit)
        flagged = [match.model_copy(update={"degraded": True, "note": reason}) for match in matches]
        self._note(reason=reason, detail=detail, returned=len(flagged), budget_ms=budget_ms)
        return flagged

    def _note(self, *, reason: str, detail: str, returned: int, budget_ms: float | None = None) -> None:
        record = DegradationRecord(
            reason=reason,
            detail=detail[:400],
            primary=type(self._primary).__name__,
            fallback=type(self._fallback).__name__,
            returned=returned,
            budget_ms=budget_ms,
            at=now_iso(),
        )
        self._history.append(record)
        if self._on_degradation is not None:
            self._on_degradation(record)

    def _record_latency(self, elapsed_ms: float, budget_ms: float, outcome: str) -> None:
        if self._tracer is None:
            return
        self._tracer.ledger.set_budget(RECALL_LATENCY_METRIC, budget_ms)
        self._tracer.record(RECALL_LATENCY_METRIC, elapsed_ms, outcome=outcome)


def build_knowledge_provider(
    settings: Settings | None = None,
    *,
    graphiti_uri: str | None = None,
    cases: Sequence[HazardCase] | None = None,
    recall_budget_ms: float | None = None,
    on_degradation: Callable[[DegradationRecord], None] | None = None,
    tracer: Tracer | None = None,
) -> KnowledgeProvider:
    """装配入口：未给 `graphiti_uri` 时返回纯内存提供者（降级链形态，零外部依赖）。

    图谱开关目前由调用方（container）决定是否给出 URI：`config.py` 尚未新增 knowledge 相关字段，
    这里不猜测不存在的配置项，需要哪些字段见交付说明。
    """
    from aegis.knowledge.memory_store import InMemoryKnowledgeProvider

    cfg = settings or get_settings()
    memory = InMemoryKnowledgeProvider(cases=cases)
    if not graphiti_uri:
        return memory

    from aegis.knowledge.graphiti_store import GraphitiConfig, GraphitiKnowledgeProvider  # 延迟导入：降级模式不要求 neo4j/graphiti

    budget = recall_budget_ms if recall_budget_ms is not None else _recall_budget_from_env()
    primary = GraphitiKnowledgeProvider(
        config=GraphitiConfig.from_parts(uri=graphiti_uri, settings=cfg),
        cases=cases,
        default_budget_ms=budget,
    )
    return FallbackKnowledgeProvider(
        primary=primary,
        fallback=memory,
        default_budget_ms=budget,
        on_degradation=on_degradation,
        tracer=tracer,
    )


def _recall_budget_from_env() -> float:
    """预算可由 AEGIS_KNOWLEDGE_RECALL_BUDGET_MS 覆盖：不改 config.py 也能现场调参。"""
    import os

    raw = os.getenv("AEGIS_KNOWLEDGE_RECALL_BUDGET_MS", "")
    try:
        value = float(raw) if raw else DEFAULT_RECALL_BUDGET_MS
    except ValueError:
        return DEFAULT_RECALL_BUDGET_MS
    return value if value > 0 else DEFAULT_RECALL_BUDGET_MS
