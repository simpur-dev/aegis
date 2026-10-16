"""跨模块共用的两样东西：同名 logger（日志归属不因搬家而变）与连接串脱敏 `target_of`。（P2 纯搬运：函数体逐字未改。）"""

from __future__ import annotations

import logging
from urllib.parse import urlsplit

#: 与拆分前同名，日志归属不因搬家而改变。
log = logging.getLogger("aegis.integrations")


def target_of(uri: str) -> str:
    """连接串的可公开目标段（host[:port]）：scheme、凭据、路径与参数一律丢弃。

    用 `urlsplit` 而不是按 '@' 切串：手写切分很容易把凭据段当成主机名（这里曾错过一次），
    而状态接口是匿名可读的。主机名取不到就返回空串——宁可少报，不猜。
    """
    if not uri:
        return ""
    parts = urlsplit(uri if "://" in uri else f"aegis://{uri}")
    try:
        port = parts.port
    except ValueError:
        port = None
    host = parts.hostname or ""
    return f"{host}:{port}" if host and port else host
