"""参考/占位智能体：严格按契约实现的 Mock 智能体集。

三重用途（对应《智能体接入规范》§7）：
1. 平台侧独立开发与联调——不等智能体方进度；
2. 端到端演示与压测的基线载荷；
3. 一致性测试的合格样例，以及可注入违约行为（超时/非法载荷/静默/异常）的不合格样例。
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
from dataclasses import dataclass, field
from typing import Any

from aegis.bus import subjects
from aegis.bus.naming import consumer_name
from aegis.bus.transport import BusTransport
from aegis.domain.enums import Action, AgentType, HazardType, MessageKind, RiskLevel
from aegis.domain.messages import (
    AgentMessage,
    CapabilityRegistration,
    TriggerHit,
    make_error,
    make_response,
)
from aegis.services.risk_engine import RiskVerdict
from aegis.services.task_parser import TaskParser

log = logging.getLogger("aegis.agents.mock")

_EVENT_ID_RE = re.compile(r"^evt_[0-9a-f]{12}$")


class Misbehavior:
    NONE = "none"
    LATE = "late"  # 超过 deadline 才回复 → 网关应判 E_TIMEOUT
    SILENT = "silent"  # 完全不回复
    INVALID = "invalid"  # 回复载荷不合契约
    CRASH = "crash"  # 回 error.raise


@dataclass
class MockAgent:
    """单个智能体实例：注册 → 心跳 → 消费 in → 产出 out。"""

    transport: BusTransport
    agent_type: AgentType
    agent_id: str
    capabilities: list[str]
    hazard_types: list[str] = field(default_factory=lambda: [h.value for h in HazardType if h is not HazardType.UNKNOWN])
    latency_ms: float = 30.0
    fail_rate: float = 0.0
    misbehavior: str = Misbehavior.NONE
    max_concurrency: int = 4
    seed: int | None = None
    version: str = "mock-1.0.0"
    handled: int = 0
    duplicates_skipped: int = 0
    _seen_ids: dict[str, None] = field(default_factory=dict, repr=False)
    _dedup_capacity: int = 4_096
    _subs: list[Any] = field(default_factory=list)
    _hb_task: asyncio.Task[None] | None = None
    _rng: random.Random = field(default=None)  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self._rng = random.Random(self.seed)

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
        await self.transport.publish(subjects.agent_hb(self.agent_type), self._frame(Action.AGENT_REGISTER))
        self._subs.append(
            await self.transport.subscribe(
                subjects.agent_in(self.agent_type),
                self._on_request,
                # 名字里带 agent_id，而 agent_id 可以有点（`perceive.mock01`）：
                # JetStream 的消费者名校验会直接拒掉，内存总线却不报错——见 bus/naming.py。
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

    def _frame(self, action: Action) -> AgentMessage:
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
                await self.transport.publish(subjects.agent_hb(self.agent_type), self._frame(Action.AGENT_HEARTBEAT))
        except asyncio.CancelledError:
            return

    async def _on_request(self, request: AgentMessage) -> None:
        # 接收方按 msg_id 幂等去重（规范 §1）：重复投递不得产生二次副作用
        if request.msg_id in self._seen_ids:
            self.duplicates_skipped += 1
            return
        self._seen_ids[request.msg_id] = None
        while len(self._seen_ids) > self._dedup_capacity:
            self._seen_ids.pop(next(iter(self._seen_ids)), None)

        self.handled += 1
        reply_subject = request.reply_to or subjects.agent_out(self.agent_type)

        if self.misbehavior == Misbehavior.SILENT:
            return
        if self.misbehavior == Misbehavior.CRASH:
            await self._publish(
                reply_subject,
                make_error(
                    request,
                    source=self.agent_id,
                    code="E_INTERNAL",
                    message="mock 智能体内部异常",
                    retryable=True,
                ),
            )
            return

        await asyncio.sleep(self.latency_ms / 1000)
        if self._rng.random() < self.fail_rate:
            await self._publish(
                reply_subject,
                make_error(
                    request,
                    source=self.agent_id,
                    code="E_INTERNAL",
                    message="mock 随机失败",
                    retryable=True,
                ),
            )
            return

        if self.misbehavior == Misbehavior.LATE:
            await asyncio.sleep(max(request.deadline_ms or 1_000, 1_000) * 1.5 / 1000)

        payload = {"unexpected": "shape"} if self.misbehavior == Misbehavior.INVALID else self._respond(request)
        await self._publish(reply_subject, make_response(request, source=self.agent_id, payload=payload))

    async def _publish(self, subject: str, message: AgentMessage) -> None:
        await self.transport.publish(subject, message)

    def _respond(self, request: AgentMessage) -> dict[str, Any]:
        if self.agent_type is AgentType.ASSESS:
            return self._assess(request)
        if self.agent_type is AgentType.PLAN:
            return self._plan(request)
        if self.agent_type is AgentType.EXECUTE:
            return self._execute(request)
        if self.agent_type is AgentType.PERCEIVE:
            return {"anomalies": []}
        return {"status": "ok", "warning_id": request.payload.get("warning_id", "")}

    def _assess(self, request: AgentMessage) -> dict[str, Any]:
        hits = request.payload.get("hits", [])
        first = hits[0] if isinstance(hits, list) and hits else {}
        level = RiskLevel.RED if isinstance(hits, list) and len(hits) > 1 else RiskLevel.ORANGE
        return {
            "hazard_type": str(first.get("hazard_type", HazardType.DEBRIS_FLOW.value)),
            "risk_level": int(level),
            "confidence": 0.82,
            "rationale": f"mock 研判：{len(hits)} 条触发命中",
            "assessed_by": self.agent_id,
        }

    def _plan(self, request: AgentMessage) -> dict[str, Any]:
        risk = request.payload.get("risk") or {}
        if not isinstance(risk, dict):
            return {"task_units": []}
        try:
            hazard = HazardType(str(risk.get("hazard_type", HazardType.DEBRIS_FLOW.value)))
            region = str(risk.get("region_code", "540100A1"))
            level = RiskLevel(int(risk.get("risk_level", int(RiskLevel.ORANGE))))
            confidence = float(risk.get("confidence", 0.7))
        except (ValueError, TypeError):
            return {"task_units": []}

        evidence = [e for e in risk.get("evidence_refs", []) if isinstance(e, str)]
        hits = [
            TriggerHit(
                rule_id=item if item.startswith("R-") else f"R-MOCK-{index}",
                hazard_type=hazard.value,
                region_code=region,
                score=0.7,
            )
            for index, item in enumerate(evidence)
        ] or [TriggerHit(rule_id="R-MOCK-DEFAULT", hazard_type=hazard.value, region_code=region, score=0.6)]
        verdict = RiskVerdict(
            hazard_type=hazard,
            region_code=region,
            risk_level=level,
            confidence=confidence,
            rationale=str(risk.get("rationale", "mock 拆解"))[:500],
            hits=hits,
        )
        raw_event = str(request.payload.get("event_id", ""))
        event_id = raw_event if _EVENT_ID_RE.match(raw_event) else None
        units = TaskParser().parse(verdict, event_id=event_id)
        return {"task_units": [{**unit.model_dump(exclude_none=True), "created_by": self.agent_id} for unit in units]}

    def _execute(self, request: AgentMessage) -> dict[str, Any]:
        channels = request.payload.get("channels", [])
        audience_count = len(request.payload.get("audiences", []))
        return {
            "warning_id": str(request.payload.get("warning_id", "")),
            "channel_results": [
                {
                    "channel": str(channel),
                    "audience_count": audience_count,
                    "status": "delivered",
                    "provider_msg_id": f"mock-{self.agent_id}-{channel}",
                }
                for channel in channels
            ],
        }


async def publish_response(transport: BusTransport, message: AgentMessage, reply_to: str) -> None:
    await transport.publish(reply_to, message)


async def start_mock_agents(
    transport: BusTransport,
    *,
    latency_ms: float = 20.0,
    fail_rate: float = 0.0,
    misbehavior: dict[AgentType, str] | None = None,
    heartbeat_interval: float = 0.5,
) -> list[MockAgent]:
    """拉起五类智能体的 Mock 实例。注意：响应回投递需 request.reply_to，见 MockAgent._on_request。"""
    misbehavior = misbehavior or {}
    specs = [
        (AgentType.PERCEIVE, ["anomaly_detect", "trigger_recognize"]),
        (AgentType.ASSESS, ["risk_assess"]),
        (AgentType.PLAN, ["task_decompose"]),
        (AgentType.EXECUTE, ["warn_publish"]),
        (AgentType.FEEDBACK, ["reach_track"]),
    ]
    agents = [
        MockAgent(
            transport=transport,
            agent_type=agent_type,
            agent_id=f"{agent_type.value}.mock01",
            capabilities=caps,
            latency_ms=latency_ms,
            fail_rate=fail_rate,
            misbehavior=misbehavior.get(agent_type, Misbehavior.NONE),
            seed=abs(hash(agent_type.value)) % 65_536,
        )
        for agent_type, caps in specs
    ]
    for agent in agents:
        await agent.start(heartbeat_interval=heartbeat_interval)
    return agents
