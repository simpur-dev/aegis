"""API 依赖：请求级取容器、解析服务令牌——数据面 / 工作流面 / 助手面共用同一份。

`get_container` 原本在 `app.py`、`workflow_api.py`、`assistant_api.py` 里各写了一遍（三份逐字相同）。
"平台尚未就绪回 503" 这种口径有三处实现，改一处忘两处就是迟早的事，所以这里收成一处、其余两处引用它。
"""

from __future__ import annotations

from collections.abc import Mapping

from fastapi import HTTPException, Request

from aegis.container import PlatformContainer


def get_container(request: Request) -> PlatformContainer:
    container: PlatformContainer | None = getattr(request.app.state, "container", None)
    if container is None:
        raise HTTPException(status_code=503, detail="平台尚未就绪")
    return container


def presented_token(headers: Mapping[str, str]) -> str:
    """从请求头取服务令牌：接受 `X-Aegis-Token` 与 `Authorization: Bearer` 两种写法，取到一个算一个。

    为什么必须两个都认：只认一个头名时，"配了令牌"与"真的在拦"是两件事——本仓的智能体 REST 客户端
    与通用 MCP 客户端发的都是 Bearer，而服务端只读 X-Aegis-Token，结果是配置打开后合法客户端被一律 401、
    或者反过来（取决于谁先改）配了等于没配。两个面（数据 API 与 MCP 工具面）共用这一个函数，
    免得它们各自演化出第二套口径。
    """
    direct = headers.get("x-aegis-token") or ""
    if direct:
        return direct
    scheme, _, value = (headers.get("authorization") or "").partition(" ")
    return value.strip() if scheme.lower() == "bearer" else ""
