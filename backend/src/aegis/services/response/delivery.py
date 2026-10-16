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
from typing import Any, Protocol
from urllib.parse import urlsplit

from aegis.domain.enums import Channel
from aegis.domain.messages import DeliveryAttempt, WarningRecord, now_iso, parse_iso, utc_now
from aegis.errors import AegisError, ErrorCode
from aegis.observability.tracer import Tracer

log = logging.getLogger("aegis.services.delivery")


class AllChannelsFailedError(AegisError):
    code = ErrorCode.INTERNAL


@dataclass(frozen=True, slots=True)
class GatewayTarget:
    """网关地址的两个面：`url` 只给请求用，`masked` 才是能进状态面与日志的那一份。"""

    url: str
    masked: str


def normalize_gateway_target(raw: str, *, allowed_hosts: Sequence[str]) -> GatewayTarget:
    """校验触达网关：只允许 http/https、主机必须精确命中白名单、地址里不允许内嵌凭据。

    与 `workflow/outbound.py` 共用同一口径的理由很直接：这条路径上的载荷是"往哪些区域发什么内容"，
    白名单写歪一次，短信网关就变成了指向内网地址的代理。
    """
    text = str(raw or "").strip()
    if not text:
        raise ValueError("触达网关地址为空（AEGIS_DELIVERY_HTTP_BASE_URL）")
    try:
        parsed = urlsplit(text)
    except ValueError as exc:
        raise ValueError(f"触达网关地址无法解析: {exc}") from exc
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        raise ValueError(f"触达网关协议只允许 http/https，收到 {scheme or '<none>'}")
    host = (parsed.hostname or "").rstrip(".").lower()
    if not host:
        raise ValueError("触达网关地址没有主机名")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("触达网关地址不允许内嵌凭据（凭据请放请求头）")
    if host not in allowed_hosts:
        raise ValueError(f"触达网关主机不在白名单：{host}")
    port = f":{parsed.port}" if parsed.port else ""
    return GatewayTarget(
        url=f"{scheme}://{parsed.netloc}".rstrip("/"),
        masked=f"{scheme}://{host}{port}",
    )


def parse_delivery_channels(raw: str) -> tuple[Channel, ...]:
    """把 `sms,broadcast` 读成通道枚举。未知与空集都是配置错误：
    静默丢掉一个通道，等于那一类人群永远不会被触达，而报表上写着"发布成功"。
    """
    names = [item.strip().lower() for item in str(raw or "").split(",") if item.strip()]
    if not names:
        raise ValueError("delivery_mode=http 需要至少一个通道（AEGIS_DELIVERY_HTTP_CHANNELS）")
    resolved: list[Channel] = []
    for name in names:
        try:
            channel = Channel(name)
        except ValueError as exc:
            raise ValueError(f"未知交付通道: {name}") from exc
        if channel not in resolved:
            resolved.append(channel)
    return tuple(resolved)


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
    """真实网关对接（短信/北斗/广播/微信统一走 HTTP 发布网关）。

    客户端由装配点注入（`container.build_delivery_client`）：不跟随重定向、凭据只在请求头。
    失败不抛异常而是回一条 `failed` 回执——调度器据此重试，并把"哪个通道失败、为什么"
    留在 `status()` 里（架构铁律 7：降级必须可见）。
    """

    def __init__(
        self,
        name: Channel,
        base_url: str,
        client: Any,
        *,
        path: str = "/publish",
        timeout_ms: int = 5_000,
    ) -> None:
        self.name = name
        self._base_url = base_url.rstrip("/")
        self._path = "/" + str(path or "/publish").lstrip("/")
        self._client = client
        self._timeout = timeout_ms / 1000
        self.sent = 0
        self.delivered = 0
        self.failed = 0
        self.last_error: str | None = None

    @property
    def target(self) -> str:
        return f"{self._base_url}{self._path}"

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
        self.sent += 1
        try:
            response = await asyncio.wait_for(self._client.post(self.target, json=body), timeout=self._timeout)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                payload = {}
            status = str(payload.get("status", "delivered"))
            attempt.status = status if status in ("pending", "delivered", "failed", "retried") else "delivered"
            attempt.provider_msg_id = (str(payload.get("message_id") or payload.get("provider_msg_id") or "")) or None
            attempt.receipt_at = now_iso() if attempt.status in ("delivered", "retried") else None
        except Exception as exc:
            attempt.status = "failed"
            self.failed += 1
            # 只留类型与截断文本：网关地址与异常体都可能带凭据，不外显整段
            self.last_error = f"{type(exc).__name__}: {str(exc)[:120]}"
            log.warning("通道下发失败", extra={"channel": self.name.value, "error": self.last_error})
        else:
            self.delivered += 1 if attempt.status in ("delivered", "retried") else 0
        return attempt

    def status(self) -> dict[str, Any]:
        return {
            "channel": self.name.value,
            "target": self._base_url,
            "sent": self.sent,
            "delivered": self.delivered,
            "failed": self.failed,
            "last_error": self.last_error,
        }


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

    def channel_status(self) -> list[dict[str, Any]]:
        """各通道的运行事实：真实通道带发送/失败计数，mock 通道明写 mock。

        这一面存在的理由是考核口径本身：mock 通道的"发布成功"不能与真网关回执混成一行，
        否则 ≤20min 触达指标就是自证。
        """
        rows: list[dict[str, Any]] = []
        for channel, adapter in self._adapters.items():
            status = getattr(adapter, "status", None)
            rows.append(status() if callable(status) else {"channel": channel.value, "mode": "mock"})
        return rows

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
