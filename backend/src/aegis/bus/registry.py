"""能力注册中心：智能体声明能力、心跳保活、按负载路由（规范 §6）。

心跳判定用单调时钟；失联时触发降级回调，由平台侧走 fallback_policy，而不是让任务悬在总线上。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from aegis.config import Settings
from aegis.domain.messages import CapabilityRegistration
from aegis.errors import AegisError, ErrorCode

log = logging.getLogger("aegis.bus.registry")

UnhealthyCallback = Callable[[str, str], Awaitable[None]]


class AgentOfflineError(AegisError):
    code = ErrorCode.NO_CAPABLE_AGENT


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

    def can_serve(self, capability: str | None, hazard_type: str | None) -> bool:
        """健康、未饱和、能力与灾种域均匹配时才可接单。"""
        return (
            self.healthy
            and self.inflight < self.reg.max_concurrency
            and (not capability or capability in self.reg.capabilities)
            and (not hazard_type or not self.reg.hazard_types or hazard_type in self.reg.hazard_types)
        )


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
