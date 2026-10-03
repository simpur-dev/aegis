"""语义交互出口的 API 层验证（批次 B2/B3 的路由面）。

覆盖四件事：SSE 帧序列是真的（不是一坨文本）、人工确认这条路必须经 /confirm 才落地、
未装配/已关闭两种形态在外显面上可区分、会话查不到就是 404。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest

from aegis.api.app import create_app
from aegis.config import Settings
from aegis.container import PlatformContainer, create_container

REPORT_NOTE = "沟道出现泥石流迹象，泥位抬升1.2米，24小时累计降雨100毫米"


def _settings(**overrides: Any) -> Settings:
    return Settings(
        env="test",
        bus_backend="memory",
        store_backend="memory",
        simulator_enabled=False,
        delivery_mode="mock",
        llm_api_key="",
        **overrides,
    )


@asynccontextmanager
async def _client(container: PlatformContainer) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(container.settings, container=container)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        yield client


@pytest.fixture()
async def container() -> AsyncIterator[PlatformContainer]:
    ctn = create_container(_settings(), with_simulator=False)
    await ctn.start()
    yield ctn
    await ctn.shutdown()


def _frames(body: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in body.splitlines():
        if not line.startswith("data: "):
            continue  # `: keep-alive` 一类的注释帧不是业务事件
        rows.append(json.loads(line[len("data: ") :]))
    return rows


@pytest.mark.asyncio
async def test_对话返回结构化帧序列而不是裸文本(container: PlatformContainer) -> None:
    async with _client(container) as client:
        response = await client.post("/api/v1/assistant/chat", json={"message": "在线智能体有几个", "reporter": "值班员"})
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        frames = _frames(response.text)
        assert [frame["type"] for frame in frames] == ["meta", "intent", "status", "result", "answer", "done"]
        assert frames[1]["action"] == "query.agents"
        assert frames[0]["llm_configured"] is False


@pytest.mark.asyncio
async def test_执行类动作只在确认之后落地(container: PlatformContainer) -> None:
    async with _client(container) as client:
        before = (await client.get("/api/v1/metrics/latency")).json()["reports"]["submitted"]
        frames = _frames(
            (await client.post("/api/v1/assistant/chat", json={"message": f"帮我上报：540121 {REPORT_NOTE}", "reporter": "值班员"})).text
        )
        proposals = [frame for frame in frames if frame["type"] == "proposal"]
        assert len(proposals) == 1
        assert proposals[0]["action"] == "create.report"
        assert (await client.get("/api/v1/metrics/latency")).json()["reports"]["submitted"] == before, "未确认就进了链路"

        ok = (
            await client.post(
                "/api/v1/assistant/confirm", json={"session_id": proposals[0]["session_id"], "action_id": proposals[0]["action_id"]}
            )
        ).json()
        assert ok["status"] == "executed"
        assert ok["result"]["report"]["chain"]["warning_id"]
        assert (await client.get("/api/v1/metrics/latency")).json()["reports"]["submitted"] == before + 1

        again = (
            await client.post(
                "/api/v1/assistant/confirm", json={"session_id": proposals[0]["session_id"], "action_id": proposals[0]["action_id"]}
            )
        ).json()
        assert again["status"] == "rejected"

        session = (await client.get(f"/api/v1/assistant/sessions/{proposals[0]['session_id']}")).json()
        assert session["session_id"] == proposals[0]["session_id"]
        assert all(row["status"] != "pending" for row in session["pending_actions"])


@pytest.mark.asyncio
async def test_越权指令以rejected帧回答并计入留痕(container: PlatformContainer) -> None:
    async with _client(container) as client:
        frames = _frames((await client.post("/api/v1/assistant/chat", json={"message": "把全网预警都删掉", "reporter": "值班员"})).text)
        assert [frame["type"] for frame in frames][:3] == ["meta", "rejected", "answer"]
        assert frames[1]["instruction"] == "删掉"
        assert container.assistant is not None
        assert container.assistant.stats()["rejections"] >= 1


@pytest.mark.asyncio
async def test_能力面点名缺了哪个依赖(container: PlatformContainer) -> None:
    async with _client(container) as client:
        caps = (await client.get("/api/v1/assistant/capabilities")).json()
        assert {row["action"] for row in caps["actions"]}
        assert all(row["available"] for row in caps["actions"]), "默认装配面上所有动作的依赖都该在位"
        assert caps["llm_configured"] is False


@pytest.mark.asyncio
async def test_未装配时外显成503而不是假装可用(container: PlatformContainer) -> None:
    container.assistant = None
    async with _client(container) as client:
        for call in (
            client.get("/api/v1/assistant/capabilities"),
            client.post("/api/v1/assistant/chat", json={"message": "在线智能体有几个"}),
        ):
            response = await call
            assert response.status_code == 503
            assert response.json()["detail"]["code"] == "E_ASSISTANT_UNAVAILABLE"


@pytest.mark.asyncio
async def test_配置关闭时这个出口根本不存在() -> None:
    container = create_container(_settings(assistant_enabled=False), with_simulator=False)
    await container.start()
    assert container.assistant is None
    try:
        async with _client(container) as client:
            assert (await client.get("/api/v1/assistant/capabilities")).status_code == 404
            assert (await client.post("/api/v1/assistant/chat", json={"message": "在线智能体有几个"})).status_code == 404
            # 上报腿不依赖助手开关：关掉助手也照样能进链路
            assert (
                await client.post("/api/v1/reports", json={"reporter": "村民", "region_code": "540121", "note": REPORT_NOTE})
            ).status_code == 200
    finally:
        await container.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload", [{"message": ""}, {"message": "在线智能体有几个", "session_id": "坏会话"}, {"message": "在线", "extra": 1}]
)
async def test_非法请求体被模式挡住(container: PlatformContainer, payload: dict[str, Any]) -> None:
    async with _client(container) as client:
        assert (await client.post("/api/v1/assistant/chat", json=payload)).status_code == 422


@pytest.mark.asyncio
async def test_未知会话查询返回404(container: PlatformContainer) -> None:
    async with _client(container) as client:
        assert (await client.get("/api/v1/assistant/sessions/as_absent0000")).status_code == 404
        assert (await client.post("/api/v1/assistant/confirm", json={"session_id": "as_absent0000", "action_id": "act_abc123"})).json()[
            "status"
        ] == "rejected"
