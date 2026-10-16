"""MCP server 装配（§4.2）：把平台既有能力按清单暴露成工具，**这一层不写业务**。

五条硬约束，都是本仓已经被咬过的形态：
1. 名称/摘要/只读性/参数表/落在哪个端口全部来自 `manifest.TOOL_MANIFEST`——清单与实现分两处必漂。
2. 清单外的名字注册不进去（`exposed_names` 当场 ValueError）；清单里有但没注入实现的工具【不注册】
   并如实出现在 `absent` 里——"能列出来却调不动"和"默默少一个"都比谎报好。
3. 只读性写进 `ToolAnnotations(read_only_hint=…)`；写工具两把钥匙（点名 + `allow_write_tools`）。
4. 每次调用计数并留成因；腿断了发协议层 `ToolError`（不是塞一个 ok=false 的正文让客户端猜）。
5. §4.2 那句"工具即审计面（调用留 trace）"要有实现：每次调用量一段墙钟毫秒，
   按工具名留在 `state.latency`（经状态行出口），并把一条 `mcp_tool_ms` 样本送进时延账本
   （失败也算一次调用，`outcome=error`）。账本是 `/metrics` 与报表的真源，缺席的 tracer
   只让进程内那份计数继续成立——不为此造一个假账本。

参数校验交给 SDK：它按 inputSchema 校验实参，类型不符在协议层就被拒。本层不再"收敛"一次输入——
静默改写客户端参数会让它以为自己对 schema 的理解是对的，而这正是最难查的那类不匹配。
"""

from __future__ import annotations

import inspect
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from aegis.mcp_tools.manifest import TOOL_MANIFEST, ToolSpec, exposed_names, spec_for
from aegis.observability.tracer import Tracer, percentile

Source = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]

#: 每个工具在进程内留多少条时延样本：够算 p95，又不让一个长命 server 的内存随调用数线性涨。
LATENCY_KEEP = 200

#: 进时延账本的指标名。**不带工具维度**：工具名会乘出七个序列，而考核口径里没有"每个工具一条线"，
#: 指标目录（报表档位、前端标签表、SLA 预算）都是按固定名单登记的。工具维度留在 `state.latency`
#: 与状态行里，聚合线留在账本里——两件事各有各的读者。
LEDGER_METRIC = "mcp_tool_ms"


@dataclass(slots=True)
class ToolSources:
    """各工具背后的既有能力。缺位 = 那个工具不对外暴露（而不是返回空表冒充"查过了没有"）。"""

    telemetry: Source | None = None
    retrieval: Source | None = None
    cases: Source | None = None
    memory_search: Source | None = None
    memory_categories: Source | None = None
    memory_write: Source | None = None
    simulation: Source | None = None

    def for_tool(self, spec: ToolSpec) -> Source | None:
        return getattr(self, spec.source_attr, None)


@dataclass(slots=True)
class McpServerState:
    """这一趟装配出了什么：暴露哪些工具、哪些因未注入实现而缺席、调用/失败计数与每次调用的墙钟毫秒。"""

    exposed: list[str] = field(default_factory=list)
    absent: list[str] = field(default_factory=list)
    calls: dict[str, int] = field(default_factory=dict)
    failures: dict[str, int] = field(default_factory=dict)
    last_error: str = ""
    #: 工具名 → 最近 `LATENCY_KEEP` 次调用的毫秒数（成功与失败都在里面，失败另记 failures）。
    latency: dict[str, list[float]] = field(default_factory=dict)

    def latency_summary(self) -> dict[str, dict[str, float | int]]:
        """按工具给出可读的分位数：状态行与取证脚本读的是这一份，不是原始样本堆。"""
        summary: dict[str, dict[str, float | int]] = {}
        for name, samples in self.latency.items():
            ordered = sorted(samples)
            summary[name] = {
                "count": len(ordered),
                "p50": round(percentile(ordered, 50), 3),
                "p95": round(percentile(ordered, 95), 3),
                "max": round(ordered[-1], 3),
            }
        return summary

    def as_dict(self) -> dict[str, Any]:
        return {
            "exposed_tools": list(self.exposed),
            "absent_tools": list(self.absent),
            "calls": dict(self.calls),
            "failures": dict(self.failures),
            "last_error": self.last_error,
            "latency_ms": self.latency_summary(),
        }


