"""MCP 工具层的可选腿门面（§4.2 / D4 / R3）。

这个 `__init__` 【不许】import 任何依赖 `mcp` 包的模块：可选腿没装时，清单与可用性判定仍然要读得出来
（装配点据此写状态行、用例据此断言"没装 extra 时这条腿如实缺席"）。所以 server 只在真要装配时惰性导入。
"""

from __future__ import annotations

import importlib.util

from aegis.mcp_tools.manifest import TOOL_MANIFEST, exposed_names, spec_for

__all__ = ["TOOL_MANIFEST", "build_server", "exposed_names", "mcp_available", "spec_for"]


def mcp_available() -> bool:
    """官方 `mcp` 包在不在（不导入它——探测到就算数，导入失败留给装配点报原因）。"""
    return importlib.util.find_spec("mcp") is not None


def build_server(**kwargs):
    """惰性装配 MCP server；`mcp` 缺席时抛 ImportError，由装配点转成一行状态事实。"""
    if not mcp_available():
        raise ImportError("MCP 工具层需要可选依赖 `mcp`：`uv sync --extra dev --extra mcp`（extras 是整表替换，已有的要列全；镜像里已含）")
    from aegis.mcp_tools.server import build_server as _build

    return _build(**kwargs)
