"""子代理审查第一轮修复的回归用例。

每条对应一个已证实的审查发现（file:line 见 REPORT 的审查轮次小节）：
- 研判命中超契约上限的确定性截断（stages._cap_hits）；
- TelemetryStore 时间窗的 datetime 比较（偏移归一 + 混精度等时刻边界）；
- 四个 store 的孤儿行淘汰（有界 deque 静默丢左端）；
- Publisher：广播失败不丢已送达的账；
- IngestService：传输层异常不炸发布 gather、常驻循环不死任务。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from aegis.config import Settings
from aegis.domain.chain import ChainResult
from aegis.domain.messages import TriggerHit, now_iso
from aegis.domain.enums import RiskLevel
from aegis.pipeline.stages import HazardStages
from aegis.pipeline.publisher import WarningPublisher
from aegis.storage.store import (
    AlertStore,
    ControlActionStore,
    NotificationStore,
    PlatformStore,
    TelemetryStore,
)
from conftest import readings_rain_burst


def _hit(rule_id: str, score: float = 0.5) -> TriggerHit:
    return TriggerHit(rule_id=rule_id, hazard_type="debris_flow", region_code="540121", score=score)


class TestHitsCap:
    def _stages(self) -> HazardStages:
        """只用到 `_cap_hits` 的账本面；依赖全部缺省（不出段、不发单）。"""
        from aegis.bus.gateway import AgentGateway
        from aegis.bus.inmemory import InMemoryBus
        from aegis.bus.registry import AgentRegistry
        from aegis.contracts import ContractRegistry
        from aegis.observability.tracer import Tracer

        bus = InMemoryBus()
        registry = AgentRegistry(Settings(env="test"))
        contracts = ContractRegistry()
        return HazardStages(
            gateway=AgentGateway(bus, registry, contracts, Tracer()), registry=registry, contracts=contracts
        )

    def test_超限截断到六十四条且降级留痕(self) -> None:
        stages = self._stages()
        result = ChainResult(trace_id="trc_" + "a" * 16, event_id="evt-1")
        merged = [_hit(f"R-{index:03d}", score=index / 100) for index in range(70)]
        kept, adopted = stages._cap_hits(merged, adopted=0, trace="trc_" + "a" * 16, result=result)
        assert len(kept) == 64
        assert adopted == 0
        assert any("契约上限 64" in note for note in result.degradations)
        # 确定性口径：保留的是分数最高的 64 条
        assert min(h.score for h in kept) >= 0.06

    def test_不超限原样返回(self) -> None:
        stages = self._stages()
        result = ChainResult(trace_id="trc_" + "b" * 16, event_id="evt-2")
        merged = [_hit(f"R-{index}") for index in range(64)]
        kept, _ = stages._cap_hits(merged, adopted=3, trace="trc_" + "b" * 16, result=result)
        assert kept is merged
        assert result.degradations == []


class TestTelemetryTimeWindow:
    async def test_非UTC偏移按时刻比较而不是钟表序(self) -> None:
        store = TelemetryStore(maxlen=100)
        reading = readings_rain_burst()[0]
        reading.observed_at = "2026-11-20T09:30:00.000Z"
        await store.add([reading])
        # until=16:00+08:00 == 08:00Z：09:30Z 的读数必须被**排除**（字符串序会把它包进来）
        rows = store.query(
            region_code=reading.region_code, until=datetime.fromisoformat("2026-11-20T16:00:00+08:00")
        )
        assert rows == []
        # since=16:00+08:00 == 08:00Z：09:30Z 必须被包含（字符串序会把它漏掉）
        rows = store.query(
            region_code=reading.region_code, since=datetime.fromisoformat("2026-11-20T16:00:00+08:00")
        )
        assert len(rows) == 1

    async def test_等时刻与混精度边界不判反(self) -> None:
        platform = PlatformStore()
        store = platform.telemetry
        reading = readings_rain_burst()[0]
        reading.observed_at = "2026-11-20T14:00:00.500Z"
        await store.add([reading])
        # 等时刻（毫秒存储 vs 微秒边界）：闭区间语义必须包含
        rows = store.query(region_code=reading.region_code, until="2026-11-20T14:00:00.500000Z")
        assert len(rows) == 1
        # 同秒内混精度：无小数边界 + 毫秒读数
        rows = store.query(region_code=reading.region_code, since="2026-11-20T14:00:00Z")
        assert len(rows) == 1
        # 字符串序在此例会把 "…00.500Z" < "…00Z" 判成 False 之外的方向——datetime 比较不判反
        rows = store.query(region_code=reading.region_code, until="2026-11-20T14:00:00Z")
        assert rows == []

    async def test_缓存后结果一致(self) -> None:
        platform = PlatformStore()
        store = platform.telemetry
        reading = readings_rain_burst()[0]
        reading.observed_at = "2026-11-20T14:00:00.500Z"
        await store.add([reading])
        first = store.query(region_code=reading.region_code, until="2026-11-20T14:00:00.500000Z")
        second = store.query(region_code=reading.region_code, until="2026-11-20T14:00:00.500000Z")
        assert len(first) == len(second) == 1


class TestStoreOrphanEviction:
    async def test_control_action_满员淘汰无孤儿(self) -> None:
        store = ControlActionStore(maxlen=2)
        ids = []
        for index in range(3):
            row, _replayed = await store.create_action(
                action_id=f"A-{index}",
                operator_id="op",
                action="warn.publish",
                space_id=None,
                utterance="",
                resolved_args={},
                impact_scope={},
                expires_in_seconds=60,
                idempotency_key=None,
            )
            ids.append(row["action_id"])
        listed = {row["action_id"] for row in await store.list_actions()}
        assert listed == {"A-1", "A-2"}, "最旧一条必须彻底出账（此前会留孤儿：list 看不见但 get 捞得到）"
        for aid in ("A-0", "A-1", "A-2"):
            fetched = await store.get_action(aid)
            assert (fetched is None) == (aid == "A-0")

    async def test_alert_满员淘汰清反向索引(self) -> None:
        store = AlertStore(maxlen=2)
        for index in range(3):
            await store.raise_alert(
                alert_id=f"AL-{index}",
                warning_id=f"W-{index}",
                level=1,
                region_code="540121",
                drill=False,
            )
        rows = await store.list_alerts()
        assert [row["warning_id"] for row in rows] == ["W-1", "W-2"]
        assert await store.get_alert("AL-0") is None, "最旧一条必须彻底出账"
        assert store._by_warning.get("W-0") is None, "by_warning 反向索引必须跟着淘汰"

    async def test_notification_满员淘汰无孤儿(self) -> None:
        store = NotificationStore(maxlen=2)
        for index in range(4):
            await store.insert_notification(recipient="op", message=f"m{index}", kind="alert")
        rows = await store.list_notifications(recipient="op")
        assert [row["message"] for row in rows] == ["m2", "m3"]


class TestPublisherBroadcastFailure:
    async def test_广播失败不丢已送达的账(self) -> None:
        class _BrokenGateway:
            async def publish_to(self, *args: object, **kwargs: object) -> None:
                raise RuntimeError("NATS 未连接")

        publisher = WarningPublisher(dispatcher=None, gateway=_BrokenGateway())  # type: ignore[arg-type]
        from aegis.domain.messages import WarningRecord

        record = WarningRecord(
            event_id="evt-1",
            trace_id="trc_" + "c" * 16,
            hazard_type="debris_flow",
            region_codes=["540121"],
            risk_level=RiskLevel.RED,
            title_zh="t",
            body_zh="b",
        )
        returned = await publisher.release(record)
        assert returned.released_at is not None, "发布成功的事实必须留在账上"
        assert any(attempt.channel == "broadcast" and attempt.status == "failed" for attempt in returned.deliveries)


@pytest.fixture
def platform_store() -> PlatformStore:
    return PlatformStore()


@pytest.fixture
async def started_gateway(gateway):
    await gateway.start()
    return gateway


class TestRunLoopGuard:
    async def test_单轮失败任务不死(self, gateway, tracer, settings, platform_store: PlatformStore) -> None:
        from aegis.connectors.base import DataSource, IngestService

        class _BoomSource(DataSource):
            name = "boom"

            async def collect(self) -> list:
                raise RuntimeError("站端掉电")

        ingest = IngestService(
            gateway=gateway,
            store=platform_store,
            tracer=tracer,
            settings=settings,
            sources=[_BoomSource()],
            per_source_timeout_ms=40,
        )
        await gateway.start()
        stop = asyncio.Event()
        run_task = asyncio.create_task(ingest.run_loop(stop, interval_seconds=0.01))
        await asyncio.sleep(0.05)
        stop.set()
        rounds = await asyncio.wait_for(run_task, timeout=2.0)
        assert rounds >= 2, "单轮连续失败不许把常驻任务打死（此前一次异常即永久停摆）"


class TestPublishTransportIsolation:
    async def test_传输层异常只丢单条回执(self, started_gateway, tracer, settings, platform_store: PlatformStore) -> None:
        from aegis.connectors.base import IngestService

        ingest = IngestService(
            gateway=started_gateway,
            store=platform_store,
            tracer=tracer,
            settings=settings,
            sources=[],
            per_source_timeout_ms=40,
        )
        readings = readings_rain_burst()

        async def _boom(reading, message):
            raise RuntimeError("NATS 未连接")

        started_gateway.publish_telemetry = _boom  # type: ignore[method-assign]
        published = await ingest._publish(readings, trace_id="trc_" + "d" * 16)
        assert published == 0, "传输层 RuntimeError 不是 AegisError，但隔离承诺按单条读数立——不该炸整轮 gather"
