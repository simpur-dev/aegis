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

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from aegis.api.deps import OperatorKeyHeader, get_container
from aegis.container import PlatformContainer
from aegis.domain.operator import Operator
from aegis.services.assistant import ERROR_STATUS, AssistantService
from aegis.services.assistant.base import ActionError
from aegis.services.operator_auth import OperatorKeyError

log = logging.getLogger("aegis.api.assistant")

_DISCONNECT_POLL_SECONDS = 0.25
_SSE_KEEPALIVE_SECONDS = 10.0
_SESSION_PATTERN = r"^[A-Za-z0-9_-]{4,64}$"
_ACTION_ID_PATTERN = r"^act_[0-9a-f]{6,24}$"

OperatorHeader = Annotated[str | None, Header(alias="X-Aegis-Operator")]


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=2_000)
    session_id: str | None = Field(default=None, pattern=_SESSION_PATTERN)
    reporter: str | None = Field(default=None, min_length=2, max_length=64)
    region_code: str | None = Field(default=None, pattern=r"^[0-9A-Z]{6,24}$")
    hazard_hint: str | None = Field(default=None, max_length=64)


class CreateActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: str = Field(min_length=3, max_length=64)
    params: dict[str, Any] = Field(default_factory=dict)
    space_id: str | None = Field(default=None, max_length=64)
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=64)
    utterance: str = Field(default="", max_length=500)


class ConfirmActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actor: str | None = Field(default=None, max_length=64)


def _assistant(container: PlatformContainer) -> AssistantService:
    if container.assistant is None:
        # 装配事实而不是"500 才看得见"：语义腿没接上时前端要能读到"未配置"
        raise HTTPException(
            status_code=503,
            detail={"code": "E_ASSISTANT_UNAVAILABLE", "message": "语义交互服务未装配（assistant）"},
        )
    return container.assistant


async def _operator(container: PlatformContainer, operator_id: str | None) -> Operator | None:
    """席位头 → 席位。缺席位或台账不可用时返回 None：读动作允许匿名，写动作由执行器拒绝。

    这里不抛 401/403：`GET /capabilities` 必须能在"还没选席位"时返回完整动作清单，
    否则前端连"该选哪个角色"都显示不出来。
    """
    if not operator_id or container.operators is None or not container.operators.available:
        return None
    operator = await container.operators.get(operator_id)
    return operator if operator is not None and operator.active else None


async def _identity(
    container: PlatformContainer,
    operator_id: str | None,
    operator_key: str | None,
) -> tuple[Operator | None, str]:
    """身份解析的**唯一实现**（P4 强认证，设计 §14.1.1）。

    口径：按人令牌优先，其次声明身份。
    - 带了有效令牌 → 身份是**令牌绑定的那个人**（`identity_kind='key'`）；
      令牌与席位头不一致 → 401（借别人席位的事不能当成没发生）。
    - 没带令牌 → 声明身份（`identity_kind='declared'`，可伪造，这是 P0–P3 的既有口径）。
    - 带了无效令牌 → 401（"带了凭据但不对"与"没带凭据"是两种事实，不能混成后者）。

    令牌错误在这里就翻成 401：它是一个 HTTP 事实（凭据不对），
    不是动作级的业务拒绝，交给执行器的 `ActionError` 通道会变成 422。
    """
    auth = container.operator_auth
    if auth is not None and auth.enabled and operator_key:
        try:
            outcome = await auth.authenticate(raw_key=operator_key, claimed_operator_id=operator_id)
        except OperatorKeyError as exc:
            raise HTTPException(
                status_code=401,
                detail={"code": exc.reason, "message": exc.message},
            ) from exc
        return outcome.operator, outcome.identity_kind
    return await _operator(container, operator_id), "declared"


def _require_strong_identity(container: PlatformContainer, operator: Operator | None, identity_kind: str) -> None:
    """写动作的强身份门禁（设计 §14.1.1）。

    强认证打开时，写动作必须带**有效令牌**；只带席位头 → 401。
    读动作不受影响——强认证不等于"什么都得登录"。
    开关关着时这道门不存在（声明身份照旧），部署方没启用强认证就不该被拦。
    """
    auth = container.operator_auth
    if auth is None or not auth.enabled:
        return
    if identity_kind == "key" and operator is not None:
        return
    raise HTTPException(
        status_code=401,
        detail={
            "code": "E_OPERATOR_KEY_REQUIRED",
            "message": "该操作需要按人令牌：请带上 X-Aegis-Operator-Key（写动作在强认证开启后不接受仅声明的席位）",
        },
    )


