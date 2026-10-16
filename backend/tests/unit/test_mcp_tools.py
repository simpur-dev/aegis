"""MCP 工具层（§4.2 / D4）的清单、注册与只读闸用例。

这批用例的存在理由不是"SDK 能跑"，而是四件本平台反复出事的事：
① 清单是单一真源（名称/参数/只读性/落在哪个端口一张表，分开必漂）；
② 缺位即不注册（不然空实现冒充"查过了没有"）；
③ 写工具两把钥匙（§4.8.2 工具默认只读）；
④ 参数表要真的被 SDK 读到（`__signature__` 注入这件事属于"看着对"，必须有一条 schema 断言兜着）。
全部用注入的假 source 驱动真 MCPServer，不碰网络。
"""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
from typing import Any

import pytest

from aegis.mcp_tools import TOOL_MANIFEST, exposed_names, mcp_available, spec_for
from aegis.mcp_tools.server import McpServerState, ToolSources, build_server

#: 需要真 SDK 才能跑的那几组：没装可选腿时如实跳过（与 pgvector/zenoh 那些用例同一口径），
#: 而"CI 到底装没装"由 `TestOptionalLegStaysOptional::test_ci与镜像都装了这一条可选腿` 兜——
#: 拿本机环境当断言会让没装 extra 的人看到红，拿部署面当断言才能让漏装真的变红。
needs_mcp = pytest.mark.skipif(not mcp_available(), reason="未安装可选依赖 mcp（`uv sync --extra dev --extra mcp`）")


async def _ok(args: dict[str, Any]) -> dict[str, Any]:
    return {"echo": args}


async def _boom(args: dict[str, Any]) -> dict[str, Any]:
    raise RuntimeError("检索腿断了")


def _sources(**overrides: Any) -> ToolSources:
    """默认给每个工具位一个能用的假实现，单个用例再按需替换/置空。"""
    base = dict.fromkeys(ToolSources.__dataclass_fields__, _ok)
    base.update(overrides)
    return ToolSources(**base)  # type: ignore[arg-type]


def _build(**kwargs: Any):
    return build_server(sources=kwargs.pop("sources", _sources()), **kwargs)


def _listed(server: Any) -> dict[str, Any]:
    return {tool.name: tool for tool in asyncio.run(server.list_tools())}


class TestManifestIsSingleSource:
    def test_每项都带参数表与端口字段名(self) -> None:
        """参数表与 ToolSources 字段必须对得上：对不上就是"列出来却调不动"或"注册了一个不收参数的工具"。"""
        fields = set(ToolSources.__dataclass_fields__)
        for spec in TOOL_MANIFEST:
            assert spec.params, f"{spec.name} 没有参数表"
            assert spec.source_attr in fields, f"{spec.name} 指向的端口字段不存在：{spec.source_attr}"
            assert set(spec.defaults) <= set(spec.params), f"{spec.name} 的默认值写到了不存在的参数上"
        assert len({spec.source_attr for spec in TOOL_MANIFEST}) == len(TOOL_MANIFEST), "两个工具共用一个端口=其中一个名不副实"

    def test_清单里没有名不副实的读面(self) -> None:
        """provider 只有一对 recall/learn，图谱与内置预案走同一条 recall——所以不给它编一个 graph.search。"""
        assert "graph.search" not in {spec.name for spec in TOOL_MANIFEST}

    def test_每项都指到真实存在的端点或端口(self) -> None:
        """source 必须写到"哪个方法/哪个端点"这一级；只写"检索服务"这种模块名，事后没人能核对它是否真同源。"""
        for spec in TOOL_MANIFEST:
            assert spec.source, f"{spec.name} 没写复用了哪条既有能力"
            assert "." in spec.source or "/api/" in spec.source, f"{spec.name} 的 source 不够具体：{spec.source}"

    def test_spec_for_与tool_names(self) -> None:
        assert spec_for("没有这个工具") is None
        assert spec_for("telemetry.query") is not None


