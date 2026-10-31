"""总线网关：契约校验 + 幂等去重 + 能力路由 + 协同事务台账 + 时延埋点。

这是平台侧与智能体侧的唯一交汇点，《智能体接入规范》§1/§3/§5/§8 的机制实现：
- 上行/下行消息（含注册/心跳帧）100% 经 JSON Schema 校验，不合规拒收（E_SCHEMA_INVALID）；
- 动作必须在注册表内（E_UNREGISTERED_ACTION）；
- request→response 构成一次协同事务，成功/失败/超期在此记账 → 协同成功率指标；
- 上行消息 ts → 网关处理时刻 → 共享同步时延指标（≤3s）；
- 未知来源的心跳若携带完整 registration，就地补登记：注册帧丢一次（总线重连/网关重启）
  不应让本体永久不可见，而它随后发生的一切上行都会被当成"未知智能体"静默吞掉。
"""

from __future__ import annotations

import json
import logging
import time
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from functools import partial
from typing import Any

from aegis.bus import subjects
from aegis.bus.naming import consumer_name
from aegis.bus.registry import AgentRegistry
from aegis.bus.transport import BusTransport
from aegis.contracts import ContractRegistry
from aegis.domain.enums import Action, AgentType, MessageKind, RiskLevel
from aegis.domain.messages import (
    AgentMessage,
    CapabilityRegistration,
    Ref,
    TelemetryReading,
    make_request,
    now_iso,
    parse_iso,
    utc_now,
)
from aegis.errors import (
    AegisError,
    DeadlineExceededError,
    ErrorCode,
    NoCapableAgentError,
    UnregisteredActionError,
)
from aegis.observability import instrumentation
from aegis.observability.tracer import Tracer

log = logging.getLogger("aegis.bus.gateway")

ResultHandler = Callable[[AgentMessage], Awaitable[None]]


@dataclass(slots=True)
class TxnRecord:
    msg_id: str
    trace_id: str
    action: str
    agent_id: str | None
    outcome: str  # ok | timeout | error | no_agent
    latency_ms: float
    at: str
    error_code: str | None = None
    #: 仅 no_agent 有意义：no_agents / all_unhealthy / all_saturated / capability_missing / hazard_mismatch / mixed
    reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "msg_id": self.msg_id,
            "trace_id": self.trace_id,
            "action": self.action,
            "agent_id": self.agent_id,
            "outcome": self.outcome,
            "latency_ms": round(self.latency_ms, 3),
            "at": self.at,
            "error_code": self.error_code,
            "reason": self.reason,
        }


def _message_payload(message: AgentMessage) -> dict[str, Any]:
    return json.loads(message.model_dump_json(exclude_none=True))


