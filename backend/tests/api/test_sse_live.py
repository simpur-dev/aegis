"""SSE 事件流实测：起真实 uvicorn 服务器验证流式推送。

为什么不用 ASGITransport：httpx 的 ASGI 传输会缓冲整个响应体，无法逐块读取 SSE，
用它测流式接口会得到"假失败"。这里用真实服务器 + 真实 HTTP 流。
"""

from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator

import httpx
import pytest
import uvicorn

from aegis.api.app import create_app
from aegis.config import Settings


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture
async def live() -> AsyncIterator[httpx.AsyncClient]:
    port = _free_port()
    settings = Settings(env="dev", bus_backend="memory", delivery_mode="mock", http_port=port, simulator_seed=33)
    server = uvicorn.Server(uvicorn.Config(create_app(settings), host="127.0.0.1", port=port, log_level="warning"))
    task = asyncio.create_task(server.serve())
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=20.0) as client:
        for _ in range(200):
            try:
                if (await client.get("/healthz")).status_code == 200:
                    break
            except httpx.HTTPError:
                await asyncio.sleep(0.05)
        else:  # pragma: no cover - 服务器未能起来时快速失败
            pytest.fail("uvicorn 服务器未在超时内就绪")
        yield client
        server.should_exit = True
    await asyncio.wait_for(task, timeout=15.0)


class TestLiveServer:
    async def test_health_and_ready_over_http(self, live: httpx.AsyncClient) -> None:
        assert (await live.get("/healthz")).json()["status"] == "ok"
        assert (await live.get("/readyz")).json()["status"] == "ready"

    async def test_openapi_available(self, live: httpx.AsyncClient) -> None:
        response = await live.get("/openapi.json")
        assert response.status_code == 200
        assert "/api/v1/drill/run" in response.json()["paths"]

    async def test_sse_streams_warning_event(self, live: httpx.AsyncClient) -> None:
        received: list[str] = []

        async def consume() -> None:
            async with live.stream("GET", "/api/v1/events/stream") as response:
                assert response.status_code == 200
                assert response.headers["content-type"].startswith("text/event-stream")
                async for line in response.aiter_lines():
                    if line.startswith("data: "):
                        received.append(line[len("data: ") :])
                        return

        consumer = asyncio.create_task(consume())
        try:
            await asyncio.sleep(0.3)  # 让订阅先建立
            drill = await live.post("/api/v1/drill/run", json={"scenario": "surge", "ticks": 1})
            assert drill.status_code == 200
            assert any(chain["acted"] for chain in drill.json()["chains"]), drill.json()["chains"]
            await asyncio.wait_for(consumer, timeout=15.0)
        finally:
            if not consumer.done():
                consumer.cancel()
                await asyncio.gather(consumer, return_exceptions=True)

        assert received, "SSE 未推送任何事件"
        assert "warning_id" in received[0]

    async def test_metrics_endpoint_serves_prometheus(self, live: httpx.AsyncClient) -> None:
        await live.post("/api/v1/drill/run", json={"scenario": "surge", "ticks": 1})
        response = await live.get("/metrics")
        assert response.status_code == 200
        assert "aegis_collab_txn_total" in response.text