def build_server(
    *,
    sources: ToolSources,
    allowed_tools: list[str] | None = None,
    allow_write_tools: bool = False,
    server_name: str = "aegis",
    instructions: str = "",
    tracer: Tracer | None = None,
) -> tuple[Any, McpServerState]:
    """装配 MCPServer。返回 (server, state)；`mcp` 包缺席时抛 ImportError（装配点转成状态行）。

    `tracer` 是平台时延账本的入口（装配点给）：给它就把每次调用记成一条 `mcp_tool_ms` 样本，
    不给就只留进程内那份按工具的样本——两种形态下 `state.latency` 都成立，取证读的是同一处。
    """
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
        _register(server, state, spec, source, tracer)
        state.exposed.append(spec.name)
    return server, state


def _register(server: Any, state: McpServerState, spec: ToolSpec, source: Source, tracer: Tracer | None) -> None:
    """把 source 包成签名明确的工具并注册。

    参数表来自清单，经 `__signature__`/`__annotations__` 挂到包装函数上：SDK 靠签名生成 inputSchema，
    裸 `**kwargs` 会让客户端看到一个"什么参数都不收"的工具。这么做的代价是必须有用例证明 SDK
    真读到了这份 schema（`test_工具schema与清单同源`），否则这里只是"看着对"。
    """

    async def tool(**kwargs: Any) -> dict[str, Any]:
        return await _invoke(state, spec.name, source, {key: kwargs.get(key) for key in spec.params}, tracer)

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


async def _invoke(
    state: McpServerState,
    name: str,
    source: Source,
    payload: dict[str, Any],
    tracer: Tracer | None = None,
) -> dict[str, Any]:
    """一次工具调用：计数与计时、把成因用协议规定的工具错误发出去、成功返回值带 `tool`/`ok` 信封。

    失败走 `ToolError`：SDK 把它映射成 `isError=true`（本机 2.3.0 源码里 `MCPServer.call_tool`
    的文档明写 tool 抛 ToolError 即为工具错误）。只把 ok=false 塞进正文的话，外部客户端会把它
    当成功结果解析——协议层说得清的事，不该留给每个客户端自己猜。
    """
    from mcp.server.mcpserver.exceptions import ToolError

    state.calls[name] = state.calls.get(name, 0) + 1
    started = time.perf_counter()
    outcome = "ok"
    try:
        result = await source(payload)
    except Exception as exc:
        outcome = "error"
        state.failures[name] = state.failures.get(name, 0) + 1
        state.last_error = f"{name}: {type(exc).__name__}: {str(exc)[:180]}"
        raise ToolError(f"{name} 取数失败：{type(exc).__name__}: {str(exc)[:180]}") from exc
    finally:
        _account(state, name, started, outcome, tracer)
    if not isinstance(result, dict):
        result = {"items": result}
    return {"tool": name, "ok": True, **result}


def _account(state: McpServerState, name: str, started: float, outcome: str, tracer: Tracer | None) -> None:
    """两份账各记一次：按工具留进程内样本（状态行的读者），按 `mcp_tool_ms` 留账本样本（/metrics 与报表的读者）。

    失败也要有样本：一次 3 s 后失败的取数若不在时延里出现，p95 就只描述"顺利的那些次"，
    而这恰恰是最需要被看见的那一类。故障次数另有 `failures`，两者不互相掩盖。
    """
    elapsed_ms = max((time.perf_counter() - started) * 1000.0, 0.0)
    samples = state.latency.setdefault(name, [])
    samples.append(elapsed_ms)
    if len(samples) > LATENCY_KEEP:
        del samples[: len(samples) - LATENCY_KEEP]
    if tracer is not None:
        tracer.record(LEDGER_METRIC, elapsed_ms, outcome=outcome)


def _annotations(spec: ToolSpec):
    from mcp.types import ToolAnnotations

    return ToolAnnotations(
        title=spec.name,
        read_only_hint=spec.read_only,
        destructive_hint=not spec.read_only,
        open_world_hint=False,  # 工具面是封闭清单：外面没有"隐藏能力"
    )


__all__ = ["McpServerState", "ToolSources", "build_server", "spec_for"]
