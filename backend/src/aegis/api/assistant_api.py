"""/api/v1/assistant —— 语义交互出口（对话 SSE + 人工确认）。

沿用 `/api/v1/events/stream` 的 SSE 基建口径：注释帧 keep-alive、慢消费者丢最旧、
长连接生命周期严格短于关停/断连窗口。这里刻意**不做 token 级伪流式**：
LLM 网关只有一次完整问答（`chat`/`chat_json`），把整段回答切成小块假装流式输出，
前端看着爽但没有任何新增事实，反而掩盖了"哪一步在等什么"。因此帧序列是过程事实：
`meta → intent → status → result → answer → done`，执行类动作再插一帧 `proposal`。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from aegis.container import PlatformContainer
from aegis.services.assistant import AssistantService

log = logging.getLogger("aegis.api.assistant")

_DISCONNECT_POLL_SECONDS = 0.25
_SSE_KEEPALIVE_SECONDS = 10.0
_SESSION_PATTERN = r"^[A-Za-z0-9_-]{4,64}$"


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=2_000)
    session_id: str | None = Field(default=None, pattern=_SESSION_PATTERN)
    reporter: str | None = Field(default=None, min_length=2, max_length=64)
    region_code: str | None = Field(default=None, pattern=r"^[0-9A-Z]{6,24}$")
    hazard_hint: str | None = Field(default=None, max_length=64)


class ConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(pattern=_SESSION_PATTERN)
    action_id: str = Field(pattern=r"^act_[0-9a-f]{6,24}$")
    actor: str | None = Field(default=None, max_length=64)


def get_container(request: Request) -> PlatformContainer:
    container: PlatformContainer | None = getattr(request.app.state, "container", None)
    if container is None:
        raise HTTPException(status_code=503, detail="平台尚未就绪")
    return container


def _assistant(container: PlatformContainer) -> AssistantService:
    if container.assistant is None:
        # 装配事实而不是"500 才看得见"：语义腿没接上时前端要能读到"未配置"
        raise HTTPException(
            status_code=503,
            detail={"code": "E_ASSISTANT_UNAVAILABLE", "message": "语义交互服务未装配（assistant）"},
        )
    return container.assistant


def build_router() -> APIRouter:
    router = APIRouter(prefix="/api/v1/assistant", tags=["assistant"])

    @router.get("/capabilities")
    async def capabilities(ctn: PlatformContainer = Depends(get_container)) -> dict[str, Any]:
        service = _assistant(ctn)
        return service.capabilities()

    @router.post("/chat")
    async def chat(
        payload: ChatRequest,
        request: Request,
        ctn: PlatformContainer = Depends(get_container),
    ) -> Response:
        service = _assistant(ctn)
        stream = service.respond(
            payload.message,
            session_id=payload.session_id,
            reporter=payload.reporter,
            region_code=payload.region_code,
            hazard_hint=payload.hazard_hint,
        )

        async def _gen() -> AsyncIterator[str]:
            """异常必须成为一帧 `error` 事件：SSE 半途断流时前端只会看到"一直转圈"。"""
            try:
                async for event in stream:
                    yield f"data: {json.dumps(event.as_dict(), ensure_ascii=False)}\n\n"
            except Exception as exc:  # pragma: no cover - 由 respond 内部降级兜住大多数路径
                log.warning("助手流式响应异常", extra={"err": f"{type(exc).__name__}: {exc}"[:200]})
                frame = {"type": "error", "message": f"{type(exc).__name__}"}
                yield f"data: {json.dumps(frame, ensure_ascii=False)}\n\n"
            finally:
                await stream.aclose()

        return StreamingResponse(_keepalive(_gen(), stopping=ctn.stopping, request=request), media_type="text/event-stream")

    @router.post("/confirm")
    async def confirm(payload: ConfirmRequest, ctn: PlatformContainer = Depends(get_container)) -> dict[str, Any]:
        service = _assistant(ctn)
        result = await service.confirm(session_id=payload.session_id, action_id=payload.action_id, actor=payload.actor)
        return result

    @router.get("/sessions/{session_id}")
    async def session(
        session_id: Annotated[str, Path(min_length=4, max_length=64, pattern=_SESSION_PATTERN)],
        ctn: PlatformContainer = Depends(get_container),
    ) -> dict[str, Any]:
        service = _assistant(ctn)
        state = service.session(session_id)
        if state is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "E_SESSION_NOT_FOUND", "message": "会话不存在或已过期", "session_id": session_id},
            )
        return state

    return router


async def _keepalive(
    source: AsyncIterator[str],
    *,
    stopping: bool,
    request: Request,
) -> AsyncIterator[str]:
    """把"没有新事件"与"客户端还在等"分开：空闲到点补注释帧，停服或断连立刻收摊。

    `shield` 是必需的：`wait_for` 超时若直接取消 `__anext__()`，那一帧回答就丢了——
    对话半途消失比慢一点糟得多。
    """
    iterator = source.__aiter__()
    pending: asyncio.Future[Any] = asyncio.ensure_future(iterator.__anext__())
    idle = 0.0
    try:
        while True:
            try:
                frame = await asyncio.wait_for(asyncio.shield(pending), timeout=_DISCONNECT_POLL_SECONDS)
            except TimeoutError:
                if stopping or await request.is_disconnected():
                    return
                idle += _DISCONNECT_POLL_SECONDS
                if idle >= _SSE_KEEPALIVE_SECONDS:
                    idle = 0.0
                    yield ": keep-alive\n\n"
                continue
            except StopAsyncIteration:
                return
            idle = 0.0
            pending = asyncio.ensure_future(iterator.__anext__())
            yield frame
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        aclose = getattr(iterator, "aclose", None)
        if aclose is not None:
            await aclose()