class TestWriteToolsNeedTwoKeys:
    def test_默认面只含只读且默认暴露的项(self) -> None:
        assert set(exposed_names()) == {s.name for s in TOOL_MANIFEST if s.default_exposed and s.read_only}
        assert "simulation.run" not in exposed_names(), "推演单次是 K×T 量级，成本敞口不给默认面"

    def test_只开闸不够_只点名也不够(self) -> None:
        assert "memory.write" not in exposed_names()
        assert "memory.write" not in exposed_names(allow_write=True), "开闸不等于把写面送出去，还得点名"
        assert "memory.write" not in exposed_names(["memory.write"]), "点名不等于放行"
        assert "memory.write" in exposed_names(["memory.write"], allow_write=True)

    def test_点名清单外的工具要当场报错(self) -> None:
        """配了没生效＝空旋钮；这里选择装配期失败。"""
        with pytest.raises(ValueError, match="清单外"):
            exposed_names(["telemetry.query", "os.exec"])
        with pytest.raises(ValueError):
            exposed_names(["memory.write", "graph.drop"], allow_write=True)


@needs_mcp
class TestRegistration:
    def test_工具schema与清单同源(self) -> None:
        """这条是 `__signature__` 注入的验收面：SDK 读不到参数表时属性集会空或少键。"""
        server, state = _build(allowed_tools=[spec.name for spec in TOOL_MANIFEST], allow_write_tools=True)
        listed = _listed(server)
        assert state.absent == []
        assert set(listed) == {spec.name for spec in TOOL_MANIFEST}
        for spec in TOOL_MANIFEST:
            tool = listed[spec.name]
            assert set((tool.input_schema or {}).get("properties", {})) == set(spec.params), spec.name
            assert tool.description == spec.summary
            assert tool.annotations is not None
            assert tool.annotations.read_only_hint is spec.read_only
            assert tool.annotations.destructive_hint is (not spec.read_only)
            assert tool.annotations.open_world_hint is False

    def test_没注入实现的能力不注册而记为缺席(self) -> None:
        server, state = _build(sources=ToolSources(telemetry=_ok))  # type: ignore[arg-type]
        assert list(_listed(server)) == ["telemetry.query"]
        assert set(state.absent) == {"retrieval.search", "cases.search", "memory.search", "memory.categories"}
        assert "memory.write" not in state.absent, "没被点名的写工具压根不在期望集里，不该混进缺席读数"

    def test_点名要求暴露未注入实现的能力时报缺席(self) -> None:
        _server, state = _build(allowed_tools=["simulation.run"], sources=ToolSources())
        assert state.exposed == [] and state.absent == ["simulation.run"]

    def test_写工具注册需要两把钥匙(self) -> None:
        closed, closed_state = _build(sources=_sources(memory_write=_ok))
        assert "memory.write" not in _listed(closed) and "memory.write" not in closed_state.exposed
        opened, opened_state = _build(
            sources=_sources(memory_write=_ok),
            allowed_tools=[*exposed_names(), "memory.write"],
            allow_write_tools=True,
        )
        assert "memory.write" in _listed(opened) and "memory.write" in opened_state.exposed