class AgentGateway:
    platform_id = "platform.gateway"

    def __init__(
        self,
        transport: BusTransport,
        registry: AgentRegistry,
        contracts: ContractRegistry,
        tracer: Tracer,
        *,
        allow_unregistered_action: bool = False,
        default_deadline_ms: int = 10_000,
        dedup_capacity: int = 50_000,
        txn_capacity: int = 50_000,
    ) -> None:
        self._transport = transport
        self._registry = registry
        self._contracts = contracts
        self._tracer = tracer
        self._allow_unregistered = allow_unregistered_action
        self._default_deadline_ms = default_deadline_ms
        self._seen: OrderedDict[str, float] = OrderedDict()
        self._dedup_capacity = dedup_capacity
        self._txn: deque[TxnRecord] = deque(maxlen=txn_capacity)
        self._inbox_seq = 0
        self._subs: list[Any] = []
        self._result_handler: ResultHandler | None = None
        self.counters: dict[str, int] = {
            "inbound": 0,
            "outbound": 0,
            "rejected": 0,
            "duplicate": 0,
            "dropped_ttl": 0,
            "no_agent": 0,
            # 心跳自愈登记次数：注册帧丢失后靠心跳补登记的智能体，运营侧要能看见这条兜底腿真的跑过。
            "self_healed": 0,
        }

    # ---------- 生命周期 ----------

    async def start(self) -> None:
        for agent_type in AgentType:
            self._subs.append(
                await self._transport.subscribe(
                    subjects.agent_out(agent_type),
                    self._on_agent_out,
                    queue="aegis-gateway",
                    durable=consumer_name("cg_gateway", agent_type.value),
                )
            )
            self._subs.append(
                await self._transport.subscribe(
                    subjects.agent_hb(agent_type),
                    partial(self._on_heartbeat, expected_type=agent_type),
                )
            )
        log.info("网关已启动", extra={"subscriptions": len(self._subs)})

    async def close(self) -> None:
        for sub in self._subs:
            await sub.cancel()
        self._subs.clear()

    def set_result_handler(self, handler: ResultHandler | None) -> None:
        """注入上行结论处理器（pipeline 消费智能体产出）。传 None 解除。"""
        self._result_handler = handler

    @property
    def ledger(self) -> Tracer:
        return self._tracer

    # ---------- 校验与去重 ----------

    def validate(self, message: AgentMessage) -> None:
        self._contracts.validate_message(_message_payload(message))
        if not self._allow_unregistered and message.action not in _ALLOWED_ACTIONS:
            raise UnregisteredActionError(
                f"未注册动作: {message.action}",
                detail={"action": message.action},
            )

    def is_duplicate(self, msg_id: str) -> bool:
        if msg_id in self._seen:
            self.counters["duplicate"] += 1
            return True
        self._seen[msg_id] = time.monotonic()
        while len(self._seen) > self._dedup_capacity:
            self._seen.popitem(last=False)
        return False

    # ---------- 下行：平台 → 智能体 ----------

    def new_inbox(self) -> str:
        self._inbox_seq += 1
        return f"reply.gw{self._inbox_seq:08d}.inbox"

    async def dispatch(
        self,
        agent_type: AgentType,
        action: Action,
        payload: dict[str, Any],
        *,
        trace_id: str,
        capability: str | None = None,
        hazard_type: str | None = None,
        refs: list[Ref] | None = None,
        priority: int = 3,
        deadline_ms: int | None = None,
        ttl_ms: int = 30_000,
    ) -> AgentMessage:
        """发起一次协同事务。返回 response；失败抛类型化错误并同时记账。"""
        # 跨度在计时窗口之外创建、在账本落点之后结束：建跨度/写属性的开销不进入 collab_txn 样本，
        # SLA 判定仍以时延账本为准（跨度只提供可第三方引用的链路结构）。
        txn_span = instrumentation.start_span(
            "collab_txn",
            trace_id=trace_id,
            kind="client",
            action=action.value,
            agent_type=agent_type.value,
            capability=capability or "",
        )
        if (entry := self._registry.pick(agent_type.value, capability, hazard_type)) is None:
            # 失败必须可归因：零注册、全部失联、全部饱和、能力/灾种错位是四种不同的处置动作，
            # 压成一句"无可用智能体"就只能靠人翻日志猜。
            report = self._registry.explain(agent_type.value, capability, hazard_type)
            reason = report.reason.value
            message = f"无可用智能体: type={agent_type.value} capability={capability}（{report.summary()}）"
            self.counters["no_agent"] += 1
            self._record_txn(
                TxnRecord(
                    msg_id="-",
                    trace_id=trace_id,
                    action=action.value,
                    agent_id=None,
                    outcome="no_agent",
                    latency_ms=0.0,
                    at=now_iso(),
                    error_code=ErrorCode.NO_CAPABLE_AGENT.value,
                    reason=reason,
                )
            )
            instrumentation.mark_error(
                txn_span,
                message,
                code=ErrorCode.NO_CAPABLE_AGENT.value,
                outcome=f"no_agent:{reason}",
            )
            instrumentation.end_span(txn_span)
            raise NoCapableAgentError(
                message,
                detail={"agent_type": agent_type.value, "capability": capability, "diagnosis": report.as_dict()},
            )

        budget_ms = deadline_ms or self._default_deadline_ms
        request = make_request(
            source=self.platform_id,
            target=entry.agent_id,
            action=action.value,
            payload=payload,
            trace_id=trace_id,
            reply_to=self.new_inbox(),
            deadline_ms=budget_ms,
            refs=refs,
            priority=priority,
            ttl_ms=ttl_ms,
        )
        self.validate(request)
        self.counters["outbound"] += 1
        instrumentation.set_attributes(
            txn_span,
            span_attrs={
                instrumentation.MSG_ID_ATTR: request.msg_id,
                "aegis.contract.span_id": request.span_id or "",
                "agent_id": entry.agent_id,
            },
        )

        entry.inflight += 1
        started = time.perf_counter()
        try:
            # 派到被选中本体的专属收件箱：类型级 subject 是广播，N 个同类型智能体会各自执行一遍，
            # 而网关只取第一封回执——选谁只影响"谁的回信被采纳"，不影响"动作被做了几次"。
            response = await self._transport.request(subjects.agent_in(agent_type, entry.agent_id), request, timeout_ms=budget_ms)
        except DeadlineExceededError as timeout_exc:
            latency = (time.perf_counter() - started) * 1000
            entry.failed += 1
            self._record_txn(
                TxnRecord(
                    msg_id=request.msg_id,
                    trace_id=trace_id,
                    action=action.value,
                    agent_id=entry.agent_id,
                    outcome="timeout",
                    latency_ms=latency,
                    at=now_iso(),
                    error_code=ErrorCode.TIMEOUT.value,
                )
            )
            self._tracer.record("collab_txn", latency, trace_id=trace_id, outcome="timeout", agent_id=entry.agent_id)
            instrumentation.record_error(
                txn_span,
                timeout_exc,
                code=ErrorCode.TIMEOUT.value,
                outcome="timeout",
                latency_ms=round(latency, 3),
            )
            instrumentation.end_span(txn_span)
            raise
        finally:
            entry.inflight = max(0, entry.inflight - 1)

        latency = (time.perf_counter() - started) * 1000
        self.validate(response)
        self.counters["inbound"] += 1
        # 回信 msg_id 作为链接（不是父子）：智能体侧若另起链路，取证时仍能从本次事务直接跳过去
        instrumentation.add_link(txn_span, response.msg_id, trace_id)
        # 共享同步时延（≤3s 指标）：智能体发出时刻 → 网关接收处理时刻，响应与事件同口径
        self._tracer.record(
            "sync_agent_to_gateway_ms",
            max((utc_now() - parse_iso(response.ts)).total_seconds() * 1000, 0.0),
            trace_id=trace_id,
            agent_id=entry.agent_id,
        )

        if response.kind is MessageKind.ERROR:
            entry.failed += 1
            self._record_txn(
                TxnRecord(
                    msg_id=request.msg_id,
                    trace_id=trace_id,
                    action=action.value,
                    agent_id=entry.agent_id,
                    outcome="error",
                    latency_ms=latency,
                    at=now_iso(),
                    error_code=str(response.payload.get("code")),
                )
            )
            self._tracer.record("collab_txn", latency, trace_id=trace_id, outcome="error", agent_id=entry.agent_id)
            instrumentation.mark_error(
                txn_span,
                str(response.payload.get("message") or "智能体返回错误"),
                code=str(response.payload.get("code")),
                outcome="error",
                latency_ms=round(latency, 3),
                reply_msg_id=response.msg_id,
            )
            instrumentation.end_span(txn_span)
            raise AegisError(
                f"智能体返回错误: {response.payload.get('message')}",
                detail={"code": response.payload.get("code"), "agent_id": entry.agent_id},
                retryable=bool(response.payload.get("retryable")),
            )

        entry.served += 1
        self._record_txn(
            TxnRecord(
                msg_id=request.msg_id,
                trace_id=trace_id,
                action=action.value,
                agent_id=entry.agent_id,
                outcome="ok",
                latency_ms=latency,
                at=now_iso(),
            )
        )
        self._tracer.record("collab_txn", latency, trace_id=trace_id, outcome="ok", agent_id=entry.agent_id)
        instrumentation.set_attributes(txn_span, outcome="ok", reply_msg_id=response.msg_id, latency_ms=round(latency, 3))
        instrumentation.end_span(txn_span)
        return response

    async def publish_to(self, subject: str, message: AgentMessage) -> None:
        """向指定 subject 广播（大屏/边缘订阅方使用）。"""
        self.validate(message)
        await self._transport.publish(subject, message)
        self.counters["outbound"] += 1

    async def publish_alert(self, risk_level: RiskLevel, region_code: str, message: AgentMessage) -> None:
        await self.publish_to(subjects.alert(int(risk_level), region_code), message)

    async def publish_telemetry(self, reading: TelemetryReading, message: AgentMessage) -> None:
        """遥测上总线；同时记录"观测→入库→发布"两段时延，供 ≤5min 接入指标归因。

        每读数热点刻意不建跨度：只做进程内聚合计数（O(1)、零网络、零对象分配之外的开销），
        跨度证据留在事务级（协同事务/上行事件），避免万级读数把链路淹成噪声。
        """
        instrumentation.count_hot_path("telemetry_publish")
        self.validate(message)
        await self._transport.publish(subjects.data(reading.station_id, reading.metric), message)
        self.counters["outbound"] += 1
        observed = parse_iso(reading.observed_at)
        ingested = parse_iso(reading.ingested_at)
        published = utc_now()
        self._tracer.record(
            "ingest_store_ms",
            max((ingested - observed).total_seconds() * 1000, 0.0),
            trace_id=message.trace_id,
        )
        self._tracer.record(
            "ingest_publish_ms",
            max((published - ingested).total_seconds() * 1000, 0.0),
            trace_id=message.trace_id,
        )

    # ---------- 上行：智能体 → 平台 ----------

    async def _on_heartbeat(self, message: AgentMessage, *, expected_type: AgentType | None = None) -> None:
        if message.action not in (Action.AGENT_REGISTER.value, Action.AGENT_HEARTBEAT.value):
            return
        try:
            self.validate(message)
        except AegisError as exc:
            self.counters["rejected"] += 1
            log.warning(
                "生命周期消息被拒",
                extra={"agent_id": message.source, "action": message.action, "reason": exc.message},
            )
            return

        if message.action == Action.AGENT_REGISTER.value:
            self._register_lifecycle(message, expected_type=expected_type)
            return

        if self._registry.heartbeat(message.source):
            return
        # 心跳由 AgentKit/Mock 携带完整 registration。注册帧只在启动时发一次，丢在重连窗口里
        # 就再也没有第二次机会——本体会一直有心跳、却永远进不了候选集，所有派单都记 no_agent。
        if self._register_lifecycle(message, expected_type=expected_type, via_heartbeat=True):
            self._registry.heartbeat(message.source)
            self.counters["self_healed"] += 1
            log.warning(
                "未知智能体心跳，已按注册载荷补登记",
                extra={
                    "agent_id": message.source,
                    "agent_type": expected_type.value if expected_type is not None else None,
                },
            )

    def _register_lifecycle(
        self,
        message: AgentMessage,
        *,
        expected_type: AgentType | None,
        via_heartbeat: bool = False,
    ) -> bool:
        """从注册/心跳帧的 payload 登记本体；载荷与来源对不上就拒收，绝不按来源猜登记。"""
        label = "心跳" if via_heartbeat else "注册"
        raw = message.payload.get("registration")
        if not isinstance(raw, dict):
            if via_heartbeat:
                # 规范只要求启动期 register 带能力声明；老接入方的心跳可能只有空 payload，
                # 这属于"无法自愈"而不是违约，保持原有静默，不计入 rejected。
                log.debug("未知智能体心跳未携带 registration，无法补登记", extra={"agent_id": message.source})
            else:
                self.counters["rejected"] += 1
                log.warning(f"{label}载荷缺失 registration 字段", extra={"agent_id": message.source})
            return False
        try:
            registration = CapabilityRegistration.model_validate(raw)
        except Exception as exc:
            self.counters["rejected"] += 1
            log.warning(f"{label}载荷非法", extra={"agent_id": message.source, "error": str(exc)})
            return False
        if registration.agent_id != message.source or (expected_type is not None and registration.agent_type != expected_type.value):
            self.counters["rejected"] += 1
            log.warning(
                f"{label}声明与来源不符",
                extra={
                    "agent_id": message.source,
                    "declared_id": registration.agent_id,
                    "declared_type": registration.agent_type,
                    "subject_type": expected_type.value if expected_type is not None else None,
                },
            )
            return False
        self._registry.register(registration)
        return True

    async def _on_agent_out(self, message: AgentMessage) -> None:
        # 智能体 → 平台这一腿单独成跨度：它跑在订阅任务里、没有活动父节点，
        # 靠契约 trace_id 接续到同一条链路，于是"平台→智能体→平台"在 Jaeger 里落在同一个 traceID 下。
        uplink_span = instrumentation.start_span(
            "agent_uplink",
            trace_id=message.trace_id,
            kind="server",
            span_attrs={
                instrumentation.MSG_ID_ATTR: message.msg_id,
                "aegis.contract.span_id": message.span_id or "",
                "aegis.agent_id": message.source,
                "aegis.action": message.action,
                "aegis.kind": message.kind.value,
            },
        )
        self.counters["inbound"] += 1
        try:
            self.validate(message)
        except AegisError as exc:
            self.counters["rejected"] += 1
            log.warning("上行消息被拒", extra={"reason": exc.message, "msg_id": message.msg_id})
            instrumentation.record_error(uplink_span, exc, outcome="rejected")
            instrumentation.end_span(uplink_span)
            return

        if message.ttl_ms and parse_iso(message.ts) + timedelta(seconds=message.ttl_ms / 1000) < utc_now():
            self.counters["dropped_ttl"] += 1
            log.debug("超期消息丢弃", extra={"msg_id": message.msg_id})
            # 丢弃是网关的正常工作结果（幂等/超期治理），记为降级而不是故障
            instrumentation.mark_degraded(uplink_span, "超期丢弃（ttl_ms 已过）", outcome="dropped_ttl")
            instrumentation.end_span(uplink_span)
            return

        if self.is_duplicate(message.msg_id):
            log.debug("重复消息幂等丢弃", extra={"msg_id": message.msg_id})
            instrumentation.mark_degraded(uplink_span, "幂等去重丢弃（msg_id 重复）", outcome="duplicate")
            instrumentation.end_span(uplink_span)
            return

        sync_ms = max((utc_now() - parse_iso(message.ts)).total_seconds() * 1000, 0.0)
        self._tracer.record(
            "sync_agent_to_gateway_ms",
            sync_ms,
            trace_id=message.trace_id,
            agent_id=message.source,
        )
        self._registry.heartbeat(message.source)

        instrumentation.set_attributes(uplink_span, sync_ms=round(sync_ms, 3), outcome="ok")
        if self._result_handler is not None:
            await self._result_handler(message)
        instrumentation.end_span(uplink_span)

    # ---------- 台账 ----------

    def _record_txn(self, record: TxnRecord) -> None:
        self._txn.append(record)

    @property
    def transactions(self) -> list[TxnRecord]:
        return list(self._txn)

    def success_rate(self, *, window: int | None = None) -> float | None:
        records = list(self._txn)[-window:] if window else list(self._txn)
        if not records:
            return None
        return sum(1 for r in records if r.outcome == "ok") / len(records)

    def snapshot(self) -> dict[str, Any]:
        return {
            "counters": dict(self.counters),
            "transactions": len(self._txn),
            "success_rate": self.success_rate(),
            "agents": self._registry.snapshot(),
            "agents_online": self._registry.online_count,
            "latency": self._tracer.ledger.snapshot(),
        }


_ALLOWED_ACTIONS: frozenset[str] = frozenset(a.value for a in Action)
