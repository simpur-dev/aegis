"""结构化日志：JSON 行输出 + trace_id 上下文注入（一次灾害事件全链路可归因）。"""

from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

trace_id_var: ContextVar[str | None] = ContextVar("aegis_trace_id", default=None)

_RESERVED = {
    "name",
    "msg",
    "args",
    "levelname",
    "levelno",
    "pathname",
    "filename",
    "module",
    "exc_info",
    "exc_text",
    "stack_info",
    "lineno",
    "funcName",
    "created",
    "msecs",
    "relativeCreated",
    "thread",
    "threadName",
    "processName",
    "process",
    "taskName",
    "message",
    "asctime",
}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        if tid := trace_id_var.get():
            payload["trace_id"] = tid
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def setup_logging(level: str = "INFO", *, json_output: bool = True) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if json_output else logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())
    # neo4j 的 INFO 会按条吐 Graphiti 建索引时的 "index already exists" 通知（一次启动几十行），
    # 对排障没有信息量；驱动自己的告警/错误仍按 WARNING 以上照常出。
    for noisy in ("uvicorn.access", "httpx", "neo4j"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def log_ctx(logger: logging.Logger, event: str, **fields: Any) -> None:
    logger.info(event, extra=fields)
