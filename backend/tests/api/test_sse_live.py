"""SSE 事件流实测：起真实 uvicorn 服务器验证流式推送。

为什么不用 ASGITransport：httpx 的 ASGI 传输会缓冲整个响应体，无法逐块读取 SSE，
用它测流式接口会得到"假失败"。这里用真实服务器 + 真实 HTTP 流。
"""

from __future__ import annotations

import asyncio
import json
import socket
import time
from collections.abc import AsyncIterator

import httpx
import pytest
import uvicorn

from aegis.api.app import create_app
from aegis.config import Settings

#: 判定线：第一帧必须在这一秒数内到，否则浏览器顶着一句假的"重连中"。
FIRST_FRAME_LIMIT_S = 2.0
#: 等帧天花板要宽过判定线：两处都写 2.0 时，慢的第一帧会以裸 TimeoutError 收场，
#: 那句真正有用的诊断永远走不到。
FIRST_FRAME_CEILING_S = 8.0


@pytest.fixture
async def live() -> AsyncIterator[httpx.AsyncClient]:
    """把监听 socket 自己绑好再交给 uvicorn：端口取自己绑到的那个，不存在"抢号窗口"。

    旧写法 `_free_port()` 先 bind(0) 拿号、关闭、再把同号交给 uvicorn——
    这两步之间任何进程占走该口，本机就是随机 `EADDRINUSE`。
    """
    settings = Settings(env="dev", bus_backend="memory", delivery_mode="mock", http_port=0, simulator_seed=33)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    port = int(listener.getsockname()[1])
    # uvicorn 默认无限等待在途连接结束；SSE 长连接在 Windows 的 proactor 事件循环上
    # 关闭后连接对象未必被判定为已断开，会让优雅退出永久挂起。给 1s 上限，让测试
    # 断言的是"能在超时内退出"，而不是依赖平台清理时序。
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(settings),
            host="127.0.0.1",
            port=port,
            log_level="warning",
            timeout_graceful_shutdown=1,
        )
    )
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
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
    finally:
        listener.close()


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

    async def test_打开流后立刻有第一帧而不是等保活周期(self, live: httpx.AsyncClient) -> None:
        """EventSource 要收到第一个字节才触发 `open`。

        原本空闲流的第一字节是 15 秒后的那条 keep-alive 注释，于是真机上每次进页面
        都有 15 秒顶着一句"事件流重连中"（实测 open 落在 15.27s），而连接一直是好的。
        这里断言的不是内容而是**时间**：第一帧必须在保活周期之前到达。
        """
        started = time.perf_counter()
        async with live.stream("GET", "/api/v1/events/stream") as response:
            assert response.status_code == 200
            # 等帧的天花板要比判定线宽：两处都写 2.0 时，慢的第一帧会变成裸 TimeoutError，
            # 那句"浏览器在这段时间里显示的是假的重连中"永远走不到——诊断信息自己先失效。
            first = await asyncio.wait_for(response.aiter_lines().__anext__(), timeout=FIRST_FRAME_CEILING_S)
        elapsed = time.perf_counter() - started
        assert first.startswith(":"), f"第一帧应是注释帧（不是事件），实际 {first!r}"
        assert elapsed < FIRST_FRAME_LIMIT_S, f"第一帧等了 {elapsed:.2f}s：浏览器在这段时间里显示的是假的『重连中』"

    async def test_空闲时的保活帧得是JS收得到的心跳而不是注释(self, live: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
        """注释帧（`: keep-alive`）浏览器根本不给 JS，于是这条流"看起来连着"没有任何证据。

        真机后果：把后端进程杀掉之后，`/healthz` 立刻不可达、取数一直失败，
        而页头的 SSE 徽标仍然写着"事件流已连接"——浏览器不知道上游死了。
        心跳得是一帧真的事件，前端才有"多久没动静"这个判断的依据。
        """
        from aegis.api import app as app_module

        monkeypatch.setattr(app_module, "_SSE_KEEPALIVE_SECONDS", 0.3)
        frames: list[str] = []
        async with live.stream("GET", "/api/v1/events/stream") as response:
            assert response.status_code == 200
            async with asyncio.timeout(10.0):
                async for line in response.aiter_lines():
                    if not line.strip():
                        continue
                    frames.append(line)
                    if '"heartbeat"' in line:
                        break
        beat = next((row for row in frames if '"heartbeat"' in row), None)
        assert beat is not None, f"等不到心跳帧，只读到 {frames[:6]}"
        assert beat.startswith("data: "), f"心跳必须是 JS 收得到的一帧事件，实际 {beat!r}"
        payload = json.loads(beat[len("data: ") :])
        assert payload["type"] == "heartbeat"
        assert isinstance(payload["ts"], str) and payload["ts"], "心跳要带时间，前端据此判断多久没动静"

    async def test_metrics_endpoint_serves_prometheus(self, live: httpx.AsyncClient) -> None:
        await live.post("/api/v1/drill/run", json={"scenario": "surge", "ticks": 1})
        response = await live.get("/metrics")
        assert response.status_code == 200
        assert "aegis_collab_txn_total" in response.text
