"""MCP server 装配（§4.2）：把平台既有能力按清单暴露成工具，**这一层不写业务**。

四条硬约束，都是本仓已经被咬过的形态：
1. 名称/摘要/只读性/参数表/落在哪个端口全部来自 `manifest.TOOL_MANIFEST`——清单与实现分两处必漂。
2. 清单外的名字注册不进去（`exposed_names` 当场 ValueError）；清单里有但没注入实现的工具【不注册】
   并如实出现在 `absent` 里——"能列出来却调不动"和"默默少一个"都比谎报好。
3. 只读性写进 `ToolAnnotations(read_only_hint=…)`；写工具两把钥匙（点名 + `allow_write_tools`）。
4. 每次调用计数并留成因；腿断了发协议层 `ToolError`（不是塞一个 ok=false 的正文让客户端猜）。

参数校验交给 SDK：它按 inputSchema 校验实参，类型不符在协议层就被拒。本层不再"收敛"一次输入——
静默改写客户端参数会让它以为自己对 schema 的理解是对的，而这正是最难查的那类不匹配。
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from aegis.mcp_tools.manifest import TOOL_MANIFEST, ToolSpec, exposed_names, spec_for

Source = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass(slots=True)
class ToolSources:
    """各工具背后的既有能力。缺位 = 那个工具不对外暴露（而不是返回空表冒充"查过了没有"）。"""

    telemetry: Source | None = None
    retrieval: Source | None = None
    cases: Source | None = None
    memory_search: Source | None = None
    memory_write: Source | None = None
    simulation: Source | None = None

    def for_tool(self, spec: ToolSpec) -> Source | None:
        return getattr(self, spec.source_attr, None)


@dataclass(slots=True)
class McpServerState:
    """这一趟装配出了什么：暴露哪些工具、哪些因未注入实现而缺席、调用与失败计数。"""

    exposed: list[str] = field(default_factory=list)
    absent: list[str] = field(default_factory=list)
    calls: dict[str, int] = field(default_factory=dict)
    failures: dict[str, int] = field(default_factory=dict)
    last_error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "exposed_tools": list(self.exposed),
            "absent_tools": list(self.absent),
            "calls": dict(self.calls),
            "failures": dict(self.failures),
            "last_error": self.last_error,
        }


def build_server(
    *,
    sources: ToolSources,
    allowed_tools: list[str] | None = None,
    allow_write_tools: bool = False,
    server_name: str = "aegis",
    instructions: str = "",
) -> tuple[Any, McpServerState]:
    """装配 MCPServer。返回 (server, state)；`mcp` 包缺席时抛 ImportError（装配点转成状态行）。"""
    from mcp.server.mcpserver import MCPServer

    wanted = exposed_names(allowed_tools, allow_write=allow_write_tools)
    state = McpServerState()
    server = MCPServer(
        server_name,
        instructions=instructions or "AEGIS 山地灾害平台工具面：以只读取数为主，写操作需服务端显式放行。",
    )
    for spec in TOOL_MANIFEST:
        if spec.name not in wanted:
            continue
        source = sources.for_tool(spec)
        if source is None:
            state.absent.append(spec.name)  # 没注入实现：不注册，而不是注册一个返回空的假工具
            continue
        _register(server, state, spec, source)
        state.exposed.append(spec.name)
    return server, state


def _register(server: Any, state: McpServerState, spec: ToolSpec, source: Source) -> None:
    """把 source 包成签名明确的工具并注册。

    参数表来自清单，经 `__signature__`/`__annotations__` 挂到包装函数上：SDK 靠签名生成 inputSchema，
    裸 `**kwargs` 会让客户端看到一个"什么参数都不收"的工具。这么做的代价是必须有用例证明 SDK
    真读到了这份 schema（`test_工具schema与清单同源`），否则这里只是"看着对"。
    """

    async def tool(**kwargs: Any) -> dict[str, Any]:
        return await _invoke(state, spec.name, source, {key: kwargs.get(key) for key in spec.params})

    tool.__name__ = spec.name.replace(".", "_")
    tool.__qualname__ = spec.name.replace(".", "_")
    tool.__doc__ = spec.summary
    tool.__annotations__ = {**spec.params, "return": dict[str, Any]}
    # `__signature__` 是可赋值的方法属性，但 mypy 只认函数对象的读侧（本机 2.13 报 attr-defined）。
    # 这里压掉而不是改结构，是因为这份签名就是 inputSchema 的唯一真源，换写法等于换 SDK 的读取路径。
    tool.__signature__ = inspect.Signature(  # type: ignore[attr-defined]
        parameters=[
            inspect.Parameter(
                key,
                inspect.Parameter.KEYWORD_ONLY,
                default=_default(spec, key, type_),
                annotation=type_,
            )
            for key, type_ in spec.params.items()
        ]
    )
    server.tool(name=spec.name, description=spec.summary, annotations=_annotations(spec))(tool)


def _default(spec: ToolSpec, key: str, type_: type) -> Any:
    if key in spec.defaults:
        return spec.defaults[key]
    if type_ is bool:
        return False
    if type_ in (int, float):
        return 0 if type_ is int else 0.0
    return ""


async def _invoke(state: McpServerState, name: str, source: Source, payload: dict[str, Any]) -> dict[str, Any]:
    """一次工具调用：计数、把成因用协议规定的工具错误发出去、成功返回值带 `tool`/`ok` 信封。

    失败走 `ToolError`：SDK 把它映射成 `isError=true`（本机 2.3.0 源码里 `MCPServer.call_tool`
    的文档明写 tool 抛 ToolError 即为工具错误）。只把 ok=false 塞进正文的话，外部客户端会把它
    当成功结果解析——协议层说得清的事，不该留给每个客户端自己猜。
    """
    from mcp.server.mcpserver.exceptions import ToolError

    state.calls[name] = state.calls.get(name, 0) + 1
    try:
        result = await source(payload)
    except Exception as exc:
        state.failures[name] = state.failures.get(name, 0) + 1
        state.last_error = f"{name}: {type(exc).__name__}: {str(exc)[:180]}"
        raise ToolError(f"{name} 取数失败：{type(exc).__name__}: {str(exc)[:180]}") from exc
    if not isinstance(result, dict):
        result = {"items": result}
    return {"tool": name, "ok": True, **result}


def _annotations(spec: ToolSpec):
    from mcp.types import ToolAnnotations

    return ToolAnnotations(
        title=spec.name,
        read_only_hint=spec.read_only,
        destructive_hint=not spec.read_only,
        open_world_hint=False,  # 工具面是封闭清单：外面没有"隐藏能力"
    )


__all__ = ["McpServerState", "ToolSources", "build_server", "spec_for"]
