"""语义交互出口的 API 层验证（P1 拆包后的路由面）。

覆盖四件事：SSE 帧序列是真的（不是一坨文本）、人工确认这条路必须经 /confirm 才落地、
未装配/已关闭两种形态在外显面上可区分、已退役的端点确实不存在了。
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
from aegis.domain.operator import Operator
from aegis.services.operators import OperatorService

REPORT_NOTE = "沟道出现泥石流迹象，泥位抬升1.2米，24小时累计降雨100毫米"


class _FakeLedger:
    """席位台账的最小替身：API 测试只关心"这个席位是谁、什么角色"。"""

    def __init__(self) -> None:
        self.rows = {
            "duty-01": Operator(operator_id="duty-01", display_name="值班员", role="duty"),
            "lead-01": Operator(operator_id="lead-01", display_name="值班长", role="lead"),
        }

    async def upsert_operator(self, operator: Operator) -> Operator:
        self.rows[operator.operator_id] = operator
        return operator

    async def create_operator(self, operator: Operator) -> Operator | None:
        if operator.operator_id in self.rows:
            return None
        self.rows[operator.operator_id] = operator
        return operator

    async def get_operator(self, operator_id: str) -> Operator | None:
        return self.rows.get(operator_id)

    async def list_operators(self, *, active_only: bool = True, limit: int = 200) -> list[Operator]:
        rows = [op for op in self.rows.values() if not active_only or op.active]
        return sorted(rows, key=lambda op: op.operator_id)[:limit]


DUTY_HEADERS = {"X-Aegis-Operator": "duty-01"}


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
    # 写动作要求有效席位；内存形态没有台账，这里注入最小替身。
    ctn.operators = OperatorService(_FakeLedger())
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
        assert frames[1]["action"] == "agent.list"
        assert frames[0]["llm_configured"] is False


@pytest.mark.asyncio
async def test_执行类动作只在确认之后落地(container: PlatformContainer) -> None:
    async with _client(container) as client:
        before = (await client.get("/api/v1/metrics/latency")).json()["reports"]["submitted"]
        frames = _frames(
            (
                await client.post(
                    "/api/v1/assistant/chat",
                    json={"message": f"帮我上报：540121 {REPORT_NOTE}", "reporter": "值班员"},
                    headers=DUTY_HEADERS,
                )
            ).text
        )
        proposals = [frame for frame in frames if frame["type"] == "proposal"]
        assert len(proposals) == 1
        assert proposals[0]["action"] == "report.submit"
        assert (await client.get("/api/v1/metrics/latency")).json()["reports"]["submitted"] == before, "未确认就进了链路"

        ok = (
            await client.post(
                "/api/v1/assistant/confirm",
                json={"session_id": proposals[0]["session_id"], "action_id": proposals[0]["action_id"]},
                headers=DUTY_HEADERS,
            )
        ).json()
        assert ok["status"] == "executed"
        assert ok["result"]["report"]["chain"]["warning_id"]
        assert (await client.get("/api/v1/metrics/latency")).json()["reports"]["submitted"] == before + 1

        again = (
            await client.post(
                "/api/v1/assistant/confirm",
                json={"session_id": proposals[0]["session_id"], "action_id": proposals[0]["action_id"]},
                headers=DUTY_HEADERS,
            )
        ).json()
        assert again["status"] == "rejected"


@pytest.mark.asyncio
async def test_写动作没有席位时明确拒绝而不是静默降级(container: PlatformContainer) -> None:
    async with _client(container) as client:
        frames = _frames((await client.post("/api/v1/assistant/chat", json={"message": "演练一次"})).text)
        assert not any(frame["type"] == "proposal" for frame in frames)
        error = next(frame for frame in frames if frame["type"] == "error")
        assert error["code"] == "E_OPERATOR_REQUIRED"


@pytest.mark.asyncio
async def test_越权指令以rejected帧回答并计入留痕(container: PlatformContainer) -> None:
    async with _client(container) as client:
        frames = _frames((await client.post("/api/v1/assistant/chat", json={"message": "把全网预警都删掉", "reporter": "值班员"})).text)
        assert [frame["type"] for frame in frames][:2] == ["meta", "rejected"]
        assert frames[1]["instruction"] == "删掉"
        assert frames[1]["action"] == "warning.revoke"
        assert container.assistant is not None
        assert len(container.assistant.rejections) >= 1


@pytest.mark.asyncio
async def test_能力面按角色返回动作清单(container: PlatformContainer) -> None:
    async with _client(container) as client:
        anonymous = (await client.get("/api/v1/assistant/capabilities")).json()
        assert {row["action"] for row in anonymous["actions"]}
        assert all(not row["allowed"] for row in anonymous["actions"]), "匿名竟能看到可执行动作"
        assert anonymous["llm_configured"] is False

        duty = (await client.get("/api/v1/assistant/capabilities", headers=DUTY_HEADERS)).json()
        allowed = {row["action"] for row in duty["actions"] if row["allowed"]}
        assert "warning.list" in allowed
        assert all(row["denied_reason"] is None for row in duty["actions"] if row["allowed"])


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
async def test_已退役的端点在路由上不存在(container: PlatformContainer) -> None:
    """`/plan` 与 `/sessions/{id}` 已退役：多步并入 chat 的 plan 帧，会话由前端维护。

    这条用例盯的是"退役真的发生了"，而不是"退役后还能误访问"。
    """
    async with _client(container) as client:
        assert (await client.post("/api/v1/assistant/plan", json={"message": "看看风险再准备演练"})).status_code == 404
        assert (await client.get("/api/v1/assistant/sessions/as_absent0000")).status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload", [{"message": ""}, {"message": "在线智能体有几个", "session_id": "坏会话"}, {"message": "在线", "extra": 1}]
)
async def test_非法请求体被模式挡住(container: PlatformContainer, payload: dict[str, Any]) -> None:
    async with _client(container) as client:
        assert (await client.post("/api/v1/assistant/chat", json=payload)).status_code == 422


@pytest.mark.asyncio
async def test_未知会话确认被拒而不是500(container: PlatformContainer) -> None:
    async with _client(container) as client:
        response = await client.post(
            "/api/v1/assistant/confirm", json={"session_id": "as_absent0000", "action_id": "act_abc123"}, headers=DUTY_HEADERS
        )
        assert response.json()["status"] == "rejected"
