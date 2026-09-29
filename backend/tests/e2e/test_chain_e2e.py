"""端到端链路测试：三形态（智能体在场/缺位/违约）下的完整业务闭环与指标量测。

这些测试是"感知—研判—决策—执行—反馈"全链条与考核指标埋点的验收证据：
断言的是运行时实测数字（时延分位、协同成功率），不是配置里的期望值。
"""

from __future__ import annotations

import asyncio

import pytest

from aegis.agents.mock import Misbehavior, start_mock_agents
from aegis.config import Settings
from aegis.connectors.base import DataSource
from aegis.container import PlatformContainer, create_container
from aegis.domain.enums import AgentType, Channel, RiskLevel
from aegis.domain.messages import DeliveryAttempt
from aegis.services.delivery import ChannelAdapter

REGIONS = ("540121", "540221", "540321")


class StaticSource(DataSource):
    """按轮次回放固定读数，替代模拟器以便构造正/负样本。"""

    def __init__(self, name: str, batches: list[list]) -> None:
        self.name = name
        self._batches = list(batches)
        self._index = 0

    async def collect(self) -> list:
        if self._index >= len(self._batches):
            return []
        batch = self._batches[self._index]
        self._index += 1
        return batch


def surge_readings():
    """构造 3 个区域、5 类灾种均可触发的读数（复用模拟器保证真实字段）。"""
    from aegis.connectors.simulator import HazardScenarioSimulator

    simulator = HazardScenarioSimulator(scenario="surge", seed=99)
    return simulator.collect_at(_now())


def normal_readings():
    from aegis.connectors.simulator import HazardScenarioSimulator

    simulator = HazardScenarioSimulator(scenario="normal", seed=99)
    return simulator.collect_at(_now())


def _now():
    from aegis.domain.messages import utc_now

    return utc_now()


def group(readings) -> dict[str, list]:
    grouped: dict[str, list] = {}
    for reading in readings:
        grouped.setdefault(reading.region_code, []).append(reading)
    return grouped


async def build_container(settings: Settings, *, channels: dict | None = None) -> PlatformContainer:
    container = create_container(settings, channels=channels, with_simulator=False)
    await container.start()
    return container


class TestDegradedPath:
    async def test_full_chain_without_agents(self, settings: Settings) -> None:
        container = await build_container(settings)
        try:
            readings = surge_readings()
            grouped = group(readings)
            assert len(grouped) == len(REGIONS), "模拟器应覆盖三个风险区域"

            results = await container.chain.process_many(grouped)
            assert all(r.ok for r in results), [r.as_dict() for r in results]
            acted = [r for r in results if r.acted]
            assert acted, "激增场景必须触发预警"
            for result in acted:
                assert result.verdict is not None and result.warning is not None
                assert result.warning.deliveries, "预警必须有通道投递记录"
                assert result.task_units, "预警必须产出标准化任务单元"
                assert [s.name for s in result.stages] == ["perceive", "assess", "plan", "execute", "feedback"]
                assert result.degradations == []
            assert container.store.warnings.size >= 1
            assert container.store.tasks.size >= 5
        finally:
            await container.shutdown()

    async def test_negative_scenario_does_not_alert(self, settings: Settings) -> None:
        container = await build_container(settings)
        try:
            results = await container.chain.process_many(group(normal_readings()))
            assert all(r.ok for r in results)
            assert not any(r.acted for r in results), "正常天气背景不得误报"
            assert container.store.warnings.size == 0
        finally:
            await container.shutdown()

    async def test_suspect_only_data_is_ignored(self, settings: Settings) -> None:
        container = await build_container(settings)
        try:
            readings = surge_readings()
            for reading in readings:
                reading.quality_flag = "suspect"
            results = await container.chain.process_many(group(readings))
            assert not any(r.acted for r in results), "劣化读数不得进入触发判定"
        finally:
            await container.shutdown()


class TestAgentPath:
    async def test_agents_take_over_and_success_rate(self, settings: Settings) -> None:
        container = await build_container(settings)
        try:
            await start_mock_agents(container.transport, latency_ms=8.0, heartbeat_interval=0.05)
            await asyncio.sleep(0.05)
            await container.transport.idle()

            results = await container.chain.process_many(group(surge_readings()))
            acted = [r for r in results if r.acted]
            assert acted
            for result in acted:
                modes = {s.name: s.mode for s in result.stages}
                assert modes["assess"] == "agent", modes
                assert modes["plan"] == "agent", modes
                assert modes["execute"] == "agent", modes
                assert all(u.created_by.startswith("plan.") for u in result.task_units)

            rate = container.gateway.success_rate()
            assert rate is not None and rate >= settings.sla_collaboration_success_rate
            sync = container.tracer.ledger.stats("sync_agent_to_gateway_ms")
            assert sync.count >= 1
            assert sync.p95 <= settings.sla_sync_ms, f"共享同步 P95 {sync.p95:.0f}ms 超阈值"
        finally:
            await container.shutdown()

    async def test_concurrent_regions_complete(self, settings: Settings) -> None:
        container = await build_container(settings)
        try:
            loop = asyncio.get_running_loop()
            started = loop.time()
            results = await container.chain.process_many(group(surge_readings()))
            elapsed = loop.time() - started
            assert len(results) == len(REGIONS)
            assert elapsed < 3.0, f"三区域并发链路耗时 {elapsed:.2f}s 超预算"
        finally:
            await container.shutdown()