@needs_mcp
class TestCalls:
    def test_成功调用带信封并计数(self) -> None:
        server, state = _build()
        result = asyncio.run(server.call_tool("telemetry.query", {"region_code": "540121"}))
        assert result.is_error is False
        assert '"ok": true' in result.content[0].text and "telemetry.query" in result.content[0].text
        assert state.calls == {"telemetry.query": 1} and state.failures == {}

    def test_缺省参数由schema补齐后送进端口(self) -> None:
        seen: dict[str, Any] = {}

        async def capture(args: dict[str, Any]) -> dict[str, Any]:
            seen.update(args)
            return {"count": 0}

        server, _state = _build(sources=ToolSources(cases=capture))  # type: ignore[arg-type]
        asyncio.run(server.call_tool("cases.search", {"query": "泥石流"}))
        assert seen["query"] == "泥石流" and seen["limit"] == 3 and seen["region_code"] == ""

    def test_参数类型不符在协议层就被拒_不进端口(self) -> None:
        """本层不再"收敛"客户端输入：静默改写会让它以为自己对 schema 的理解是对的。"""
        from mcp.server.mcpserver.exceptions import ToolError

        seen: dict[str, Any] = {}

        async def capture(args: dict[str, Any]) -> dict[str, Any]:
            seen.update(args)
            return {"ok": True}

        server, state = _build(sources=ToolSources(telemetry=capture))  # type: ignore[arg-type]
        with pytest.raises(ToolError, match="validation error"):
            asyncio.run(server.call_tool("telemetry.query", {"region_code": 540121}))
        assert seen == {} and state.calls == {}, "协议层就拒了的调用既没进端口，也不算一次工具调用"
        asyncio.run(server.call_tool("telemetry.query", {"region_code": "540121", "limit": "5"}))
        assert state.calls == {"telemetry.query": 1}, "宽松可转换的写法（数字字符串给 int）按 schema 正常进来"

    def test_腿断了发协议层工具错误并留下成因(self) -> None:
        from mcp.server.mcpserver.exceptions import ToolError

        server, state = _build(sources=_sources(retrieval=_boom))
        with pytest.raises(ToolError) as boom:
            asyncio.run(server.call_tool("retrieval.search", {"query": "泥石流"}))
        assert "retrieval.search" in str(boom.value) and "检索腿断了" in str(boom.value)
        assert state.failures == {"retrieval.search": 1}
        assert state.last_error.startswith("retrieval.search: RuntimeError")

    def test_未知工具由协议层拒绝且不计数(self) -> None:
        server, state = _build()
        with pytest.raises(Exception) as boom:
            asyncio.run(server.call_tool("does.not.exist", {}))
        assert "does.not.exist" in str(boom.value)
        assert state.calls == {}


