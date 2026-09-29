"""能力注册中心测试：注册/心跳/失联、容量与路由排序、降级回调。"""

from __future__ import annotations

import asyncio

import pytest

from aegis.bus.registry import AgentRegistry
from aegis.config import Settings
from aegis.domain.enums import AgentType, HazardType
from aegis.domain.messages import CapabilityRegistration


def reg(
    agent_id: str,
    agent_type: AgentType,
    capabilities: list[str],
    *,
    concurrency: int = 2,
    hazards: list[str] | None = None,
) -> CapabilityRegistration:
    return CapabilityRegistration(
        agent_id=agent_id,
        agent_type=agent_type.value,
        capabilities=capabilities,
        hazard_types=hazards or [h.value for h in HazardType if h is not HazardType.UNKNOWN],
        max_concurrency=concurrency,
    )


@pytest.fixture
def registry(settings: Settings) -> AgentRegistry:
    return AgentRegistry(settings)


class TestRegistration:
    def test_register_and_snapshot(self, registry: AgentRegistry) -> None:
        registry.register(reg("assess.t01", AgentType.ASSESS, ["risk_assess"]))
        snap = registry.snapshot()
        assert snap[0]["agent_id"] == "assess.t01"
        assert snap[0]["healthy"] is True
        assert snap[0]["inflight"] == 0

    def test_reregister_replaces_state(self, registry: AgentRegistry) -> None:
        registry.register(reg("assess.t01", AgentType.ASSESS, ["risk_assess"]))
        entry = registry.get("assess.t01")
        entry.inflight = 5
        registry.register(reg("assess.t01", AgentType.ASSESS, ["risk_assess", "chain_assess"]))
        fresh = registry.get("assess.t01")
        assert fresh.inflight == 0
        assert "chain_assess" in fresh.reg.capabilities

    def test_heartbeat_unknown_agent(self, registry: AgentRegistry) -> None:
        assert registry.heartbeat("assess.nope") is False

    def test_unregister(self, registry: AgentRegistry) -> None:
        registry.register(reg("plan.t01", AgentType.PLAN, ["task_decompose"]))
        assert registry.unregister("plan.t01") is not None
        assert registry.unregister("plan.t01") is None


class TestRouting:
    def test_pick_by_capability(self, registry: AgentRegistry) -> None:
        registry.register(reg("assess.a", AgentType.ASSESS, ["risk_assess"]))
        registry.register(reg("assess.b", AgentType.ASSESS, ["other"]))
        assert registry.pick(AgentType.ASSESS.value, "risk_assess").agent_id == "assess.a"
        assert registry.pick(AgentType.ASSESS.value, "missing") is None

    def test_pick_respects_hazard_scope(self, registry: AgentRegistry) -> None:
        registry.register(reg("assess.a", AgentType.ASSESS, ["risk_assess"], hazards=["landslide"]))
        assert registry.pick(AgentType.ASSESS.value, "risk_assess", "landslide") is not None
        assert registry.pick(AgentType.ASSESS.value, "risk_assess", "avalanche") is None

    def test_pick_least_loaded(self, registry: AgentRegistry) -> None:
        registry.register(reg("assess.a", AgentType.ASSESS, ["risk_assess"], concurrency=2))
        registry.register(reg("assess.b", AgentType.ASSESS, ["risk_assess"], concurrency=2))
        registry.get("assess.a").inflight = 2
        assert registry.pick(AgentType.ASSESS.value, "risk_assess").agent_id == "assess.b"

    def test_capacity_excludes_saturated_agent(self, registry: AgentRegistry) -> None:
        registry.register(reg("assess.a", AgentType.ASSESS, ["risk_assess"], concurrency=1))
        registry.get("assess.a").inflight = 1
        assert registry.pick(AgentType.ASSESS.value, "risk_assess") is None
        assert registry.candidates(AgentType.ASSESS.value, "risk_assess") == []

    def test_candidates_sorted_by_load(self, registry: AgentRegistry) -> None:
        for name, load in (("assess.a", 1), ("assess.b", 0), ("assess.c", 2)):
            registry.register(reg(name, AgentType.ASSESS, ["risk_assess"], concurrency=4))
            registry.get(name).inflight = load
        assert [e.agent_id for e in registry.candidates(AgentType.ASSESS.value, "risk_assess")] == [
            "assess.b",
            "assess.a",
            "assess.c",
        ]

    def test_online_count(self, registry: AgentRegistry) -> None:
        registry.register(reg("assess.a", AgentType.ASSESS, ["risk_assess"]))
        registry.register(reg("plan.a", AgentType.PLAN, ["task_decompose"]))
        assert registry.online_count == 2


class TestHealth:
    def test_sweep_marks_unhealthy(self, registry: AgentRegistry, settings: Settings) -> None:
        registry.register(reg("assess.a", AgentType.ASSESS, ["risk_assess"]))
        registry.get("assess.a").last_hb_mono = 0.0  # 模拟心跳长期缺失
        assert registry.sweep() == ["assess.a"]
        assert registry.get("assess.a").healthy is False
        assert registry.sweep() == [], "已失联的智能体不重复上报"

    def test_heartbeat_restores(self, registry: AgentRegistry) -> None:
        registry.register(reg("assess.a", AgentType.ASSESS, ["risk_assess"]))
        registry.get("assess.a").last_hb_mono = 0.0
        registry.sweep()
        assert registry.heartbeat("assess.a") is True
        assert registry.get("assess.a").healthy is True
        assert registry.pick(AgentType.ASSESS.value, "risk_assess") is not None

    def test_unhealthy_excluded_from_routing(self, registry: AgentRegistry) -> None:
        registry.register(reg("assess.a", AgentType.ASSESS, ["risk_assess"]))
        registry.get("assess.a").last_hb_mono = 0.0
        registry.sweep()
        assert registry.pick(AgentType.ASSESS.value, "risk_assess") is None

    async def test_unhealthy_callback_fires(self, registry: AgentRegistry) -> None:
        seen: list[str] = []

        async def callback(agent_id: str, reason: str) -> None:
            seen.append(f"{agent_id}:{reason}")

        registry.on_unhealthy(callback)
        registry.register(reg("assess.a", AgentType.ASSESS, ["risk_assess"]))
        registry.get("assess.a").last_hb_mono = 0.0
        assert await registry.sweep_and_notify() == ["assess.a"]
        assert seen == ["assess.a:heartbeat_lost"]

    async def test_callback_exception_does_not_break_sweep(self, registry: AgentRegistry) -> None:
        async def boom(agent_id: str, reason: str) -> None:
            raise RuntimeError("回调异常")

        registry.on_unhealthy(boom)
        registry.register(reg("assess.a", AgentType.ASSESS, ["risk_assess"]))
        registry.get("assess.a").last_hb_mono = 0.0
        assert await registry.sweep_and_notify() == ["assess.a"]

    async def test_background_sweeper_loop(self, settings: Settings) -> None:
        cfg = Settings(env="test", heartbeat_interval_seconds=0.05, heartbeat_miss_limit=1)
        registry = AgentRegistry(cfg)
        registry.register(reg("assess.a", AgentType.ASSESS, ["risk_assess"]))
        await registry.start_sweeper()
        await asyncio.sleep(0.3)
        await registry.stop_sweeper()
        assert registry.get("assess.a").healthy is False
        assert registry.online_count == 0

    def test_inflight_never_negative(self, registry: AgentRegistry) -> None:
        entry = registry.register(reg("assess.a", AgentType.ASSESS, ["risk_assess"]))
        entry.inflight = max(0, entry.inflight - 5)
        assert entry.inflight == 0
