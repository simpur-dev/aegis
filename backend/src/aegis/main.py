"""服务入口：uvicorn 启动（Linux 下自动启用 uvloop 提升并发吞吐）。"""

from __future__ import annotations

import logging

import uvicorn

from aegis.api.app import create_app
from aegis.config import get_settings
from aegis.logging import setup_logging

log = logging.getLogger("aegis.main")


def build_cli() -> uvicorn.Config:
    settings = get_settings()
    uvicorn_settings: dict[str, object] = {
        "host": settings.http_host,
        "port": settings.http_port,
        "log_config": None,  # 交由本项目结构化日志接管
        "access_log": False,
        "ws": "websockets",
    }
    if settings.env == "prod":
        uvicorn_settings.update({"workers": 4, "backlog": 2048, "limit_concurrency": 500})
    else:
        uvicorn_settings.update({"reload": False})

    # uvloop 仅在非 Windows 可用，缺失时自动回退标准事件循环
    try:
        import uvloop  # noqa: F401

        uvicorn_settings["loop"] = "uvloop"
    except ImportError:
        log.info("uvloop 不可用，使用标准 asyncio 事件循环")

    return uvicorn.Config(create_app(), **uvicorn_settings)  # type: ignore[arg-type]


def main() -> None:
    settings = get_settings()
    setup_logging(settings.log_level, json_output=settings.env != "dev")
    config = build_cli()
    log.info("启动 AEGIS 平台", extra={"host": settings.http_host, "port": settings.http_port, "bus": settings.bus_backend})
    uvicorn.Server(config).run()


if __name__ == "__main__":
    main()
