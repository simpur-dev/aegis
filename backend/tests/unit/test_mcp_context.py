"""MCP 取数门面（§4.2 阶段 2）的用例：内环经标准工具面取佐证，且与 REST 门面同一口径。

这批用例守的是四件事，都不是"SDK 能不能跑"：
① 门面选择是显式的：`auto` 跟着部署形态走、`rest` 是回退位、`mcp` 在没有工具面时**当场失败**
   ——静默退回 REST 就是"配了没生效"（本仓最贵的那类旋钮）；
② 两条门面产出的上下文【形状】必须一致：哪些键在、空命中算不算降级、一条腿坏了不拖累另一条。
   口径分叉的代价不在这里，在下游——预案提示词与 `context_degraded` 都会跟着变味；
③ 工具报错/缺席/连不上都落在 `ctx["degraded"]` 里且带原话，不塞空列表冒充"查过了没有"；
④ 真握手那一趟走真 uvicorn + 真工具面（ASGITransport 载不动 SDK 客户端，见 REPORT MB-5）。
"""

from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
import uvicorn

from aegis.agents.assess import AssessContextProvider
from aegis.agents.mcp_context import McpAssessContext
from aegis.agents.rest_context import RestAssessContext
from aegis.agents.runtime import resolve_context_face
from aegis.api.app import create_app
from aegis.config import Settings

REGION = "540121"
CASE_ITEM = {"title": "2024 河口泥石流", "degraded": False}
DOC_ITEM = {"id": "doc-1", "title": "应急预案第 3 节"}


def _envelope(tool: str, items: list[Any]) -> str:
    """工具正文的信封形状，与 `integrations._mcp_*_source` 的返回一致（items 是上下文唯一要的键）。"""
    return json.dumps({"tool": tool, "ok": True, "count": len(items), "items": items}, ensure_ascii=False)


