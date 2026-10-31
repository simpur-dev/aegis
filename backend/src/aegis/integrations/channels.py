"""总线与触达通道的装配：内核只认 `BusTransport` / `ChannelAdapter` 协议，实现体在这里挑。

（P4 纯搬运：函数体逐字未改，原先住在 `container.py` 的这几位装配辅助现在归装配包。）
"""

from __future__ import annotations

from typing import Any

from aegis.bus.inmemory import InMemoryBus
from aegis.bus.transport import BusTransport
from aegis.config import Settings
from aegis.domain.enums import Channel
from aegis.services.response.delivery import (
    ChannelAdapter,
    HttpChannelAdapter,
    normalize_gateway_target,
    parse_delivery_channels,
)
from aegis.workflow.outbound import parse_host_list


def build_transport(settings: Settings) -> BusTransport:
    if settings.bus_backend == "nats":
        from aegis.bus.nats_bus import ACK_WAIT_MARGIN_SECONDS, NatsBus  # 延迟导入：仅在启用时要求 nats 依赖

        return NatsBus(
            settings.nats_url,
            stream_prefix=settings.nats_stream_prefix,
            # 回执窗口必须盖住一次协同事务的预算：短于预算时服务端会在智能体仍在处理时重投同一请求。
            ack_wait_seconds=settings.sla_agent_txn_ms / 1000.0 + ACK_WAIT_MARGIN_SECONDS,
        )
    return InMemoryBus()


def default_channels(settings: Settings, *, client: Any | None = None) -> dict[Channel, ChannelAdapter]:
    """交付通道矩阵：mock 供演练与压测复现，http 才是现场触达。

    `delivery_mode=http` 而网关没配齐时是**构造期失败**，不静默回落 mock：
    把"网关没接上"读成"触达已达成"是这一型平台最贵的事故（架构铁律 7：指标必须实测、降级必须可见）。
    """
    if settings.delivery_mode == "mock":
        return {
            Channel.SMS: MockChannel(settings),
            Channel.BEIDOU: MockChannel(settings, latency_ms=900.0, name=Channel.BEIDOU),
            Channel.BROADCAST: MockChannel(settings, latency_ms=400.0, name=Channel.BROADCAST),
            Channel.WECHAT: MockChannel(settings, latency_ms=250.0, name=Channel.WECHAT),
        }
    return build_http_channels(settings, client=client)


def build_http_channels(settings: Settings, *, client: Any | None = None) -> dict[Channel, ChannelAdapter]:
    """按配置拼装真实通道。主机白名单为空即整条腿不可用（口径与 `workflow/outbound.py` 一致）。"""
    if client is None:
        raise ValueError("delivery_mode=http 需要注入 HTTP 客户端（见 build_delivery_client）")
    target = normalize_gateway_target(
        settings.delivery_http_base_url,
        allowed_hosts=parse_host_list(settings.delivery_http_allowed_hosts),
    )
    channels = parse_delivery_channels(settings.delivery_http_channels)
    return {
        channel: HttpChannelAdapter(
            channel,
            target.url,
            client,
            path=settings.delivery_http_path,
            timeout_ms=settings.delivery_http_timeout_ms,
        )
        for channel in channels
    }


def build_delivery_client(settings: Settings, *, client: Any | None = None) -> tuple[Any | None, bool]:
    """真实通道的 HTTP 客户端：凭据只进请求头、不跟随重定向。返回 (客户端, 是否本装配点自建)。"""
    if settings.delivery_mode != "http":
        return None, False
    if client is not None:
        return client, False
    import httpx  # 延迟导入：只有真实通道形态才要求 httpx

    headers = {"Authorization": f"Bearer {settings.delivery_http_token}"} if settings.delivery_http_token else {}
    return (
        httpx.AsyncClient(
            timeout=settings.delivery_http_timeout_ms / 1000,
            headers=headers,
            follow_redirects=False,
        ),
        True,
    )


class MockChannel:
    def __init__(self, settings: Settings, *, name: Channel = Channel.SMS, latency_ms: float = 150.0) -> None:
        from aegis.services.response.delivery import MockChannelAdapter

        self._inner = MockChannelAdapter(
            name=name,
            latency_ms=latency_ms,
            fail_rate=0.0,
            receipt_latency_ms=300.0,
            seed=settings.simulator_seed,
        )
        self.name = name

    async def send(self, record, audiences):
        return await self._inner.send(record, audiences)
