"""能力注册中心：智能体声明能力、心跳保活、按负载路由（规范 §6）。

心跳判定用单调时钟；失联只做两件事——标记 unhealthy、从候选集摘除。平台当前不注册
`on_unhealthy` 回调，降级发生在派单侧：网关按 `SelectionReport.reason` 记账并抛
`NoCapableAgentError`，工作流再按节点/STU 的降级策略处置。回调接口留给需要即时通知的装配方。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum

from aegis.config import Settings
from aegis.domain.messages import CapabilityRegistration
from aegis.errors import AegisError, ErrorCode

log = logging.getLogger("aegis.bus.registry")

UnhealthyCallback = Callable[[str, str], Awaitable[None]]


class AgentOfflineError(AegisError):
    code = ErrorCode.NO_CAPABLE_AGENT


class ServeBlocker(StrEnum):
    """一个在册智能体接不了单的具体原因；一个本体可以同时命中多条。"""

    UNHEALTHY = "unhealthy"
    SATURATED = "saturated"
    CAPABILITY = "capability"
    HAZARD = "hazard"


class SelectionReason(StrEnum):
    """选择失败的**主导**原因：多因并存时就是 `MIXED`，完整计数看 `SelectionReport.blockers`。"""

    AVAILABLE = "available"
    NO_AGENTS = "no_agents"
    ALL_UNHEALTHY = "all_unhealthy"
    ALL_SATURATED = "all_saturated"
    CAPABILITY_MISSING = "capability_missing"
    HAZARD_MISMATCH = "hazard_mismatch"
    MIXED = "mixed"


#: 主导原因的判定次序：每条阻挡原因在**全部**候选上成立才算数；多条同时普及时取先出现者。
#: 失联排在饱和前（本体根本没在跑），能力排在灾种前（更常见的配置错位）。
_UNIVERSAL_REASONS: tuple[tuple[ServeBlocker, SelectionReason], ...] = (
    (ServeBlocker.UNHEALTHY, SelectionReason.ALL_UNHEALTHY),
    (ServeBlocker.SATURATED, SelectionReason.ALL_SATURATED),
    (ServeBlocker.CAPABILITY, SelectionReason.CAPABILITY_MISSING),
    (ServeBlocker.HAZARD, SelectionReason.HAZARD_MISMATCH),
)

_BLOCKER_LABELS: dict[str, str] = {
    ServeBlocker.UNHEALTHY: "失联",
    ServeBlocker.SATURATED: "饱和",
    ServeBlocker.CAPABILITY: "缺能力",
    ServeBlocker.HAZARD: "灾种不匹配",
}


@dataclass(slots=True)
class AgentEntry:
    reg: CapabilityRegistration
    registered_mono: float
    last_hb_mono: float
    misses: int = 0
    inflight: int = 0
    healthy: bool = True
    served: int = 0
    failed: int = 0

    @property
    def agent_id(self) -> str:
        return self.reg.agent_id

    def serve_blockers(self, capability: str | None = None, hazard_type: str | None = None) -> tuple[ServeBlocker, ...]:
        """列出全部不可接单原因（而不是在第一个条件上短路）。

        健康、容量、能力、灾种是四类**不同**的运营事实：合并成一个布尔之后，
        "全部失联"与"能力配错"在调用侧长得一模一样，只能靠人去猜。
        """
        blockers: list[ServeBlocker] = []
        if not self.healthy:
            blockers.append(ServeBlocker.UNHEALTHY)
        if self.inflight >= self.reg.max_concurrency:
            blockers.append(ServeBlocker.SATURATED)
        if capability and capability not in self.reg.capabilities:
            blockers.append(ServeBlocker.CAPABILITY)
        if hazard_type and self.reg.hazard_types and hazard_type not in self.reg.hazard_types:
            blockers.append(ServeBlocker.HAZARD)
        return tuple(blockers)

    def can_serve(self, capability: str | None = None, hazard_type: str | None = None) -> bool:
        """健康、未饱和、能力与灾种域均匹配时才可接单。"""
        return not self.serve_blockers(capability, hazard_type)


@dataclass(frozen=True, slots=True)
class SelectionReport:
    """一次候选筛选的诊断快照：`registered` 里为什么 `available` 是 0。"""

    agent_type: str
    capability: str | None
    hazard_type: str | None
    registered: int
    available: int
    blockers: dict[ServeBlocker, int]

    @property
    def reason(self) -> SelectionReason:
        if self.registered == 0:
            return SelectionReason.NO_AGENTS
        if self.available > 0:
            return SelectionReason.AVAILABLE
        universal = {blocker for blocker, count in self.blockers.items() if count == self.registered}
        for blocker, reason in _UNIVERSAL_REASONS:
            if blocker in universal:
                return reason
        return SelectionReason.MIXED

    def summary(self) -> str:
        if self.registered == 0:
            return "该类型智能体未注册"
        head = f"注册 {self.registered} 个，可用 {self.available} 个"
        if not self.blockers:
            return head
        detail = "、".join(f"{_BLOCKER_LABELS.get(blocker, blocker)} {count}" for blocker, count in self.blockers.items())
        return f"{head}（{detail}）"

    def as_dict(self) -> dict[str, object]:
        return {
            "agent_type": self.agent_type,
            "capability": self.capability,
            "hazard_type": self.hazard_type,
            "registered": self.registered,
            "available": self.available,
            "blockers": {blocker.value: count for blocker, count in self.blockers.items()},
            "reason": self.reason.value,
        }


class AgentRegistry:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._entries: dict[str, AgentEntry] = {}
        self._callbacks: list[UnhealthyCallback] = []
        self._sweeper: asyncio.Task[None] | None = None

    def register(self, reg: CapabilityRegistration) -> AgentEntry:
        now = time.monotonic()
        entry = AgentEntry(reg=reg, registered_mono=now, last_hb_mono=now)
        self._entries[reg.agent_id] = entry
        log.info("智能体注册", extra={"agent_id": reg.agent_id, "capabilities": reg.capabilities})
        return entry

    def heartbeat(self, agent_id: str) -> bool:
        entry = self._entries.get(agent_id)
        if entry is None:
            return False
        entry.last_hb_mono = time.monotonic()
        if not entry.healthy:
            entry.healthy = True
            entry.misses = 0
            log.info("智能体恢复", extra={"agent_id": agent_id})
        return True

    def unregister(self, agent_id: str) -> AgentEntry | None:
        return self._entries.pop(agent_id, None)

    def on_unhealthy(self, callback: UnhealthyCallback) -> None:
        self._callbacks.append(callback)

    def candidates(
        self,
        agent_type: str,
        capability: str | None = None,
        hazard_type: str | None = None,
    ) -> list[AgentEntry]:
        return sorted(
            (e for e in self._entries.values() if e.reg.agent_type == agent_type and e.can_serve(capability, hazard_type)),
            key=lambda e: (e.inflight, -e.served),
        )

    def pick(
        self,
        agent_type: str,
        capability: str | None = None,
        hazard_type: str | None = None,
    ) -> AgentEntry | None:
        if options := self.candidates(agent_type, capability, hazard_type):
            return options[0]
        return None

    def explain(
        self,
        agent_type: str,
        capability: str | None = None,
        hazard_type: str | None = None,
    ) -> SelectionReport:
        """筛选失败时回答"为什么"：把四类阻挡原因分开计数，供网关错误与链路降级留痕。"""
        entries = [e for e in self._entries.values() if e.reg.agent_type == agent_type]
        blockers: Counter[ServeBlocker] = Counter()
        available = 0
        for entry in entries:
            found = entry.serve_blockers(capability, hazard_type)
            if found:
                blockers.update(found)
            else:
                available += 1
        ordered = {blocker: blockers[blocker] for blocker, _ in _UNIVERSAL_REASONS if blockers[blocker]}
        return SelectionReport(
            agent_type=agent_type,
            capability=capability,
            hazard_type=hazard_type,
            registered=len(entries),
            available=available,
            blockers=ordered,
        )

    def get(self, agent_id: str) -> AgentEntry | None:
        return self._entries.get(agent_id)

    def sweep(self) -> list[str]:
        """把超过 miss_limit 个心跳周期的智能体标记为失联，返回新失联的 agent_id。"""
        limit = self._settings.heartbeat_interval_seconds * (self._settings.heartbeat_miss_limit + 1)
        now = time.monotonic()
        lost: list[str] = []
        for entry in self._entries.values():
            if not entry.healthy:
                continue
            if (idle := now - entry.last_hb_mono) > limit:
                entry.healthy = False
                entry.misses += 1
                lost.append(entry.agent_id)
                log.warning("智能体失联", extra={"agent_id": entry.agent_id, "idle_seconds": round(idle, 2)})
        return lost

    async def sweep_and_notify(self) -> list[str]:
        lost = self.sweep()
        for agent_id in lost:
            for callback in self._callbacks:
                try:
                    await callback(agent_id, "heartbeat_lost")
                except Exception:  # 回调异常不得中断健康巡检
                    log.exception("失联回调异常", extra={"agent_id": agent_id})
        return lost

    async def start_sweeper(self) -> None:
        if self._sweeper is not None:
            return
        self._sweeper = asyncio.create_task(self._loop(), name="registry-sweeper")

    async def stop_sweeper(self) -> None:
        if self._sweeper is None:
            return
        self._sweeper.cancel()
        await asyncio.gather(self._sweeper, return_exceptions=True)
        self._sweeper = None

    async def _loop(self) -> None:
        interval = max(0.2, self._settings.heartbeat_interval_seconds)
        try:
            while True:
                await asyncio.sleep(interval)
                await self.sweep_and_notify()
        except asyncio.CancelledError:
            return

    def snapshot(self) -> list[dict[str, object]]:
        return [
            {
                "agent_id": e.agent_id,
                "agent_type": e.reg.agent_type,
                "capabilities": e.reg.capabilities,
                "hazard_types": e.reg.hazard_types,
                "healthy": e.healthy,
                "inflight": e.inflight,
                "max_concurrency": e.reg.max_concurrency,
                "served": e.served,
                "failed": e.failed,
                "version": e.reg.version,
                "idle_seconds": round(time.monotonic() - e.last_hb_mono, 2),
            }
            for e in sorted(self._entries.values(), key=lambda x: x.agent_id)
        ]

    @property
    def online_count(self) -> int:
        return sum(1 for e in self._entries.values() if e.healthy)