class Caller:
    """假传输：记下每次真被点到的工具与实参，按表回 (is_error, 正文)。"""

    def __init__(self, table: dict[str, tuple[bool, str]], *, boom: Exception | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._table = table
        self._boom = boom

    async def __call__(self, tool: str, arguments: dict[str, Any]) -> tuple[bool, str]:
        self.calls.append((tool, arguments))
        if self._boom is not None:
            raise self._boom
        return self._table[tool]

    def args_of(self, tool: str) -> dict[str, Any]:
        return next(args for name, args in self.calls if name == tool)


def subject(table: dict[str, tuple[bool, str]], *, boom: Exception | None = None, **kw: Any) -> tuple[McpAssessContext, Caller]:
    caller = Caller(table, boom=boom)
    return McpAssessContext(url="http://platform/mcp/", tool_caller=caller, **kw), caller


BOTH_HIT = {
    "cases.search": (False, _envelope("cases.search", [CASE_ITEM])),
    "retrieval.search": (False, _envelope("retrieval.search", [DOC_ITEM])),
}
BOTH_EMPTY = {
    "cases.search": (False, _envelope("cases.search", [])),
    "retrieval.search": (False, _envelope("retrieval.search", [])),
}


class TestFaceChoice:
    def test_auto_跟着工具面走(self) -> None:
        assert resolve_context_face("auto", mcp_url="http://127.0.0.1:8000/mcp/") == "mcp"
        assert resolve_context_face("auto", mcp_url="") == "rest"

    def test_rest_是留着的回退位(self) -> None:
        """显式 rest 时，工具面开着也不切——灰度回退与 A/B 对照都靠这条路。"""
        assert resolve_context_face("rest", mcp_url="http://127.0.0.1:8000/mcp/") == "rest"

    def test_点名mcp而工具面不在时当场失败(self) -> None:
        with pytest.raises(ValueError, match="AEGIS_AGENT_CONTEXT_FACE=mcp"):
            resolve_context_face("mcp", mcp_url="")

    async def test_门面按选择装配_两条路各走各的(self) -> None:
        """`build_agent_runtime` 里这条选择是唯一的分叉点，装配错了没人会再发现。"""
        from aegis.agents.runtime import build_agent_runtime
        from aegis.services.trigger_rules import RuleEngine

        common: dict[str, Any] = {
            "rule_engine": RuleEngine(),
            "base_url": "http://127.0.0.1:8000",
            "llm": None,
            "parser": None,
            "contracts": None,
            "memory": None,
        }
        rest_agents = build_agent_runtime(_NullTransport(), **common)  # type: ignore[arg-type]
        mcp_agents = build_agent_runtime(_NullTransport(), mcp_url="http://127.0.0.1:8000/mcp/", **common)  # type: ignore[arg-type]
        assert isinstance(next(a for a in rest_agents if a.agent_type.value == "assess").context, RestAssessContext)
        assert isinstance(next(a for a in mcp_agents if a.agent_type.value == "assess").context, McpAssessContext)


class _NullTransport:
    """`build_agent_runtime` 只做构造，不碰传输；这里给个占位（构造路径真跑过，见上面那条用例）。"""

    name = "null"

    async def connect(self) -> None:
        pass

    async def close(self) -> None:
        pass


class TestCorroborate:
    async def test_两条腿命中时按真实items交出(self) -> None:
        provider, caller = subject(BOTH_HIT)
        ctx = await provider.corroborate(region_code=REGION, hazard_hint="debris_flow", hits=[])
        assert ctx["cases"] == [CASE_ITEM] and ctx["retrieval"] == [DOC_ITEM]
        assert ctx["degraded"] == []
        assert [name for name, _ in caller.calls] == ["cases.search", "retrieval.search"]

    async def test_实参按工具schema给(self) -> None:
        """参数名/必给性都对着 inputSchema；令牌那档由 `TestRealHandshake` 用真会话覆盖。"""
        provider, caller = subject(BOTH_HIT, case_limit=4, search_k=7)
        await provider.corroborate(region_code=REGION, hazard_hint="debris_flow", hits=[])
        assert caller.args_of("cases.search") == {"query": "debris_flow", "region_code": REGION, "hazard_type": "debris_flow", "limit": 4}
        assert caller.args_of("retrieval.search") == {"query": "debris_flow", "k": 7, "rerank": True}, (
            "检索工具没有区划/灾种参数，硬塞会被协议层拒"
        )

    async def test_灾种从hits里推(self) -> None:
        provider, caller = subject(BOTH_HIT)
        await provider.corroborate(region_code=REGION, hazard_hint=None, hits=[{"hazard_type": "landslide", "rule_id": "R-1"}])
        assert caller.args_of("cases.search")["hazard_type"] == "landslide"

    async def test_非枚举灾种不发出去(self) -> None:
        """与 REST 同一句护栏：枚举外的值交给工具会被当普通字符串查，命中与否都不知所云。"""
        provider, caller = subject(BOTH_HIT)
        await provider.corroborate(region_code=REGION, hazard_hint="泥石流", hits=[])
        assert caller.args_of("cases.search")["hazard_type"] == "" and caller.args_of("cases.search")["query"] == "泥石流"

    async def test_工具报错时把原话放进降级且不塞空items(self) -> None:
        table = {**BOTH_HIT, "cases.search": (True, "cases.search 取数失败：RuntimeError: 图谱连不上")}
        provider, _ = subject(table)
        ctx = await provider.corroborate(region_code=REGION, hazard_hint="debris_flow", hits=[])
        assert "cases" not in ctx
        assert ctx["degraded"] == ["cases: cases.search 取数失败：RuntimeError: 图谱连不上"]
        assert ctx["retrieval"] == [DOC_ITEM], "一条腿坏了不拖累另一条腿"

    async def test_腿缺席时协议层的原话照搬(self) -> None:
        """检索没装配 → 工具根本没注册 → SDK 回 `Unknown tool`，这句话本身就是降级原因。"""
        table = {**BOTH_HIT, "retrieval.search": (True, "Unknown tool: retrieval.search")}
        provider, _ = subject(table)
        ctx = await provider.corroborate(region_code=REGION, hazard_hint="debris_flow", hits=[])
        assert ctx["cases"] == [CASE_ITEM] and "retrieval" not in ctx
        assert ctx["degraded"] == ["retrieval: Unknown tool: retrieval.search"]

    async def test_连不上工具面时两条腿各自记同一条成因(self) -> None:
        provider, _ = subject(BOTH_HIT, boom=OSError("connection refused"))
        ctx = await provider.corroborate(region_code=REGION, hazard_hint="debris_flow", hits=[])
        assert "cases" not in ctx and "retrieval" not in ctx
        assert len(ctx["degraded"]) == 2 and all("工具面不可达" in line for line in ctx["degraded"])

    async def test_空命中不算降级(self) -> None:
        provider, _ = subject(BOTH_EMPTY)
        ctx = await provider.corroborate(region_code=REGION, hazard_hint="debris_flow", hits=[])
        assert "cases" not in ctx and "retrieval" not in ctx and ctx["degraded"] == []

    async def test_没有关键词时检索腿不发也不记故障(self) -> None:
        """两头都要不沾：既不发一次注定被拒的取数，也不把"没词"写成故障（真链路里 hits 为空的事件本来就没词）。

        这条与 `test_rest_context.py::test_没有关键词时检索腿不发` 是一对——口径分叉就从这种
        "什么算降级"的小分歧开始长。
        """
        provider, caller = subject(BOTH_EMPTY)
        ctx = await provider.corroborate(region_code=REGION, hazard_hint=None, hits=[])
        assert [name for name, _ in caller.calls] == ["cases.search"]
        assert ctx["degraded"] == [] and "retrieval" not in ctx

    async def test_正文不是JSON时按没命中处理(self) -> None:
        table = {**BOTH_HIT, "cases.search": (False, "not json at all")}
        provider, _ = subject(table)
        ctx = await provider.corroborate(region_code=REGION, hazard_hint="debris_flow", hits=[])
        assert "cases" not in ctx and ctx["degraded"] == []

    async def test_正文是JSON但不是对象时也不当命中(self) -> None:
        """`items` 只认"对象里那个数组"。工具回了个顶层数组/字符串时（合法 JSON 但形状不对），
        把它整个塞进上下文等于把一条协议外的东西送进提示词。
        """
        for weird in ('[{"title": "假命中"}]', '"一段字符串"', "null", "123"):
            table = {**BOTH_HIT, "cases.search": (False, weird)}
            provider, _ = subject(table)
            ctx = await provider.corroborate(region_code=REGION, hazard_hint="debris_flow", hits=[])
            assert "cases" not in ctx, f"合法 JSON 但形状不对的正文被当成命中：{weird} -> {ctx}"
            assert ctx["degraded"] == [], "形状不对不是故障：它只是『这一腿没给出可用的 items』"

    async def test_它是AssessContextProvider要的那个形状(self) -> None:
        provider, _ = subject(BOTH_EMPTY)
        assert isinstance(provider, AssessContextProvider), "装配点按协议注入，形状不认就是取数断链"


class TestTwoFacesOneStandard:
    """同一个 provider、同一份命中，两条门面对上下文的【读法】必须一致。

    只比形状不比文案：REST 说"知识层未装配"、MCP 说"Unknown tool: cases.search"，
    两句都是真话，硬要它们同字反而会逼其中一边改写原因。要钉的是"什么算降级、什么算空"。
    """

    def _rest(self, table: dict[str, tuple[int, Any]]) -> RestAssessContext:
        def handler(request: httpx.Request) -> httpx.Response:
            status, body = table[request.url.path]
            return httpx.Response(status, json=body)

        transport = httpx.MockTransport(handler)

        def factory(**kw: Any) -> httpx.AsyncClient:
            kw.pop("transport", None)  # 被测对象传进来的真 transport 在这里被测试替身顶掉
            return httpx.AsyncClient(transport=transport, **kw)

        return RestAssessContext(base_url="http://platform", client_factory=factory)

    @pytest.mark.parametrize(
        ("rest_table", "mcp_table", "expect_keys", "expect_degraded"),
        [
            (
                {"/api/v1/cases/recall": (200, {"items": [CASE_ITEM]}), "/api/v1/retrieval/search": (200, {"items": [DOC_ITEM]})},
                BOTH_HIT,
                {"cases", "retrieval"},
                [],
            ),
            (
                {"/api/v1/cases/recall": (200, {"items": []}), "/api/v1/retrieval/search": (200, {"items": []})},
                BOTH_EMPTY,
                set(),
                [],
            ),
            (
                {"/api/v1/cases/recall": (503, {"detail": "知识层未装配"}), "/api/v1/retrieval/search": (200, {"items": [DOC_ITEM]})},
                {**BOTH_HIT, "cases.search": (True, "cases.search 取数失败：RuntimeError: 知识层未装配")},
                {"retrieval"},
                ["cases"],
            ),
        ],
    )
    async def test_同一个结论两条门面向一致(
        self, rest_table: dict, mcp_table: dict, expect_keys: set[str], expect_degraded: list[str]
    ) -> None:
        rest_ctx = await self._rest(rest_table).corroborate(region_code=REGION, hazard_hint="debris_flow", hits=[])
        mcp_provider, _ = subject(mcp_table)
        mcp_ctx = await mcp_provider.corroborate(region_code=REGION, hazard_hint="debris_flow", hits=[])
        for label in ("cases", "retrieval"):
            assert (label in rest_ctx) is (label in mcp_ctx), f"{label} 这条腿一边给了一边没给：两套口径已经开始分叉"
        assert {k for k in rest_ctx if k != "degraded"} == expect_keys
        rest_labels = [line.split(":")[0] for line in rest_ctx["degraded"]]
        mcp_labels = [line.split(":")[0] for line in mcp_ctx["degraded"]]
        assert rest_labels == mcp_labels == expect_degraded, "哪条腿算降级必须两门面同判——分叉会一路长到提示词与 context_degraded 里"


class TestRealHandshake:
    """真握手档：假传输只能证明解析，证明不了 SDK 那趟会话真的能成。

    形态与 `tests/api/test_mcp_api.py` 一致——起真 uvicorn，不用 ASGITransport
    （SDK 客户端在同进程 ASGI 上握不了手，REPORT MB-5 记了这条）。
    """

    @asynccontextmanager
    async def _serve(self, **overrides: Any) -> AsyncIterator[str]:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = int(probe.getsockname()[1])
        settings = Settings(
            env="test",
            bus_backend="memory",
            store_backend="memory",
            simulator_enabled=False,
            delivery_mode="mock",
            llm_api_key="",
            agent_runtime="off",
            http_port=port,
            mcp_enabled=True,
            **overrides,
        )
        server = uvicorn.Server(
            uvicorn.Config(create_app(settings), host="127.0.0.1", port=port, log_level="warning", timeout_graceful_shutdown=3)
        )
        task = asyncio.create_task(server.serve())
        base = f"http://127.0.0.1:{port}"
        async with httpx.AsyncClient(base_url=base, timeout=20) as probe_client:
            for _ in range(200):
                try:
                    if (await probe_client.get("/healthz")).status_code == 200:
                        break
                except httpx.HTTPError:
                    await asyncio.sleep(0.05)
            else:  # pragma: no cover - 起不来时快速失败
                pytest.fail("uvicorn 未在超时内就绪")
        try:
            yield base
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, timeout=20)

    async def test_真工具面取回的正文能被解析成items(self) -> None:
        async with self._serve() as base:
            ctx = await McpAssessContext(url=f"{base}/mcp/").corroborate(region_code=REGION, hazard_hint="debris_flow", hits=[])
        # 内存装配下知识腿有内置预案：要么真取回条目，要么把原因写清楚，两头都不沾就是假绿
        assert ("cases" in ctx) or ctx["degraded"], f"既没命中也没原因：{ctx}"
        assert "retrieval" not in ctx, "这台没开检索 ⇒ 工具面里没有这个工具，应当如实记降级而不是空命中"
        assert any(line.startswith("retrieval:") for line in ctx["degraded"]), ctx["degraded"]

    async def test_配了令牌时真会话带着bearer才走得通(self) -> None:
        """这一条把 `_sdk_call` 的鉴权头逼到真服务器上验：头名错了的表现是"取数全变降级"。"""
        token = "mcp-face-token"
        async with self._serve(service_token=token) as base:
            ok = await McpAssessContext(url=f"{base}/mcp/", token=token).corroborate(region_code=REGION, hazard_hint="debris_flow", hits=[])
            denied = await McpAssessContext(url=f"{base}/mcp/").corroborate(region_code=REGION, hazard_hint="debris_flow", hits=[])
        assert not any("不可达" in line for line in ok["degraded"]), f"带了令牌还被拒：{ok['degraded']}"
        assert len(denied["degraded"]) == 2 and all("不可达" in line for line in denied["degraded"]), f"没带令牌却取到了东西：{denied}"