@needs_mcp
class TestCallLatency:
    """§4.2 那句"工具即审计面（调用留 trace）"的账：每次调用要有时延样本，工具维度要读得到。

    两份账各有读者——账本里那条 `mcp_tool_ms` 是给 `/metrics`、报表与违约判定用的（聚合线），
    `state.latency` 里按工具分开的那份是给状态行与取证脚本用的（工具维度）。
    只留一份就会缺另一件事：只有聚合线就答不出"哪个工具慢"，只有进程内那份就没人能告警。
    """

    def test_一次成功调用两份账各留一条(self) -> None:
        from aegis.observability.tracer import Tracer

        tracer = Tracer()
        server, state = _build(tracer=tracer)
        asyncio.run(server.call_tool("telemetry.query", {"region_code": "540121"}))
        assert tracer.ledger.stats("mcp_tool_ms").count == 1, "调用没进账本：/metrics 与报表里这条腿是空的"
        summary = state.latency_summary()
        assert list(summary) == ["telemetry.query"], summary
        assert {"count", "p50", "p95", "max"} <= set(summary["telemetry.query"])
        assert summary["telemetry.query"]["count"] == 1

    def test_读数不是恒零(self) -> None:
        """时钟选错时这条读成 0（Windows 的 `monotonic` 步进 15.6 ms，见 `test_latency_clock_choice.py`）。"""

        async def sleepy(args: dict[str, Any]) -> dict[str, Any]:
            await asyncio.sleep(0.002)
            return {"count": 0}

        server, state = _build(sources=ToolSources(telemetry=sleepy), allowed_tools=["telemetry.query"])  # type: ignore[arg-type]
        asyncio.run(server.call_tool("telemetry.query", {"region_code": "540121"}))
        measured = state.latency["telemetry.query"][0]
        assert measured > 0.0, f"一次 2 ms 的取数量出 {measured} ms：这个计时点等于没在测"

    def test_失败的那次也算一次调用并留样本(self) -> None:
        """3 s 后失败的取数若不在时延里出现，p95 就只描述"顺利的那些次"——那正是最该看见的一类。"""
        from aegis.observability.tracer import Tracer

        tracer = Tracer()
        server, state = _build(sources=_sources(retrieval=_boom), tracer=tracer)
        with pytest.raises(Exception, match="检索腿断了"):
            asyncio.run(server.call_tool("retrieval.search", {"query": "泥石流"}))
        assert tracer.ledger.stats("mcp_tool_ms", outcome="error").count == 1
        assert tracer.ledger.stats("mcp_tool_ms", outcome="ok").count == 0
        assert len(state.latency["retrieval.search"]) == 1
        assert state.failures == {"retrieval.search": 1}

    def test_协议层就拒掉的调用不留下任何一份账(self) -> None:
        """类型不符的实参根本没进端口：把它算成一次工具调用会虚增时延样本与调用数。"""
        from mcp.server.mcpserver.exceptions import ToolError  # 协议层的错从这里来（见 `test_参数类型不符…`）

        from aegis.observability.tracer import Tracer

        tracer = Tracer()
        server, state = _build(sources=ToolSources(telemetry=_ok), tracer=tracer)  # type: ignore[arg-type]
        with pytest.raises(ToolError, match="validation error"):
            asyncio.run(server.call_tool("telemetry.query", {"region_code": 540121}))
        assert tracer.ledger.stats("mcp_tool_ms").count == 0
        assert state.latency == {} and state.calls == {}

    def test_没给tracer时进程内那份账照样成立(self) -> None:
        """记时延不是挂载工具面的前置条件；缺 tracer 只该让账本少一条线，不该让调用炸掉。"""
        server, state = _build()
        asyncio.run(server.call_tool("telemetry.query", {"region_code": "540121"}))
        assert len(state.latency["telemetry.query"]) == 1

    def test_每工具样本有上界不随调用数线性涨(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import aegis.mcp_tools.server as mcp_server

        monkeypatch.setattr(mcp_server, "LATENCY_KEEP", 3)
        server, state = _build()
        for _ in range(5):
            asyncio.run(server.call_tool("telemetry.query", {"region_code": "540121"}))
        assert len(state.latency["telemetry.query"]) == 3, "长命 server 的进程内样本要留上界，不然内存随调用数涨"
        assert state.calls == {"telemetry.query": 5}, "上界只管样本，不能把调用计数也截掉"


class TestSimulationSourceNeedsNoSdk:
    """沙盒实现这一头不碰 SDK：没装可选腿时它照样该能跑、形状照样要说清"是谁选的"。"""

    def test_工具面的推演交出角色档且不因开关冒充LLM档(self) -> None:
        """§4.4 从工具面出去的那一份也要说清"是谁选的"。

        网关没进 `build_mcp`，所以 `simulation_roles_llm=true` 在工具面拧不动——报告里
        `roles.policy=rule` 就是这件事的事实。把它钉住：§4.2 真接上网关时这一格会变，
        那时该改的是这条断言，而不是让"配了没生效"继续没人发现。
        """
        from aegis.config import Settings
        from aegis.integrations import _mcp_simulation_source

        call = _mcp_simulation_source(Settings(env="test", simulation_roles_llm=True))
        out = asyncio.run(call({"rounds": 2, "scenarios": 2}))
        assert out["mode"] == "demo_sandbox"
        assert out["roles"]["policy"] == "rule" and out["roles"]["asked"] == 8
        assert all(set(row["actions"]) == {"commander", "rescue", "residents", "monitor"} for row in out["timeline"])

    def test_工具面的情景条数被夹住(self) -> None:
        """外部客户端给 `scenarios=1e9`：这条腿是公开面，循环上限必须在装配点就成立。"""
        from aegis.config import Settings
        from aegis.integrations import _mcp_simulation_source

        out = asyncio.run(_mcp_simulation_source(Settings(env="test"))({"scenarios": 10**9, "rounds": 10**9}))
        assert out["cost"]["requested_scenarios"] <= 24
        assert len(out["timeline"]) <= 24


class TestOptionalLegStaysOptional:
    def test_门面在没装可选腿时也能import且available为False(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """可选腿的"没装"必须是一行事实：`mcp_available()` 走 find_spec 探测，不真 import mcp。"""
        import aegis.mcp_tools as facade

        real = importlib.util.find_spec

        def fake(name: str, *a: Any, **k: Any) -> Any:
            return None if name == "mcp" else real(name, *a, **k)

        monkeypatch.setattr(importlib.util, "find_spec", fake)
        assert facade.mcp_available() is False
        with pytest.raises(ImportError, match="mcp"):
            facade.build_server(sources=_sources())

    def test_ci与镜像都装了这一条可选腿(self) -> None:
        """装没装这一腿不该由本机环境说了算：查部署面，漏装才会当场变红。

        原本这里断的是"本机 import 得到 mcp"，那在没装 extra 的开发机上是一句必然的红，
        在装了 extra 的机器上又永远为真——两边都不管住真正会出事的那一刻：CI 或镜像的
        extras 列表里 `mcp` 被摘掉，于是工具面在生产里静默 `absent`，而测试仍是绿的
        （`deploy/Dockerfile` 上方那段注释记的就是这类事故）。
        """
        from pathlib import Path

        repo = Path(__file__).resolve().parents[3]
        ci = (repo / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        dockerfile = (repo / "deploy" / "Dockerfile").read_text(encoding="utf-8")
        assert "--extra mcp" in ci, "CI 不装 mcp 时，MCP 用例只是跳过，绿色 CI 测不到工具面"
        assert "--extra mcp" in dockerfile, "镜像少这个 extra ⇒ 容器里工具面永远 driver=absent"

    def test_状态字典形状稳定(self) -> None:
        state = McpServerState(exposed=["a"], absent=["b"], calls={"a": 1}, failures={}, last_error="")
        assert set(state.as_dict()) == {"exposed_tools", "absent_tools", "calls", "failures", "last_error", "latency_ms"}

    def test_装配入口只收关键字(self) -> None:
        """位置传参会让 container 里的实参错位（sources 与 allowed_tools 换位的失败最难查）。"""
        params = inspect.signature(build_server).parameters
        assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in params.values())


@needs_mcp
class TestConfigFace:
    """配置面这一层只测一件事：`.env` 里那行字能不能变成装配用的选择集。

    走 env → Settings → build_mcp 的整链路，而不是直接构造 `Settings(mcp_tools=...)`——
    键形选错的表现是"部署当天启动失败"，只有从 env 进才测得到（本机实测：`list[str]` 字段
    会被 pydantic-settings 先做 JSON 解码，`AEGIS_MCP_TOOLS=a,b` 当场 SettingsError）。
    """

    def _settings(self, **overrides: Any):
        from aegis.config import Settings

        return Settings(_env_file=None, mcp_enabled=True, **overrides)

    def test_部署里的逗号写法能读成选择集(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from aegis.integrations import build_mcp

        monkeypatch.setenv("AEGIS_MCP_TOOLS", "telemetry.query, cases.search")
        assembly = build_mcp(settings=self._settings())
        assert assembly.server is not None
        # 没注入任何端口：点名的两项都要出现在缺席名单里，顺序按清单，且不允许多/少一项
        assert assembly.state.detail["absent_tools"] == ["telemetry.query", "cases.search"]

    def test_不点名就是清单默认面(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from aegis.integrations import build_mcp

        monkeypatch.delenv("AEGIS_MCP_TOOLS", raising=False)
        settings = self._settings()
        assert settings.mcp_tools == ""
        assembly = build_mcp(settings=settings)
        assert set(assembly.state.detail["absent_tools"]) == set(exposed_names())
        assert "memory.write" not in assembly.state.detail["absent_tools"], "没被点名的写工具不该出现在任何名单里"
