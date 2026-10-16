"""MCP 工具面的 HTTP 侧用例（§4.2 / D4，方案 A：server + 鉴权 + 状态行）。

这里守的五件事都不是"SDK 能不能跑"：
① 配置关 ⇒ 这条路径不存在（404）；开着但能力没装配 ⇒ 503 说清缺什么（与几何查询同一口径）；
② 鉴权与数据 API 同面、同头名（Bearer 与 X-Aegis-Token 都认——这是 #15 那次对盘的直接产物）；
③ `/api/v1/integrations` 的 `mcp` 行能分辨"没开 / 没装 / 开了但某条腿没接上"；
④ 默认面里没有写工具：外部客户端能读不能写是这一层的闸，不是文档里的约定；
⑤ 清单外的工具名在装配期就失败，不留"配了没生效"的空旋钮。

握手与工具调用走真协议。这里有一条实测换来的分工：
① 只看状态码的闸（401/503/404/307）用 httpx2 的 ASGITransport 直调应用，不起端口、建得快；
② 真握手【必须】起真 uvicorn——SDK 客户端在 ASGITransport 上跑不通，
   服务端与客户端被塞进同一个任务，退出时 anyio 抛
   "Attempted to exit a cancel scope that isn't the current task's current cancel scope"。
   同一份装配代码换到真服务器上握手、调用、关闭全部正常（`.tmp-verify/probe_mcp_server.py` 取证），
   所以那是测试传输的形态问题，不是挂载问题；但用它做握手断言就等于断言一个不存在的生产形态。
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

#: 整个文件都要真 SDK：没装可选腿时如实跳过（不是失败，也不是"看着绿"）。
#: `importorskip` 必须排在 mcp/httpx2 的导入之前——它一抛 Skip 就不再往下导，
#: 否则 `--extra dev` 一台干净机器会在收集期直接 ImportError。
pytest.importorskip("mcp", reason="未安装可选依赖 mcp（`uv sync --extra dev --extra mcp`）：MCP 工具面用例无法运行")

import httpx2
import uvicorn
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

from aegis.api.app import create_app, lifespan
from aegis.config import Settings
from aegis.container import create_container
from aegis.mcp_tools.manifest import exposed_names

MCP_PATH = "/mcp/"


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "env": "test",
        "bus_backend": "memory",
        "store_backend": "memory",
        "simulator_enabled": False,
        "delivery_mode": "mock",
        "llm_api_key": "",
        "agent_runtime": "off",
    }
    base.update(overrides)
    return Settings(**base)


@asynccontextmanager
async def _running(app: Any) -> AsyncIterator[None]:
    """走产品自己的 lifespan（容器在其中被启动，MCP 会话管理器也随之跑起来）。"""
    async with lifespan(app):
        yield


@asynccontextmanager
async def _live_server(settings: Settings) -> AsyncIterator[str]:
    """真 uvicorn + 真端口，返回 base_url。会话管理器随进程 lifespan 起来，与部署同形。"""
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(settings),
            host="127.0.0.1",
            port=settings.http_port,
            log_level="warning",
            timeout_graceful_shutdown=3,
        )
    )
    task = asyncio.create_task(server.serve())
    base_url = f"http://127.0.0.1:{settings.http_port}"
    async with httpx.AsyncClient(base_url=base_url, timeout=20.0) as probe:
        for _ in range(200):
            try:
                if (await probe.get("/healthz")).status_code == 200:
                    break
            except httpx.HTTPError:
                await asyncio.sleep(0.05)
        else:  # pragma: no cover - 服务器起不来时快速失败，别把用例挂死
            pytest.fail("uvicorn 未在超时内就绪")
    try:
        yield base_url
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=20.0)


@asynccontextmanager
async def _mcp_session(base_url: str, *, headers: dict[str, str] | None = None) -> AsyncIterator[ClientSession]:
    client = httpx2.AsyncClient(base_url=base_url, headers=headers or {}, timeout=20.0)
    streams = streamable_http_client(f"{base_url}{MCP_PATH}", http_client=client)
    read, write = await streams.__aenter__()
    session = ClientSession(read, write)
    await session.__aenter__()
    await session.initialize()
    try:
        yield session
    finally:
        await session.__aexit__(None, None, None)
        await streams.__aexit__(None, None, None)
        await client.aclose()


async def _mcp_post(app: Any, *, headers: dict[str, str], path: str = MCP_PATH) -> int:
    """直接 POST 一个 initialize，只看 HTTP 状态——用来判 401/503 这两条闸，不必建整条会话。"""
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://platform", headers=headers) as client:
        response = await client.post(
            path,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "aegis-test", "version": "0"},
                },
            },
            headers={"accept": "application/json, text/event-stream"},
        )
        return response.status_code


class TestMountArms:
    async def test_默认配置下这条路径不存在(self) -> None:
        app = create_app(_settings())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://platform") as client:
            bare = await client.post("/mcp", json={})
            slashed = await client.post("/mcp/", json={})
        assert bare.status_code == 404 and slashed.status_code == 404, (
            "没开的腿不该留一条能握手的路径：留了就会有人以为平台对外已经有工具面"
        )

    async def test_清单外的工具名在装配期就失败(self) -> None:
        with pytest.raises(ValueError, match="清单外"):
            create_container(_settings(mcp_enabled=True, mcp_tools="os.exec"))

    async def test_挂载点配成根路径在启动期就失败(self) -> None:
        """`/` 会让工具面接住整张 API，表现是"所有端点突然要令牌"——这种配错不该活到运行期。"""
        for bad in ("/", "", "mcp"):
            with pytest.raises(ValueError, match="AEGIS_MCP_MOUNT_PATH"):
                create_app(_settings(mcp_enabled=True, mcp_mount_path=bad))

    async def test_开着但可选腿没装时回503并说清缺什么(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import aegis.mcp_tools as facade

        real = facade.mcp_available
        monkeypatch.setattr(facade, "mcp_available", lambda: False)
        try:
            container = create_container(_settings(mcp_enabled=True))
        finally:
            monkeypatch.setattr(facade, "mcp_available", real)
        row = next(item.as_dict() for item in container.integration_status() if item.name == "mcp")
        assert row["enabled"] is False and row["driver"] == "absent"
        assert "extra mcp" in str(row["detail"]["reason"])
        app = create_app(container.settings, container=container)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://platform") as client:
            response = await client.post("/mcp/", json={})
        assert response.status_code == 503
        assert "REST 兼容面" in response.text, "503 要说清没装什么，以及这期间取数走哪条路"
        await container.shutdown()

    async def test_不带尾斜杠的路径按框架规矩重定向(self) -> None:
        """挂载点用 `/mcp`，握手 URL 要写 `/mcp/`：Starlette 的 Mount 对裸路径发 307。

        钉住这条是因为客户端配错一个斜杠的表现是"连上了但没响应"，而 307 保留方法与时序、
        是否跟随由客户端决定。README 因此把带斜杠的 URL 写成唯一正确形态。
        """
        app = create_app(_settings(mcp_enabled=True))
        async with _running(app):
            transport = httpx2.ASGITransport(app=app)
            async with httpx2.AsyncClient(transport=transport, base_url="http://platform", follow_redirects=False) as client:
                response = await client.post("/mcp", json={})
        assert response.status_code == 307
        assert response.headers["location"].endswith("/mcp/")


class TestStatusRow:
    async def test_开启后状态行带出挂载点与暴露面(self) -> None:
        app = create_app(_settings(mcp_enabled=True))
        async with _running(app):
            container = app.state.container
            row = next(item.as_dict() for item in container.integration_status() if item.name == "mcp")
        assert row["enabled"] is True and row["driver"] == "streamable-http"
        detail = row["detail"]
        assert detail["mount_path"] == "/mcp"
        assert detail["allow_write_tools"] is False
        exposed, absent = detail["exposed_tools"], detail["absent_tools"]
        # 默认面 = 清单里 default_exposed 且只读的那几项；每一项要么注册、要么记缺席，不许默默消失。
        assert set(exposed) | set(absent) == set(exposed_names())
        assert "telemetry.query" in exposed and "cases.search" in exposed and "memory.search" in exposed
        assert "memory.write" not in exposed and "simulation.run" not in exposed, "写工具与非默认工具不该出现在默认面上"

    async def test_没装配的腿对应的工具记为缺席而不是注册成空工具(self) -> None:
        """检索默认关（`retrieval_enabled=false`）⇒ `retrieval.search` 进缺席名单。

        这一条钉的是"能列出来却调不动"那类谎：客户端列到一个永远回空表的工具，
        比列不到它更难查——它会以为"平台里没有相关资料"。
        """
        app = create_app(_settings(mcp_enabled=True))
        async with _running(app):
            row = next(item.as_dict() for item in app.state.container.integration_status() if item.name == "mcp")
        assert "retrieval.search" in row["detail"]["absent_tools"]
        assert "retrieval.search" not in row["detail"]["exposed_tools"]

    async def test_装上检索后同一个工具换成暴露(self) -> None:
        app = create_app(_settings(mcp_enabled=True, retrieval_enabled=True))
        async with _running(app):
            row = next(item.as_dict() for item in app.state.container.integration_status() if item.name == "mcp")
        assert "retrieval.search" in row["detail"]["exposed_tools"], "缺席名单只该反映真实装配，能力到位后必须自动转为暴露"


class TestHandshake:
    async def test_真握手能列工具并调到只读工具(self) -> None:
        async with _live_server(_settings(mcp_enabled=True, http_port=_free_port())) as base_url, _mcp_session(base_url) as session:
            names = {tool.name for tool in (await session.list_tools()).tools}
            assert {"telemetry.query", "cases.search", "memory.search", "memory.categories"} <= names
            assert "memory.write" not in names and "simulation.run" not in names
            result = await session.call_tool("telemetry.query", {"region_code": "540121", "limit": 5})
            assert result.is_error is False
            assert '"ok": true' in result.content[0].text

    async def test_四类记忆门面经工具面读得到三种现场(self) -> None:
        """§4.3：`connected=false`（这条腿没接）与"接了但没命中"必须是两种读数，工具面不许把它们折成一个空表。

        这一条走的是真握手 + 真装配点，所以它还钉住另一件事：容器没给 provider 时，
        外部客户端读到的是"哪一类没接"，而不是一个看不出原因的 `items: []`。
        """
        settings = _settings(mcp_enabled=True, mcp_tools="memory.categories", http_port=_free_port())
        async with _live_server(settings) as base_url, _mcp_session(base_url) as session:
            result = await session.call_tool("memory.categories", {"query": "泥石流", "region_code": "540121", "limit": 2})
        assert result.is_error is False, result.content
        body = json.loads(result.content[0].text)
        categories = body["categories"]
        assert set(categories) >= {"episodic", "semantic", "knowledge"}, categories
        for name, entry in categories.items():
            assert {"connected", "items", "note"} <= set(entry), f"{name} 的三态被折掉了：{entry}"
        assert body["disconnected"] == sorted(n for n, e in categories.items() if not e["connected"]), body

    async def test_调用留痕既是状态行也是时延账本(self) -> None:
        """§4.2 那句"工具即审计面（调用留 trace）"的端到端面：一次真调用要在三处留下可读的账。

        状态行给工具维度（哪个工具、几次、多慢），账本给聚合线，`/metrics` 给外部抓取方。
        只验进程内那一份不够——"留了 trace"最后要能被别人独立复核；
        而只验 `/metrics` 也不够：那条线没有工具维度，答不出"是哪个工具慢"。
        """
        port = _free_port()
        async with (
            _live_server(_settings(mcp_enabled=True, http_port=port)) as base_url,
            _mcp_session(base_url) as session,
            httpx.AsyncClient(base_url=base_url, timeout=20.0) as probe,
        ):
            for _ in range(3):
                assert (await session.call_tool("telemetry.query", {"region_code": "540121", "limit": 5})).is_error is False
            integrations = (await probe.get("/api/v1/integrations")).json()
            metrics = (await probe.get("/metrics")).text
        row = next(item for item in integrations["items"] if item["name"] == "mcp")
        latency = row["detail"]["latency_ms"]
        assert latency["telemetry.query"]["count"] == 3, latency
        assert row["detail"]["calls"] == {"telemetry.query": 3}
        line = next((entry for entry in metrics.splitlines() if 'aegis_latency_ms_count{metric="mcp_tool_ms"}' in entry), "")
        assert line.endswith(" 3.0"), f"三次调用没进对外指标出口：{line!r}"

    async def test_只给灾种时案例召回按中文名问与REST同句回退(self) -> None:
        """`GET /api/v1/cases/recall` 在 `q` 空时用灾种中文名去问 provider，工具面必须问同一次。

        少这句的话，`cases.search(hazard_type=...)` 会拿空串去召回：外部客户端明明给了灾种，
        读到的却是"没有相似案例"——同一个 provider 因为门面不同给出两个答案，正是最难查的那类分叉。
        """
        settings = _settings(mcp_enabled=True, mcp_tools="cases.search", http_port=_free_port())
        async with _live_server(settings) as base_url, _mcp_session(base_url) as session:
            by_hazard = await session.call_tool("cases.search", {"hazard_type": "debris_flow", "limit": 3})
        assert by_hazard.is_error is False
        body = json.loads(by_hazard.content[0].text)
        assert body["count"] > 0, f"只给灾种也该召回内置预案：{body}"
        assert any("泥石" in json.dumps(item, ensure_ascii=False) for item in body["items"]), body["items"]

    async def test_点名的非默认工具才出现在工具面上(self) -> None:
        """`mcp_tools` 是清单的子集选择器：点了 simulation.run 才有它，同时不再白送默认面。"""
        settings = _settings(mcp_enabled=True, mcp_tools="simulation.run,memory.search,memory.write", http_port=_free_port())
        async with _live_server(settings) as base_url, _mcp_session(base_url) as session:
            names = {tool.name for tool in (await session.list_tools()).tools}
        assert "simulation.run" in names and "memory.search" in names
        assert "memory.write" not in names, "写工具还要第二把钥匙（allow_write_tools）"
        assert "telemetry.query" not in names, "既然点了名，就不该再白送默认面"

    async def test_工具面的推演单次真跑到沙盒并交出角色档(self) -> None:
        """§4.4：同一份沙盒事实要从两个门面（REST `/api/v1/simulation/run` 与工具 `simulation.run`）交出来。

        这一条顺带钉装配签名——`_mcp_simulation_source(settings)` 少拿一个参数只在调用期炸，
        外部客户端看到的是一个 `isError` 而不说为什么；`roles` 那一格则是"多角色真被问过"的凭据。
        """
        settings = _settings(mcp_enabled=True, mcp_tools="simulation.run", http_port=_free_port())
        async with _live_server(settings) as base_url, _mcp_session(base_url) as session:
            result = await session.call_tool("simulation.run", {"scenarios": 3, "rounds": 2, "seed": 5})

        assert result.is_error is False
        body = json.loads(result.content[0].text)
        assert body["mode"] == "demo_sandbox"
        assert body["roles"]["policy"] == "rule" and body["roles"]["asked"] == 8 and body["roles"]["calls"] == 8
        assert body["probability_matrix"] and body["timeline"]
        assert all(set(row["actions"]) == {"commander", "rescue", "residents", "monitor"} for row in body["timeline"])
        assert {row["playbook"] for row in body["playbook_comparison"]} == {"转移", "封路", "不行动"}

    async def test_腿断了过线报工具错误并把成因带进状态行(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """外部客户端拿到的必须是协议层的 `isError`，而平台侧要留下同一件事的账。

        这一条同时钉两件事，因为它们各自都错过：
        ① 失败不许伪装成成功正文（客户端会照 ok=false 之外的字段解析，然后以为"平台里没资料"）；
        ② 状态行的调用/失败/成因必须是**运行期真值**——装配时刻的快照会把"刚刚失败过"永远读成 0 笔。
        """
        import aegis.integrations.mcp as igs  # 工具面装配与它的私有 source 现在住在这个模块

        def _failing(_store: object):
            async def _call(args: dict[str, Any]) -> dict[str, Any]:
                raise RuntimeError("遥测后端不可达")

            return _call

        monkeypatch.setattr(igs, "_mcp_telemetry_source", _failing)
        settings = _settings(mcp_enabled=True, http_port=_free_port())
        async with _live_server(settings) as base_url:
            async with _mcp_session(base_url) as session:
                result = await session.call_tool("telemetry.query", {"region_code": "540121", "limit": 5})
            assert result.is_error is True, "腿断了必须让协议层说是错，不能让客户端自己从正文里猜"
            assert "遥测后端不可达" in result.content[0].text
            async with httpx.AsyncClient(base_url=base_url, timeout=10.0) as client:
                row = next(item for item in (await client.get("/api/v1/integrations")).json()["items"] if item["name"] == "mcp")
        detail = row["detail"]
        assert detail["calls"]["telemetry.query"] == 1
        assert detail["failures"]["telemetry.query"] == 1
        assert "遥测后端不可达" in detail["last_error"]


class TestAuthIsOneFace:
    async def test_配了令牌时工具面与数据api都认两种头(self) -> None:
        token = "svc-token-for-mcp"
        app = create_app(_settings(mcp_enabled=True, service_token=token))
        async with _running(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://platform") as client:
                assert (await client.get("/api/v1/telemetry")).status_code == 401
                assert (await client.get("/api/v1/telemetry", headers={"authorization": f"Bearer {token}"})).status_code == 200
                assert (await client.get("/api/v1/telemetry", headers={"x-aegis-token": token})).status_code == 200
                assert (await client.get("/api/v1/telemetry", headers={"x-aegis-token": "nope"})).status_code == 401
            assert await _mcp_post(app, headers={}) == 401, "工具面漏校验＝两个面两套口径，正是 #15 的原始形态"
            assert await _mcp_post(app, headers={"authorization": f"Bearer {token}"}) != 401
            assert await _mcp_post(app, headers={"x-aegis-token": token}) != 401
            assert await _mcp_post(app, headers={"x-aegis-token": "nope"}) == 401
