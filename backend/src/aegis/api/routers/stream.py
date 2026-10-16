"""事件流（SSE）：把总线上的预警与反馈帧推给大屏，一连接一订阅、断开即取消。

两条常量是从真机上量出来的，不是拍的：空闲流若不发第一个字节，浏览器 15s 内不会 open；
而注释帧 JS 看不见，后端进程死了页面仍会显示"已连接"。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, Request, Response

from aegis.api.deps import get_container
from aegis.bus import subjects
from aegis.container import PlatformContainer
from aegis.domain.messages import AgentMessage, utc_now

#: 断连轮询间隔：ASGI 不推送"客户端已断开"事件，只能轮询；0.25s 足够快且不必空转。
_DISCONNECT_POLL_SECONDS = 0.25
#: 空闲多久补一帧注释帧，防止中间设备掐掉长连接（不是业务事件，客户端会忽略）。
_SSE_KEEPALIVE_SECONDS = 15.0


def build_router() -> APIRouter:
    router = APIRouter()

    @router.get("/api/v1/events/stream", tags=["stream"])
    async def stream(request: Request, ctn: PlatformContainer = Depends(get_container)) -> Response:
        from fastapi.responses import StreamingResponse

        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=512)

        async def _on_message(message: AgentMessage) -> None:
            item = {
                "subject": message.action,
                "trace_id": message.trace_id,
                "payload": message.payload,
                "ts": message.ts,
            }
            if queue.full():
                queue.get_nowait()  # 慢消费者丢最旧事件，保证不阻塞总线
            await queue.put(item)

        subs = [
            await ctn.transport.subscribe(subjects.alert(1, ">"), _on_message),
            await ctn.transport.subscribe("platform.alert.>", _on_message),
            await ctn.transport.subscribe(subjects.ops("feedback", "status"), _on_message),
        ]

        async def _gen() -> AsyncIterator[str]:
            """长连接生命周期严格短于关停/断连窗口：否则 uvicorn 默认无限等待优雅退出。"""
            # 一开流就冲一帧注释：浏览器 EventSource 要收到第一个字节才触发 open，
            # 而空闲流原本要等 `_SSE_KEEPALIVE_SECONDS`（15s）才有第一个字节。
            # 真机量到 open 落在 15.27s——也就是说每次进页面都有 15 秒顶着一句
            # "事件流重连中"，可它其实连得好好的。假告警比没告警更坏。
            yield ": open\n\n"
            idle = 0.0
            try:
                while not ctn.stopping and not await request.is_disconnected():
                    try:
                        item = await asyncio.wait_for(queue.get(), timeout=_DISCONNECT_POLL_SECONDS)
                    except TimeoutError:
                        idle += _DISCONNECT_POLL_SECONDS
                        if idle >= _SSE_KEEPALIVE_SECONDS:
                            idle = 0.0
                            # 心跳必须是**看得见的一帧**，不能是注释帧。
                            # 真机把后端进程杀掉之后：/healthz 立刻不可达、取数一直失败，
                            # 而页面上的 EventSource 仍然认为自己"已连接"——浏览器不知道上游断了，
                            # 注释帧 JS 也永远看不到，于是那个徽标在最需要它说话的时候撒谎。
                            yield f"data: {json.dumps({'type': 'heartbeat', 'ts': utc_now().isoformat()}, ensure_ascii=False)}\n\n"
                        continue
                    yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
            finally:
                for sub in subs:
                    await sub.cancel()

        return StreamingResponse(_gen(), media_type="text/event-stream")

    return router
