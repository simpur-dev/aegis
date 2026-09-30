"""DSN 归一化：把运行配置里的连接串收敛成 asyncpg 能直接吃的形式。

deploy/docker-compose.yml 注入的是 SQLAlchemy 口径的 `postgresql+asyncpg://…`，
asyncpg 只认 `postgresql://`；这层差异在此收敛，并顺带提供口令脱敏供日志使用。
"""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

from aegis.persistence.errors import DsnError

SUPPORTED_SCHEMES = frozenset({"postgresql", "postgres"})
DEFAULT_PORT = 5432


def normalize_dsn(raw: str) -> str:
    """归一化连接串：剥离 SQLAlchemy 驱动后缀、校验协议与目标库，查询串原样透传给 asyncpg。"""
    text = (raw or "").strip()
    if not text:
        raise DsnError("DSN 为空：需要 AEGIS_DB_URL 或显式 dsn 参数")
    scheme, separator, rest = text.partition("://")
    if not separator or not rest:
        raise DsnError("DSN 缺少 :// 分隔符", detail={"dsn": redact_dsn(text)})
    base = scheme.partition("+")[0].lower()
    if base not in SUPPORTED_SCHEMES:
        raise DsnError("不支持的数据库协议：持久层只接 PostgreSQL", detail={"scheme": base})
    rebuilt = urlunsplit((base, rest, "", "", ""))
    try:
        parts = urlsplit(rebuilt)
        port = parts.port
    except ValueError as exc:
        raise DsnError("DSN 端口不合法", detail={"dsn": redact_dsn(rebuilt)}) from exc
    if not parts.hostname:
        raise DsnError("DSN 缺少主机名", detail={"dsn": redact_dsn(rebuilt)})
    if not parts.path or parts.path == "/":
        raise DsnError("DSN 缺少数据库名", detail={"dsn": redact_dsn(rebuilt)})
    if port is not None and not 0 < port <= 65_535:
        raise DsnError("DSN 端口越界", detail={"port": port})
    return urlunsplit((base, parts.netloc, parts.path, parts.query, parts.fragment))


def redact_dsn(raw: str) -> str:
    """隐去口令后的连接串：日志与错误 detail 只用它，避免凭据落盘。"""
    text = (raw or "").strip()
    try:
        parts = urlsplit(text)
        if parts.password is None:
            return text
    except ValueError:
        return "<无法解析的 DSN>"
    host = parts.netloc.rsplit("@", 1)[-1]
    return urlunsplit((parts.scheme, f"{parts.username or ''}:***@{host}", parts.path, parts.query, parts.fragment))


def dsn_label(raw: str) -> str:
    """连接目标的可读标签（host:port/db），用于日志与 readyz。"""
    parts = urlsplit(normalize_dsn(raw))
    return f"{parts.hostname}:{parts.port or DEFAULT_PORT}/{parts.path.lstrip('/')}"
