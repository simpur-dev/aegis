"""靶向触达测试：并发下发、单通道超时、重试、全通道失败、时延记录。"""

from __future__ import annotations

import asyncio

import pytest

from aegis.config import Settings
from aegis.domain.enums import Channel, RiskLevel
from aegis.domain.messages import DeliveryAttempt, WarningRecord, now_iso, parse_iso
from aegis.observability.tracer import Tracer
from aegis.services.delivery import AllChannelsFailedError, DeliveryDispatcher, MockChannelAdapter


def warning(channels: list[Channel], *, audiences: list[str] | None = None) -> WarningRecord:
    return WarningRecord(
        event_id="evt_" + "c" * 12,
        trace_id="trc_" + "d" * 16,
        hazard_type="debris_flow",
        region_codes=["540121"],
        risk_level=RiskLevel.RED,
        title_zh="红色预警",
        body_zh="正文",
        audiences=audiences if audiences is not None else ["residents", "schools"],
        channels=[c.value for c in channels],
    )


class StubAdapter:
    def __init__(
        self,
        name: Channel,
        *,
        delay_ms: float = 0.0,
        fail_first: int = 0,
        hang: bool = False,
        receipt_delay_ms: float = 0.0,
    ) -> None:
        self.name = name
        self.calls = 0
        self._delay_ms = delay_ms
        self._fail_first = fail_first
        self._hang = hang
        self._receipt_delay_ms = receipt_delay_ms

    async def send(self, record: WarningRecord, audiences) -> DeliveryAttempt:
        self.calls += 1
        if self._hang:
            await asyncio.sleep(30)
        await asyncio.sleep(self._delay_ms / 1000)
        status = "failed" if self.calls <= self._fail_first else "delivered"
        if status == "delivered" and self._receipt_delay_ms:
            await asyncio.sleep(self._receipt_delay_ms / 1000)
        return DeliveryAttempt(
            channel=self.name.value,
            audience_count=len(audiences),
            status=status,
            receipt_at=now_iso() if status == "delivered" else None,
        )


class TestConstruction:
    def test_empty_adapters_rejected(self) -> None:
        with pytest.raises(ValueError, match="至少需要一个交付通道"):
            DeliveryDispatcher({})

    def test_channels_for_respects_record(self) -> None:
        dispatcher = DeliveryDispatcher({Channel.SMS: StubAdapter(Channel.SMS), Channel.BEIDOU: StubAdapter(Channel.BEIDOU)})
        selected = dispatcher.channels_for(warning([Channel.SMS]))
        assert [a.name for a in selected] == [Channel.SMS]

    def test_channels_for_falls_back_when_unknown(self) -> None:
        dispatcher = DeliveryDispatcher({Channel.SMS: StubAdapter(Channel.SMS)})
        record = warning([])
        record.channels = ["carrier_pigeon"]
        assert [a.name for a in dispatcher.channels_for(record)] == [Channel.SMS]


class TestDispatch:
    async def test_all_delivered(self) -> None:
        adapters = {Channel.SMS: StubAdapter(Channel.SMS), Channel.WECHAT: StubAdapter(Channel.WECHAT)}
        record = await DeliveryDispatcher(adapters).dispatch(warning([Channel.SMS, Channel.WECHAT]))
        assert record.released_at
        assert {d.status for d in record.deliveries} == {"delivered"}
        assert all(d.audience_count == 2 for d in record.deliveries)

    async def test_channels_run_concurrently(self) -> None:
        adapters = {
            Channel.SMS: StubAdapter(Channel.SMS, delay_ms=120),
            Channel.BEIDOU: StubAdapter(Channel.BEIDOU, delay_ms=120),
            Channel.BROADCAST: StubAdapter(Channel.BROADCAST, delay_ms=120),
        }
        loop = asyncio.get_running_loop()
        started = loop.time()
        await DeliveryDispatcher(adapters).dispatch(warning([Channel.SMS, Channel.BEIDOU, Channel.BROADCAST]))
        elapsed = (loop.time() - started) * 1000
        assert elapsed < 300, f"并发下发应为 ~120ms，实测 {elapsed:.0f}ms（疑似串行）"

    async def test_retry_then_success(self) -> None:
        adapter = StubAdapter(Channel.SMS, fail_first=2)
        record = await DeliveryDispatcher({Channel.SMS: adapter}, max_retries=2, backoff_ms=10).dispatch(warning([Channel.SMS]))
        assert adapter.calls == 3
        assert any(d.status in ("delivered", "retried") for d in record.deliveries)

    async def test_all_channels_fail_raises(self) -> None:
        adapter = StubAdapter(Channel.SMS, fail_first=5)
        with pytest.raises(AllChannelsFailedError):
            await DeliveryDispatcher({Channel.SMS: adapter}, max_retries=1, backoff_ms=5).dispatch(warning([Channel.SMS]))

    async def test_one_channel_timeout_does_not_block_others(self) -> None:
        slow = StubAdapter(Channel.BEIDOU, hang=True)
        fast = StubAdapter(Channel.SMS)
        record = await DeliveryDispatcher(
            {Channel.BEIDOU: slow, Channel.SMS: fast},
            per_channel_timeout_ms=100,
            max_retries=0,
        ).dispatch(warning([Channel.BEIDOU, Channel.SMS]))
        statuses = {d.channel: d.status for d in record.deliveries}
        assert statuses[Channel.SMS.value] == "delivered"
        assert statuses[Channel.BEIDOU.value] == "failed"

    async def test_reach_latency_recorded(self) -> None:
        tracer = Tracer()
        adapters = {Channel.SMS: StubAdapter(Channel.SMS, receipt_delay_ms=60)}
        record = await DeliveryDispatcher(adapters, tracer).dispatch(warning([Channel.SMS]))
        stats = tracer.ledger.stats("warning_reach_ms")
        assert stats.count == 1
        assert stats.max >= 50
        assert record.reach_seconds() is not None


class TestMockAdapter:
    async def test_deterministic_with_seed(self, settings: Settings) -> None:
        results = []
        for _ in range(2):
            adapter = MockChannelAdapter(Channel.SMS, latency_ms=0, fail_rate=0.5, seed=42)
            record = await adapter.send(warning([Channel.SMS]), ["residents"])
            results.append(record.status)
        assert results[0] == results[1]

    async def test_zero_fail_rate_always_delivers(self) -> None:
        adapter = MockChannelAdapter(Channel.WECHAT, latency_ms=1, fail_rate=0.0, receipt_latency_ms=1)
        record = await adapter.send(warning([Channel.WECHAT]), ["residents"])
        assert record.status == "delivered"
        assert record.receipt_at and record.provider_msg_id

    async def test_audience_size_scales_count(self) -> None:
        adapter = MockChannelAdapter(Channel.BROADCAST, latency_ms=0, audience_size=37, fail_rate=0.0, receipt_latency_ms=0)
        record = await adapter.send(warning([Channel.BROADCAST]), ["residents", "schools"])
        assert record.audience_count == 74

    async def test_receipt_time_after_release(self) -> None:
        adapter = MockChannelAdapter(Channel.SMS, latency_ms=0, fail_rate=0.0, receipt_latency_ms=20)
        record = await adapter.send(warning([Channel.SMS]), ["residents"])
        assert parse_iso(record.receipt_at) >= parse_iso(record.attempted_at)
