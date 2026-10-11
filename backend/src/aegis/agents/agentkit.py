"""agentkit：自研契约壳（§3.0 / D2）。

把《智能体接入规范》§1/§2/§5/§6 的**客户端职责**收敛成一份可复用基类——注册、心跳、
msg_id 幂等去重、deadline 限时、error.raise——真智能体只需实现 `handle()` 内环。
这样自建（D2）与外部团队后续交付走同一契约面：都只经 `AgentMessage v1` 往来，平台不因
谁实现而改变准入。

边界（架构铁律 / 一致性门禁 §7.6）：只依赖 bus / domain / enums / errors，**绝不 import 存储层**
（`aegis.storage` / sqlalchemy / asyncpg / neo4j …）。取数一律经总线 request 载荷或（MB 起的）
MCP 工具。LLM 走网关 v2（可选注入，duck-typed）：缺位即由内环自行降级，平台侧另有规则降级轨。

D10：`handle()` 可返回 `Reply(model_info=…, disagreement=…)`，壳把它写进响应信封，
使每条 LLM 结论自带可复现凭证（与网关 `model_info_for_task` 对齐）。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from aegis.bus import subjects
from aegis.bus.naming import consumer_name
from aegis.bus.transport import BusTransport
from aegis.domain.enums import Action, AgentType, HazardType, MessageKind
from aegis.domain.messages import (
    AgentMessage,
    CapabilityRegistration,
    ModelInfo,
    Ref,
    make_error,
    make_event,
    make_response,
)
from aegis.errors import ErrorCode

log = logging.getLogger("aegis.agentkit")

_ALL_HAZARDS = [h.value for h in HazardType if h is not HazardType.UNKNOWN]


@dataclass(slots=True)
class Reply:
    """内环产出：payload 必填，D10 溯源字段可选（None 即不写入信封）。"""

    payload: dict[str, Any]
    model_info: ModelInfo | None = None
    disagreement: bool | None = None


@dataclass
class AgentKit:
    transport: BusTransport
    agent_type: AgentType
    agent_id: str
    capabilities: list[str]
    hazard_types: list[str] = field(default_factory=lambda: list(_ALL_HAZARDS))
    max_concurrency: int = 4
    version: str = "agent-1.0.0"
    llm: Any | None = None
    #: 内软超时先于网关判定（§8 风险对策）：给网关预留这点余量，避免两侧同时超时读成"网络抖"。
    deadline_margin_ms: int = 50
    dedup_capacity: int = 4_096

    _seen: OrderedDict[str, None] = field(default_factory=OrderedDict, repr=False)
    #: 请求到达时刻（msg_id → perf_counter）：预算闸与网关必须看同一只表
    _arrivals: dict[str, float] = field(default_factory=dict, repr=False)
    _subs: list[Any] = field(default_factory=list, repr=False)
    _hb_task: asyncio.Task[None] | None = field(default=None, repr=False)
    handled: int = 0
    duplicates_skipped: int = 0
    degraded: int = 0

    # ---------- 生命周期（规范 §6） ----------

    @property
    def registration(self) -> CapabilityRegistration:
        return CapabilityRegistration(
            agent_id=self.agent_id,
            agent_type=self.agent_type.value,
            capabilities=self.capabilities,
            hazard_types=self.hazard_types,
            max_concurrency=self.max_concurrency,
            version=self.version,
        )

    async def start(self, *, heartbeat_interval: float = 1.0) -> None:
        await self.transport.publish(subjects.agent_hb(self.agent_type), self._lifecycle_frame(Action.AGENT_REGISTER))
        self._subs.append(
            await self.transport.subscribe(
                subjects.agent_in(self.agent_type),
                self._on_request,
                queue=consumer_name("cg", self.agent_type.value, self.agent_id),
                durable=consumer_name("d", self.agent_type.value, self.agent_id),
            )
        )
        self._hb_task = asyncio.create_task(self._heartbeat_loop(heartbeat_interval), name=f"hb-{self.agent_id}")

    async def stop(self) -> None:
        if self._hb_task is not None:
            self._hb_task.cancel()
            await asyncio.gather(self._hb_task, return_exceptions=True)
            self._hb_task = None
        for sub in self._subs:
            await sub.cancel()
        self._subs.clear()

    def _lifecycle_frame(self, action: Action) -> AgentMessage:
        return AgentMessage(
            source=self.agent_id,
            target="platform.registry",
            kind=MessageKind.EVENT,
            action=action.value,
            payload={"agent_id": self.agent_id, "registration": self.registration.model_dump()},
        )

    async def _heartbeat_loop(self, interval: float) -> None:
        try:
            while True:
                await asyncio.sleep(interval)
                await self.transport.publish(subjects.agent_hb(self.agent_type), self._lifecycle_frame(Action.AGENT_HEARTBEAT))
        except asyncio.CancelledError:
            return

    # ---------- 内环钩子（子类实现） ----------

    async def handle(self, request: AgentMessage) -> Reply | dict[str, Any]:
        raise NotImplementedError("智能体内环须实现 handle()")

    # ---------- 请求处理：幂等 + 限时 + 降级/错误（规范 §1/§2/§5） ----------

    def elapsed_ms(self, request: AgentMessage) -> float:
        """这条请求从到达至今过了多久（ms）。没有到达记录（直接调 handle 的单测）就当 0。"""
        arrival = self._arrivals.get(request.msg_id)
        return 0.0 if arrival is None else (time.perf_counter() - arrival) * 1000

    async def _on_request(self, request: AgentMessage) -> None:
        if request.msg_id in self._seen:
            self.duplicates_skipped += 1
            return
        self._seen[request.msg_id] = None
        while len(self._seen) > self.dedup_capacity:
            self._seen.popitem(last=False)

        self.handled += 1
        reply_subject = request.reply_to or subjects.agent_out(self.agent_type)
        # 到达时刻才是预算的起跑线：网关也是从这里开始等回执。内环自己另起一只表会把排队时间
        # 当免费——真栈实测"看起来还有钱"而网关到点就砍。
        self._arrivals[request.msg_id] = time.perf_counter()

        timeout: float | None = None
        if request.deadline_ms:
            # 这里减的是"内环自己的墙钟"，不含总线排队：网关从发出那一刻起等，排队发生在投递之前，
            # 本层看不见（实测：消息一到就进 _on_request，elapsed 在此处≈0）。所以超时的最终裁量
            # 权在网关侧的 deadline 上，内环的 elapsed_ms 只用于"还要不要再花一次模型调用"的自算。
            timeout = max(0.0, (request.deadline_ms - self.deadline_margin_ms) / 1000.0)
        try:
            if timeout is None:
                outcome = await self.handle(request)
            else:
                outcome = await asyncio.wait_for(self.handle(request), timeout)
        except TimeoutError:
            # 内环没在预算内给出结论：以超时错误回执，平台据此走规则降级轨（LLM/内环不可用≠平台不可用）。
            self.degraded += 1
            await self._publish(
                reply_subject,
                make_error(
                    request,
                    source=self.agent_id,
                    code=ErrorCode.TIMEOUT,
                    message="内环超时，转平台降级",
                    detail={"deadline_ms": request.deadline_ms, "agent_id": self.agent_id},
                    retryable=True,
                ),
            )
            return
        except Exception as exc:  # 内环异常不得静默：回类型化 error.raise，让网关如实记账
            await self._publish(
                reply_subject,
                make_error(
                    request,
                    source=self.agent_id,
                    code=ErrorCode.INTERNAL,
                    message=f"内环异常: {type(exc).__name__}",
                    detail={"agent_id": self.agent_id},
                    retryable=True,
                ),
            )
            return
        finally:
            # 到达记录只为本次处理存在：不清就是按消息数无界增长（长跑班次必漏）
            self._arrivals.pop(request.msg_id, None)

        reply = outcome if isinstance(outcome, Reply) else Reply(payload=outcome)
        message = make_response(request, source=self.agent_id, payload=reply.payload)
        if reply.model_info is not None or reply.disagreement is not None:
            message = message.model_copy(update={"model_info": reply.model_info, "disagreement": reply.disagreement})
        await self._publish(reply_subject, message)

    # ---------- 主动事件产出（如 perceive 出 anomaly/trigger_hit） ----------

    async def emit_event(
        self,
        action: str,
        payload: dict[str, Any],
        *,
        trace_id: str,
        refs: list[Ref] | None = None,
        target: str = "*",
        model_info: ModelInfo | None = None,
        priority: int = 3,
    ) -> None:
        event = make_event(
            source=self.agent_id, action=action, payload=payload, trace_id=trace_id, refs=refs, target=target, priority=priority
        )
        if model_info is not None:
            event = event.model_copy(update={"model_info": model_info})
        await self._publish(subjects.agent_out(self.agent_type), event)

    async def _publish(self, subject: str, message: AgentMessage) -> None:
        await self.transport.publish(subject, message)