def build_router() -> APIRouter:
    router = APIRouter(prefix="/api/v1/assistant", tags=["assistant"])

    @router.get("/capabilities")
    async def capabilities(
        ctn: PlatformContainer = Depends(get_container),
        x_operator: OperatorHeader = None,
        x_operator_key: OperatorKeyHeader = None,
    ) -> dict[str, Any]:
        service = _assistant(ctn)
        # 能力面与写动作同一条身份解析：只看声明头会把"带了令牌的人"读成另一个角色，
        # 页面据此灰掉的按钮与执行器实际判定就对不上了。
        operator, _ = await _identity(ctn, x_operator, x_operator_key)
        return service.capabilities(None if operator is None else operator.role)

    @router.post("/chat")
    async def chat(
        payload: ChatRequest,
        request: Request,
        ctn: PlatformContainer = Depends(get_container),
        x_operator: OperatorHeader = None,
        x_operator_key: OperatorKeyHeader = None,
    ) -> Response:
        service = _assistant(ctn)
        operator, _ = await _identity(ctn, x_operator, x_operator_key)
        stream = service.respond(
            payload.message,
            session_id=payload.session_id,
            operator=operator,
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

    # ---------- 动作资源（`/confirm` 已退役：动作有自己的号，不再靠 session 定位） ----------

    @router.post("/actions")
    async def create_action(
        payload: CreateActionRequest,
        ctn: PlatformContainer = Depends(get_container),
        x_operator: OperatorHeader = None,
        x_operator_key: OperatorKeyHeader = None,
    ) -> dict[str, Any]:
        service = _assistant(ctn)
        operator, identity_kind = await _identity(ctn, x_operator, x_operator_key)
        _require_strong_identity(ctn, operator, identity_kind)
        try:
            return await service.create_action(
                payload.action,
                payload.params,
                operator=operator,
                space_id=payload.space_id,
                idempotency_key=payload.idempotency_key,
                utterance=payload.utterance,
            )
        except ActionError as exc:
            raise _http_error(exc) from exc

    @router.get("/actions/{action_id}")
    async def action_status(
        action_id: Annotated[str, Path(pattern=_ACTION_ID_PATTERN)],
        ctn: PlatformContainer = Depends(get_container),
    ) -> dict[str, Any]:
        service = _assistant(ctn)
        record = await service.action_status(action_id)
        if record is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "E_ACTION_NOT_FOUND", "message": f"动作不存在：{action_id}"},
            )
        return record

    @router.post("/actions/{action_id}/confirm")
    async def confirm_action(
        action_id: Annotated[str, Path(pattern=_ACTION_ID_PATTERN)],
        ctn: PlatformContainer = Depends(get_container),
        x_operator: OperatorHeader = None,
        x_operator_key: OperatorKeyHeader = None,
        payload: ConfirmActionRequest | None = None,
    ) -> dict[str, Any]:
        service = _assistant(ctn)
        operator, identity_kind = await _identity(ctn, x_operator, x_operator_key)
        _require_strong_identity(ctn, operator, identity_kind)
        try:
            return await service.confirm(
                action_id=action_id,
                actor=None if payload is None else payload.actor,
                operator=operator,
                # 二次认证用**原始令牌**，不复用已认证后的身份：这一道要验的就是"再证明一次"，
                # 拿已经通过的身份充数等于这道门形同虚设。
                operator_key=x_operator_key,
            )
        except ActionError as exc:
            # 双人复核的资格/重复票是 HTTP 事实（401/403/409），不是"确认没成功"的回执。
            raise _http_error(exc) from exc

    @router.post("/actions/{action_id}/revoke")
    async def revoke_action(
        action_id: Annotated[str, Path(pattern=_ACTION_ID_PATTERN)],
        ctn: PlatformContainer = Depends(get_container),
        x_operator: OperatorHeader = None,
        x_operator_key: OperatorKeyHeader = None,
    ) -> dict[str, Any]:
        service = _assistant(ctn)
        operator, identity_kind = await _identity(ctn, x_operator, x_operator_key)
        _require_strong_identity(ctn, operator, identity_kind)
        return await service.revoke_action(action_id=action_id, operator=operator)

    return router


def _http_error(exc: ActionError) -> HTTPException:
    """动作错误码 → HTTP：映射只查 `ERROR_STATUS` 一张表，路由里不写 if/elif。"""
    return HTTPException(
        status_code=ERROR_STATUS.get(exc.code, 400),
        detail={"code": exc.code, "message": exc.message, **exc.detail},
    )


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
