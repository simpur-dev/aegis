"""智能体的 REST 取数门面（§3 / §6"五智能体 API 版"、规范 §4）：内环经平台 HTTP 出口拿佐证，不碰存储层。

为什么是 REST 而不是直接 import：D4 把 MCP 工具层排在 MB，MA 阶段智能体只能按规范 §4 走 REST；
而内环若直接引用检索/知识腿，既破铁律 3 的分层，也让"外部团队按契约接入（换个 agent_id 即可）"
无路可走——外部智能体手上只有 HTTP。

诚实边界：
- 只读：不发任何写请求，不触发任何副作用（案例入库那条腿明确不进自动链路）；
- 某条腿 503 / 超时 / 非 2xx → 该腿【留白】并把原因放进 `ctx["degraded"]`，由 assess 原样带进
  响应（对齐"降级必须可见"），绝不把"没查到"写成"查过了没有"，也绝不拿空上下文继续装作有佐证；
- 端点与查询参数取自 `api/routers/knowledge.py` 的真实路由（`/api/v1/cases/recall`、`/api/v1/retrieval/search`），
  单测用 `httpx.MockTransport` 发真请求、走真解析：不起服务、不假造返回体。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import httpx

from aegis.domain.enums import HazardType

CASES_PATH = "/api/v1/cases/recall"
SEARCH_PATH = "/api/v1/retrieval/search"


def first_hazard(hits: list[Any]) -> str:
    for hit in hits:
        hazard = hit.get("hazard_type") if isinstance(hit, dict) else getattr(hit, "hazard_type", None)
        if hazard:
            return str(hazard)
    return ""


def _unavailable(detail: Any) -> str:
    """503 的 detail 是"哪条腿没装配"的人话，直接搬进降级原因，不翻译成"无数据"。"""
    return str(detail) if detail else ""


class RestAssessContext:
    """`AssessContextProvider` 的 REST 实现：研判要的案例与检索佐证都从平台自己的 API 出口取。

    参数推导用模块级的 `first_hazard`/`hazard_param`——MCP 门面（`mcp_context.py`）复用同一份，
    两条门面各写一遍"问什么词、发哪个灾种"迟早长出不一致的口径。

    **HTTP 客户端是复用的，不是每次请求新建**（2026-11-10 实测）：
    `httpx.AsyncClient` 每次构造都要重建 SSL context 并**从磁盘重载 CA 证书包**
    （`_ssl._SSLContext.load_verify_locations`，实测 **11 ms/次**纯 CPU，且**阻塞事件循环**——
    一次 `corroborate()` 两个请求 = 22 ms 的事件循环卡顿）。
    复用后连接池与 SSL context 都只建一次，实测 28.3 → 1.4 ms/次（20×）。
    与 MCP 门面的差别：MCP 那边是**协议级会话**（connect/initialize/call/close），
    开合是协议要求；这边是纯 HTTP，keep-alive 本来就该用。
    """

    def __init__(
        self,
        *,
        base_url: str,
        token: str = "",
        timeout_s: float = 1.5,
        case_limit: int = 3,
        search_k: int = 5,
        transport: httpx.AsyncBaseTransport | None = None,
        client_factory: Callable[..., Any] | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout_s
        self._case_limit = case_limit
        self._search_k = search_k
        self._transport = transport
        #: 与 `connectors/weather_api.py` 同款的可注入 client 工厂：单测换 MockTransport 不必动全局。
        self._client_factory = client_factory or httpx.AsyncClient
        self._client: Any = None
        self._client_lock = asyncio.Lock()

    async def _get_client(self) -> Any:
        """取（或首次建）复用的 client。

        首次建要 11 ms（重载 CA 证书包），所以只在第一次付；之后每次调用是零成本的。
        用锁串行化首次创建——并发研判时不该有两个线程各建一个 client。
        """
        if self._client is not None:
            return self._client
        async with self._client_lock:
            if self._client is None:
                self._client = self._client_factory(timeout=self._timeout, transport=self._transport)
        return self._client

    async def aclose(self) -> None:
        """关掉自己建的 client。

        谁建谁关（与 `connectors/weather_api.py` 同一口径）：注入进来的 transport
        归注入方，本类只关自己建的 client。
        """
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()

    async def corroborate(self, *, region_code: str, hazard_hint: str | None, hits: list[Any]) -> dict[str, Any]:
        query = (hazard_hint or first_hazard(hits) or "").strip()
        params: dict[str, Any] = {}
        if region_code:
            params["region_code"] = region_code
        if query:
            params["q"] = query
        hazard = hazard_param(hazard_hint) or hazard_param(first_hazard(hits))
        if hazard:
            params["hazard_type"] = hazard

        ctx: dict[str, Any] = {}
        degraded: list[str] = []
        cases = await self._get(CASES_PATH, {**params, "limit": self._case_limit})
        _merge(ctx, degraded, "cases", cases)
        if query:
            retrieval = await self._get(SEARCH_PATH, {**{k: v for k, v in params.items() if k != "hazard_type"}, "k": self._search_k})
            _merge(ctx, degraded, "retrieval", retrieval)
        else:
            # 与 `mcp_context` 同一口径：`q` 是这个路由的必填非空参数，没词硬发只换来一个 422。
            # 记进 `degraded` 会把"这次没有可搜的词"说成故障——真链路里 hits 为空的事件（人工上报
            # 未越限那种）本来就没词，一天里能把它刷成噪声，最后没人再看这条清单。
            pass
        ctx["degraded"] = degraded
        return ctx

    async def _get(self, path: str, params: dict[str, Any]) -> tuple[int, Any]:
        """返回 (状态码, 解析后的 JSON)；任何传输层异常都归成 (0, 原因字符串)，由上层记降级。"""
        headers = {"accept": "application/json"}
        if self._token:
            headers["authorization"] = f"Bearer {self._token}"
        try:
            client = await self._get_client()
            # 头带在请求上而不是 client 上：client 是复用的，令牌可能换（换令牌不该重建 client）。
            response = await client.get(f"{self._base}{path}", params=params, headers=headers)
        except httpx.TimeoutException:
            return 0, "请求超时"
        except httpx.HTTPError as exc:
            return 0, f"{type(exc).__name__}: {exc}"
        try:
            body: Any = response.json()
        except ValueError:
            body = None
        return response.status_code, body


def hazard_param(value: str | None) -> str | None:
    """只有落在灾种枚举里的值才发出去：接口对该参数做枚举校验，乱填会换来一个 422。"""
    if not value:
        return None
    try:
        return HazardType(value).value
    except ValueError:
        return None


def _merge(ctx: dict[str, Any], degraded: list[str], label: str, outcome: tuple[int, Any]) -> None:
    status, body = outcome
    if status == 200 and isinstance(body, dict):
        items = body.get("items")
        if isinstance(items, list) and items:
            ctx[label] = items
            return
        # 200 且确无命中：这一腿就是"没有"，不算降级，也不塞空列表假装查到了东西。
        return
    if status == 0:
        degraded.append(f"{label}: {body}")
    elif status == 503:
        detail = body.get("detail") if isinstance(body, dict) else None
        degraded.append(f"{label}: {_unavailable(detail) or '该能力未装配（503）'}")
    else:
        degraded.append(f"{label}: HTTP {status}")
