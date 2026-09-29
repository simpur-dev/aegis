"""总线网关：契约校验 + 幂等去重 + 能力路由 + 协同事务台账 + 时延埋点。

这是平台侧与智能体侧的唯一交汇点，《智能体接入规范》§1/§3/§5/§8 的机制实现：
- 上行/下行消息 100% 经 JSON Schema 校验，不合规拒收（E_SCHEMA_INVALID）；
- 动作必须在注册表内（E_UNREGISTERED_ACTION）；
- request→response 构成一次协同事务，成功/失败/超期在此记账 → 协同成功率指标；
- 上行消息 ts → 网关处理时刻 → 共享同步时延指标（≤3s）。
"""

from __future__ import annotations

import json
import logging
import time
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from aegis.bus import subjects
from aegis.bus.registry import AgentRegistry
from aegis.bus.transport import BusTransport
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
    SchemaInvalidError,
    UnregisteredActionError,
)
from aegis.observability.tracer import Tracer

log = logging.getLogger("aegis.bus.gateway")

_DEFAULT_CONTRACTS_DIR = Path(__file__).resolve().parents[4] / "contracts"

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
        }


class ContractRegistry:
    """加载并缓存契约 Schema；校验错误只取首要路径，便于回执定位。"""

    def __init__(self, contracts_dir: Path | str | None = None) -> None:
        root = Path(contracts_dir or Path(__file__).resolve().parents[4] / "contracts")
        if not root.is_dir():
            raise FileNotFoundError(f"契约目录不存在: {root}")
        self._root = root
        self._message = _load_validator(root / "agent_message.v1.schema.json")
        self._stu = _load_validator(root / "stu.v1.schema.json")

    @property
    def contracts_dir(self) -> Path:
        return self._root

    def validate_message(self, data: dict[str, Any]) -> None:
        if error := _first_error(self._message, data):
            raise SchemaInvalidError(
                f"消息不合契约: {error.message}",
                detail={"path": error.json_path, "schema": "agent_message.v1"},
            )

    def validate_stu(self, data: dict[str, Any]) -> None:
        if error := _first_error(self._stu, data):
            raise SchemaInvalidError(
                f"STU 不合契约: {error.message}",
                detail={"path": error.json_path, "schema": "stu.v1"},
            )

    def stu_errors(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """批量校验：返回 [{index, path, error}]，全空即合法。"""
        found: list[dict[str, Any]] = []
        for index, item in enumerate(items):
            if error := _first_error(self._stu, item):
                found.append({"index": index, "path": error.json_path, "error": error.message})
        return found


def _load_validator(path: Path) -> Draft202012Validator:
    schema = json.loads(path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def _first_error(validator: Draft202012Validator, data: dict[str, Any]) -> Any | None:
    errors = sorted(validator.iter_errors(data), key=lambda e: e.json_path)
    return errors[0] if errors else None


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
        }

    # ---------- 生命周期 ----------

    async def start(self) -> None:
        for agent_type in AgentType:
            self._subs.append(
                await self._transport.subscribe(
                    subjects.agent_out(agent_type),
                    self._on_agent_out,
                    queue="aegis-gateway",
                    durable=f"cg_gateway_{agent_type.value}",
                )
            )
            self._subs.append(await self._transport.subscribe(subjects.agent_hb(agent_type), self._on_heartbeat))
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
        if (entry := self._registry.pick(agent_type.value, capability, hazard_type)) is None:
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
                )
            )
            raise NoCapableAgentError(
                f"无可用智能体: type={agent_type.value} capability={capability}",
                detail={"agent_type": agent_type.value, "capability": capability},
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

        entry.inflight += 1
        started = time.perf_counter()
        try:
            response = await self._transport.request(subjects.agent_in(agent_type), request, timeout_ms=budget_ms)
        except DeadlineExceededError:
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
            raise
        finally:
            entry.inflight = max(0, entry.inflight - 1)

        latency = (time.perf_counter() - started) * 1000
        self.validate(response)
        self.counters["inbound"] += 1
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
        return response

    async def publish_to(self, subject: str, message: AgentMessage) -> None:
        """向指定 subject 广播（大屏/边缘订阅方使用）。"""
        self.validate(message)
        await self._transport.publish(subject, message)
        self.counters["outbound"] += 1

    async def publish_alert(self, risk_level: RiskLevel, region_code: str, message: AgentMessage) -> None:
        await self.publish_to(subjects.alert(int(risk_level), region_code), message)

    async def publish_telemetry(self, reading: TelemetryReading, message: AgentMessage) -> None:
        """遥测上总线；同时记录"观测→入库→发布"两段时延，供 ≤5min 接入指标归因。"""
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

    async def _on_heartbeat(self, message: AgentMessage) -> None:
        if message.action == Action.AGENT_REGISTER.value:
            raw = message.payload.get("registration")
            if not isinstance(raw, dict):
                log.warning("注册载荷缺失 registration 字段", extra={"agent_id": message.source})
                return
            try:
                self._registry.register(CapabilityRegistration.model_validate(raw))
            except Exception as exc:
                self.counters["rejected"] += 1
                log.warning("智能体注册载荷非法", extra={"agent_id": message.source, "error": str(exc)})
            return
        if message.action != Action.AGENT_HEARTBEAT.value:
            return
        if not self._registry.heartbeat(message.source):
            log.debug("未知智能体心跳", extra={"agent_id": message.source})

    async def _on_agent_out(self, message: AgentMessage) -> None:
        self.counters["inbound"] += 1
        try:
            self.validate(message)
        except AegisError as exc:
            self.counters["rejected"] += 1
            log.warning("上行消息被拒", extra={"reason": exc.message, "msg_id": message.msg_id})
            return

        if message.ttl_ms and parse_iso(message.ts) + timedelta(seconds=message.ttl_ms / 1000) < utc_now():
            self.counters["dropped_ttl"] += 1
            log.debug("超期消息丢弃", extra={"msg_id": message.msg_id})
            return

        if self.is_duplicate(message.msg_id):
            log.debug("重复消息幂等丢弃", extra={"msg_id": message.msg_id})
            return

        sync_ms = max((utc_now() - parse_iso(message.ts)).total_seconds() * 1000, 0.0)
        self._tracer.record(
            "sync_agent_to_gateway_ms",
            sync_ms,
            trace_id=message.trace_id,
            agent_id=message.source,
        )
        self._registry.heartbeat(message.source)

        if self._result_handler is not None:
            await self._result_handler(message)

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
