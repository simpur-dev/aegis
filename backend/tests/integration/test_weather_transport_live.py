"""拉取腿对真 socket 的取证：不起 MockTransport，而是本机 listen 一个最小 HTTP 服务。

MockTransport 能证明解析与计数，但证明不了三件只在真连接上才成立的事：
URL 拼装后确实能被对端收到、httpx 连接池能被我们自建并释放、对端异常断开会被如实记账成失败。
纯标准库实现（`asyncio.start_server`），不依赖网络与外部镜像，因此可以常驻回归。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from aegis.config import Settings
from aegis.connectors.weather_api import WeatherApiSource
from aegis.container import create_container

BODY = {
    "observed_at": "2026-09-30T04:20:00+00:00",
    "stations": [
        {"id": "RG-540121-01", "region_code": "540121", "rain_10min": 22.0, "debris_level": 1.7},
        {"id": "RG-540221-77", "region_code": "540221", "air_temperature_c": -3.5},
    ],
}


class TinyHttpServer:
    """只回一份固定 JSON 的 HTTP/1.1 服务；记录请求行，连接用完即关。"""

    def __init__(self, body: dict[str, Any] | None = None, *, truncate: bool = False) -> None:
        self._payload = json.dumps(body if body is not None else BODY).encode()
        self._truncate = truncate
        self.requests: list[str] = []
        self._server: asyncio.Server | None = None

    async def __aenter__(self) -> TinyHttpServer:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    @property
    def base_url(self) -> str:
        return f"http://{self.authority}"

    @property
    def authority(self) -> str:
        assert self._server is not None and self._server.sockets
        host, port = self._server.sockets[0].getsockname()[:2]
        return f"{host}:{port}"

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = await reader.readuntil(b"\r\n\r\n")
        self.requests.append(head.decode("latin-1").splitlines()[0])
        if self._truncate:  # 只写响应头就断开：模拟对端半途死掉
            writer.write(b"HTTP/1.1 200 OK\r\n")
            await writer.drain()
            writer.close()
            return
        framed = (
            b"HTTP/1.1 200 OK\r\ncontent-type: application/json\r\nconnection: close\r\n"
            + b"content-length: %d\r\n\r\n" % len(self._payload)
            + self._payload
        )
        writer.write(framed)
        await writer.drain()
        writer.close()


class TestOverRealSockets:
    async def test_self_created_client_collects_and_is_released(self) -> None:
        async with TinyHttpServer() as server:
            source = WeatherApiSource(server.base_url, path="/observation")
            readings = await source.collect()

            assert [(r.station_id, r.metric) for r in readings] == [
                ("RG-540121-01", "rain_10min"),
                ("RG-540121-01", "debris_level"),
                ("RG-540221-77", "air_temperature_c"),
            ]
            assert readings[0].unit == "mm" and readings[1].unit == "m"
            assert server.requests == ["GET /observation HTTP/1.1"]

            second = await source.collect()  # 复用同一个连接池：不该每轮重建客户端
            assert len(second) == 3
            status = source.status()
            assert (status["rounds"], status["readings"], status["failures"]) == (2, 6, 0)

            await source.aclose()
            assert source._client is None  # 自建客户端必须被回收

    async def test_second_round_hits_the_same_server_again(self) -> None:
        async with TinyHttpServer() as server:
            source = WeatherApiSource(f"{server.base_url}/", path="/observation")  # 尾斜杠要被吃掉
            await source.collect()
            await source.collect()
            assert server.requests == ["GET /observation HTTP/1.1"] * 2
            await source.aclose()

    async def test_peer_dying_mid_response_is_counted_not_swallowed(self) -> None:
        """对端半途断开：记一次失败、留下原因，且原因里不许出现端点。

        这里刻意不断言 `<endpoint>` 出现——对端半途死时 httpx 抛的是
        "Server disconnected without sending a response."，消息里本来就没有 URL；
        会不会带 URL 取决于它先吐了多少字节，那是传输层细节不是本项目的契约。
        替换逻辑本身由 `tests/unit/test_weather_connector.py` 用 MockTransport 钉住。
        """
        async with TinyHttpServer(truncate=True) as server:
            source = WeatherApiSource(server.base_url)
            with pytest.raises(httpx.HTTPError):
                await source.collect()
            status = source.status()
            assert status["failures"] == 1 and status["rounds"] == 0
            last_error = str(status["last_error"])
            assert last_error, "只记一次失败却不留原因，现场就没法区分对端死了和没人应答"
            assert server.authority not in last_error, "端点里可能带运营方填的凭据，不许进状态面"
            await source.aclose()


class TestContainerRoundOverRealSockets:
    async def test_ingest_round_pulls_from_a_live_endpoint(self) -> None:
        async with TinyHttpServer() as server:
            settings = Settings(
                env="test",
                bus_backend="memory",
                simulator_enabled=False,
                delivery_mode="mock",
                weather_api_base_url=server.base_url,
            )
            container = create_container(settings)
            await container.start()
            try:
                report = await container.ingest.ingest_once()
                assert report.sources_ok == ["weather_api"] and report.readings == 3
                assert len(container.store.telemetry.query(limit=10)) == 3

                row = {state.name: state for state in container.integration_status()}["weather"]
                assert row.enabled is True and row.driver == "http"
                assert row.detail["rounds"] == 1 and row.detail["failures"] == 0
                assert row.detail["target"] == server.base_url.removeprefix("http://")
            finally:
                await container.shutdown()
