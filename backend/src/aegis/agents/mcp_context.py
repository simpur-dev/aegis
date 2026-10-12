"""研判取数的 MCP 门面（§4.2 阶段 2 / D4）：同一份佐证改从平台自己的标准工具面取。

要替换的是"智能体内环裸调 REST"这个临时做法（方案 D4 那一行写的就是它），REST 那套端点保留为**兼容面**：
外部团队按契约接入时可以先不装 SDK，两条门面取的是同一批能力、同一个 provider。

三条诚实边界：
- 只读工具：这一层不发写请求，也不碰存储层（内环读存储层是铁律 3 的禁区，`RestAssessContext` 守着，这里同样守）；
- 一条腿缺席/失败 → 该腿【留白】并把原因放进 `ctx["degraded"]`，与 REST 实现严格同一口径。
  两套口径是本仓出事最多的形态（#15 的原始事故就是"配了令牌"与"真的在拦"脱钩），
  所以参数推导（问什么词、发哪个灾种）直接复用 `rest_context` 里的同一份函数，不在这里另写一遍；
- 连不上工具面时**不悄悄退回 REST**：那会把"这条腿现在是坏的"读成"走了另一条好腿"。
  回退是运维的决定（`AEGIS_AGENT_CONTEXT_FACE=rest`），不是每个请求的即兴。
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from aegis.agents.rest_context import first_hazard, hazard_param

#: 工具名 → 上下文里的键名。写死这一张表是因为上下文键名会进提示词，改名等于改提示词。
_CONTEXT_KEYS = {"cases.search": "cases", "retrieval.search": "retrieval"}


class McpAssessContext:
    """`AssessContextProvider` 的 MCP 实现：一次佐证 = 一次会话里的两次只读工具调用。

    会话按【每次取数】开合，不做跨请求长连：长连要管重连、管并发、管进程停服时的在途调用，
    而这里一次调用只有两个工具、预算 1.5s 量级——先让它在真栈上量出数，再决定值不值那个复杂度。
    """

    def __init__(
        self,
        *,
        url: str,
        token: str = "",
        timeout_s: float = 3.0,
        case_limit: int = 3,
        search_k: int = 5,
        tool_caller: Callable[[str, dict[str, Any]], Awaitable[tuple[bool, str]]] | None = None,
    ) -> None:
        self._url = url
        self._token = token
        self._timeout = timeout_s
        self._case_limit = case_limit
        self._search_k = search_k
        #: 与 `RestAssessContext(transport=…)` 同一用途：单测注入假传输，真路径由起真服务器的用例覆盖。
        self._tool_caller = tool_caller

    async def corroborate(self, *, region_code: str, hazard_hint: str | None, hits: list[Any]) -> dict[str, Any]:
        query = (hazard_hint or first_hazard(hits) or "").strip()
        hazard = hazard_param(hazard_hint) or hazard_param(first_hazard(hits))
        args: dict[str, dict[str, Any]] = {
            "cases.search": {
                "query": query,
                "region_code": region_code,
                "hazard_type": hazard or "",
                "limit": self._case_limit,
            },
            # 检索腿没有区划/灾种参数（与 `GET /api/v1/retrieval/search` 同一条查询的入参面一致）：
            # 传了会被协议层按 inputSchema 拒掉，那是一次"参数对不上"的失败而不是查无结果。
            "retrieval.search": {"query": query, "k": self._search_k, "rerank": True},
        }

        ctx: dict[str, Any] = {}
        degraded: list[str] = []
        for tool, label in _CONTEXT_KEYS.items():
            if not query and label == "retrieval":
                # 没词就不发这一腿，也不记降级（与 REST 门面同一口径，理由见 `rest_context.corroborate`）：
                # 上下文里【没有 retrieval 键】本身就是"这次没查"的完整表述，降级清单只放真故障。
                continue
            try:
                is_error, text = await self._call(tool, args[tool])
            except Exception as exc:  # 传输层/握手失败：两条腿都记同一条成因
                degraded.append(f"{label}: 工具面不可达 {type(exc).__name__}: {str(exc)[:160]}")
                continue
            if is_error:
                degraded.append(f"{label}: {text[:200]}")
                continue
            items = _items_of(text)
            if items:
                ctx[label] = items
        ctx["degraded"] = degraded
        return ctx

    async def _call(self, tool: str, arguments: dict[str, Any]) -> tuple[bool, str]:
        if self._tool_caller is not None:
            return await self._tool_caller(tool, arguments)
        return await _sdk_call(self._url, self._token, self._timeout, tool, arguments)


def _items_of(text: str) -> list[Any]:
    """工具正文 → 上下文用的 items；解不出或没有 items 就是"这一腿没东西"。

    与 `rest_context._merge` 同一口径：200 且空列表**不算降级**，也不塞空列表假装查到了东西。
    """
    try:
        body = json.loads(text)
    except (TypeError, ValueError):
        return []
    if not isinstance(body, dict):
        return []
    items = body.get("items")
    return items if isinstance(items, list) else []


async def _sdk_call(url: str, token: str, timeout_s: float, tool: str, arguments: dict[str, Any]) -> tuple[bool, str]:
    """真 SDK 客户端的一趟调用：建会话 → initialize → call_tool → 关。`mcp`/`httpx2` 都在函数内导入。"""
    import httpx2
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from mcp.types import TextContent

    headers = {"authorization": f"Bearer {token}"} if token else {}
    client = httpx2.AsyncClient(headers=headers, timeout=timeout_s)
    streams = streamable_http_client(url, http_client=client)
    read, write = await streams.__aenter__()
    session = ClientSession(read, write)
    try:
        await session.__aenter__()
        await session.initialize()
        result = await session.call_tool(tool, arguments)
        # 只吃 TextContent：工具正文按协议可以是多种内容类型，`getattr(part, "text")` 那种写法
        # 会把非文本内容静默拼成空串，表现是"工具回了话而这里读成没回"。
        text = "".join(item.text for item in result.content if isinstance(item, TextContent))
        return bool(result.is_error), text
    finally:
        await session.__aexit__(None, None, None)
        await streams.__aexit__(None, None, None)
        await client.aclose()


__all__ = ["McpAssessContext"]
