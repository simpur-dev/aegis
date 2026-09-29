"""靶向触达：多通道并发发布 + 超时/重试/择优（触达 ≤20min 指标的落点）。

并发策略：所有通道 asyncio.gather 并行下发，单通道独立超时与退避重试；
任一通道成功即视为预警已触达，全部失败才判失败——对应架构文件「多通道冗余」「链路切换」。
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from aegis.domain.enums import Channel
from aegis.domain.messages import DeliveryAttempt, WarningRecord, now_iso, parse_iso, utc_now
from aegis.errors import AegisError, ErrorCode
from aegis.observability.tracer import Tracer

log = logging.getLogger("aegis.services.delivery")


class AllChannelsFailedError(AegisError):
    code = ErrorCode.INTERNAL


class ChannelAdapter(Protocol):
    name: Channel

    async def send(self, record: WarningRecord, audiences: Sequence[str]) -> DeliveryAttempt: ...


@dataclass(slots=True)
class MockChannelAdapter:
    """演练/压测用通道：可注入时延、失败率与回执时延，结果可复现（seed 固定）。"""

    name: Channel
    latency_ms: float = 120.0
    fail_rate: float = 0.0
    receipt_latency_ms: float = 500.0
    audience_size: int = 100
    seed: int | None = None
    sent_count: int = 0
    _rng: random.Random = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self._rng = random.Random(self.seed)

    async def send(self, record: WarningRecord, audiences: Sequence[str]) -> DeliveryAttempt:
        self.sent_count += 1
        attempt = DeliveryAttempt(
            channel=self.name.value,
            audience_count=max(len(audiences), 1) * self.audience_size,
            status="pending",
        )
        await asyncio.sleep(self.latency_ms / 1000)
        if self._rng.random() < self.fail_rate:
            attempt.status = "failed"
            return attempt
        attempt.status = "delivered"
        attempt.provider_msg_id = f"mock-{self.name.value}-{self.sent_count}"
        await asyncio.sleep(self.receipt_latency_ms / 1000)
        attempt.receipt_at = now_iso()
        return attempt


class HttpChannelAdapter:
    """真实网关对接（短信/北斗/广播/微信统一走 HTTP 发布网关）。"""

    def __init__(self, name: Channel, base_url: str, client, *, timeout_ms: int = 5_000) -> None:
        self.name = name
        self._base_url = base_url.rstrip("/")
        self._client = client
        self._timeout = timeout_ms / 1000

    async def send(self, record: WarningRecord, audiences: Sequence[str]) -> DeliveryAttempt:
        attempt = DeliveryAttempt(
            channel=self.name.value,
            audience_count=max(len(audiences), 1),
            status="pending",
        )
        body = {
            "channel": self.name.value,
            "warning_id": record.warning_id,
            "risk_level": int(record.risk_level),
            "regions": record.region_codes,
            "title": record.title_zh,
            "body": record.body_bo or record.body_zh,
            "audiences": list(audiences),
        }
        try:
            response = await asyncio.wait_for(self._client.post(f"{self._base_url}/publish", json=body), timeout=self._timeout)
            response.raise_for_status()
            payload = response.json()
            attempt.status = str(payload.get("status", "delivered"))
            attempt.provider_msg_id = str(payload.get("message_id", "")) or None
            attempt.receipt_at = now_iso() if attempt.status == "delivered" else None
        except Exception as exc:
            attempt.status = "failed"
            log.warning("通道下发失败", extra={"channel": self.name.value, "error": str(exc)})
        return attempt


class DeliveryDispatcher:
    def __init__(
        self,
        adapters: dict[Channel, ChannelAdapter],
        tracer: Tracer | None = None,
        *,
        per_channel_timeout_ms: int = 8_000,
        max_retries: int = 2,
        backoff_ms: int = 300,
        sla_reach_seconds: float = 1_200.0,
    ) -> None:
        if not adapters:
            raise ValueError("至少需要一个交付通道")
        self._adapters = adapters
        self._tracer = tracer or Tracer()
        self._timeout_s = per_channel_timeout_ms / 1000
        self._max_retries = max_retries
        self._backoff_s = backoff_ms / 1000
        self._sla_reach_seconds = sla_reach_seconds

    def channels_for(self, record: WarningRecord) -> list[ChannelAdapter]:
        """按预警声明的通道选择适配器；未知通道名跳过而非中断发布。"""
        selected: list[ChannelAdapter] = []
        for name in record.channels:
            try:
                channel = Channel(name)
            except ValueError:
                log.warning("预警声明了未知通道，忽略", extra={"channel": name, "warning_id": record.warning_id})
                continue
            if channel in self._adapters:
                selected.append(self._adapters[channel])
        return selected or list(self._adapters.values())

    async def dispatch(self, record: WarningRecord) -> WarningRecord:
        """并发下发。返回的 record 附带各通道投递结果与触达时延。"""
        channels = self.channels_for(record)
        if not channels:
            raise AllChannelsFailedError("无可用交付通道", detail={"warning_id": record.warning_id})

        started = utc_now()
        results = await asyncio.gather(*(self._send_one(c, record) for c in channels))
        attempts = [a for a in results if a is not None]
        record.deliveries = attempts

        if not any(a.status in ("delivered", "retried") for a in attempts):
            raise AllChannelsFailedError(
                f"全部通道下发失败: {[a.channel for a in attempts]}",
                detail={"warning_id": record.warning_id},
            )

        record.released_at = now_iso()
        reach = self._reach_seconds(started, attempts)
        self._tracer.record("warning_reach_ms", reach * 1000, trace_id=record.trace_id)
        if reach > self._sla_reach_seconds:
            log.warning("触达时延超 SLA", extra={"warning_id": record.warning_id, "reach_seconds": round(reach, 2)})
        return record

    async def _send_one(self, adapter: ChannelAdapter, record: WarningRecord) -> DeliveryAttempt | None:
        last: DeliveryAttempt | None = None
        for attempt_no in range(self._max_retries + 1):
            try:
                last = await asyncio.wait_for(adapter.send(record, record.audiences), timeout=self._timeout_s)
            except TimeoutError:
                last = DeliveryAttempt(channel=adapter.name.value, audience_count=len(record.audiences), status="failed")
                log.warning(
                    "通道下发超时",
                    extra={"channel": adapter.name.value, "warning_id": record.warning_id, "attempt": attempt_no + 1},
                )
            if last.status == "delivered":
                return last
            if attempt_no < self._max_retries:
                await asyncio.sleep(self._backoff_s * (2**attempt_no))
                if last is not None:
                    retried = DeliveryAttempt(
                        channel=last.channel,
                        audience_count=last.audience_count,
                        status="retried",
                        provider_msg_id=last.provider_msg_id,
                        receipt_at=last.receipt_at,
                    )
                    last = retried
        return last

    @staticmethod
    def _reach_seconds(started, attempts: Sequence[DeliveryAttempt]) -> float:
        receipts = [parse_iso(a.receipt_at) for a in attempts if a.receipt_at]
        end = max(receipts) if receipts else utc_now()
        return max((end - started).total_seconds(), 0.0)