class TestDegradationUnderMisbehavior:
    @pytest.mark.parametrize(
        ("agent_type", "misbehavior", "stage"),
        [
            (AgentType.ASSESS, Misbehavior.SILENT, "assess"),
            (AgentType.PLAN, Misbehavior.CRASH, "plan"),
            (AgentType.EXECUTE, Misbehavior.SILENT, "execute"),
        ],
    )
    async def test_broken_agent_does_not_break_chain(self, settings: Settings, agent_type: AgentType, misbehavior: str, stage: str) -> None:
        container = await build_container(settings)
        try:
            from aegis.agents.mock import MockAgent

            capability = {AgentType.ASSESS: "risk_assess", AgentType.PLAN: "task_decompose", AgentType.EXECUTE: "warn_publish"}[agent_type]
            agent = MockAgent(
                transport=container.transport,
                agent_type=agent_type,
                agent_id=f"{agent_type.value}.broken1",
                capabilities=[capability],
                latency_ms=5.0,
                misbehavior=misbehavior,
                seed=3,
            )
            await agent.start(heartbeat_interval=0.05)
            await asyncio.sleep(0.05)
            await container.transport.idle()

            results = await container.chain.process_many(group(surge_readings()))
            acted = [r for r in results if r.acted]
            assert acted, "单段智能体失效时平台降级路径必须继续产出预警"
            for result in acted:
                assert result.warning is not None
                modes = {s.name: s.mode for s in result.stages}
                assert modes[stage] == "local", modes
                assert result.degradations, "降级必须留痕供归因"
                assert result.errors == [], "降级不是致命错误"
            await agent.stop()
        finally:
            await container.shutdown()


class TestFatalDeliveryFailure:
    async def test_all_channels_down_marks_fatal(self, settings: Settings) -> None:
        class DeadChannel(ChannelAdapter):
            name = Channel.SMS

            async def send(self, record, audiences):
                return DeliveryAttempt(channel=Channel.SMS.value, audience_count=len(audiences), status="failed")

        container = await build_container(settings, channels={Channel.SMS: DeadChannel()})
        try:
            results = await container.chain.process_many(group(surge_readings()))
            fatal = [r for r in results if r.errors]
            assert fatal, "全部通道失败必须记为致命错误"
            for result in fatal:
                assert not result.ok
                assert result.warning is None
        finally:
            await container.shutdown()


class TestMetricEvidence:
    async def test_latency_report_covers_all_indicators(self, settings: Settings) -> None:
        container = await build_container(settings)
        try:
            await start_mock_agents(container.transport, latency_ms=6.0, heartbeat_interval=0.05)
            await asyncio.sleep(0.05)
            container.ingest.add_source(StaticSource("static_replay", [surge_readings(), surge_readings()]))
            report = await container.ingest.ingest_once()
            assert report.readings > 0 and report.ok

            readings = container.store.telemetry.query(limit=2_000)
            await container.chain.process_many(group(readings))
            await container.transport.idle()

            latency = container.latency_report()
            names = set(latency["metrics"])
            for required in (
                "stage_perceive_ms",
                "stage_assess_ms",
                "stage_plan_ms",
                "stage_execute_ms",
                "stage_feedback_ms",
                "warning_generation_ms",
                "warning_reach_ms",
                "ingest_end_to_end_seconds",
                "collab_txn",
                "sync_agent_to_gateway_ms",
            ):
                assert required in names, f"缺少指标埋点: {required}"

            # 调度响应：决策段就绪→完成必须 ≤2s（常规任务调度响应口径）
            assert latency["metrics"]["stage_plan_ms"]["p95_ms"] <= settings.sla_schedule_ms
            assert latency["metrics"]["stage_assess_ms"]["p95_ms"] <= settings.sla_schedule_ms
            assert latency["collaboration"]["pass"] is True
            assert latency["metrics"]["warning_reach_ms"]["max_ms"] <= settings.sla_reach_seconds * 1000
            assert latency["violations"] == {}
        finally:
            await container.shutdown()

    async def test_warning_content_matches_level(self, settings: Settings) -> None:
        container = await build_container(settings)
        try:
            results = await container.chain.process_many(group(surge_readings()))
            levels = {r.verdict.risk_level for r in results if r.verdict}
            assert levels & {RiskLevel.RED, RiskLevel.ORANGE}, "激增场景应产生红/橙等级"
            for result in results:
                if result.warning:
                    assert int(result.warning.risk_level) == int(result.verdict.risk_level)
                    assert result.warning.channels, "红色/橙色预警必须有触达通道"
        finally:
            await container.shutdown()
