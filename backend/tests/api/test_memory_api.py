"""共享记忆的管理端点（§4.3「记忆可治理」）。

这块的失败模式不是"接口 500"，而是**空结果被读成「没有相关记忆」**：
`driver=memory` 的空是「这个进程还没人写过」，`driver=postgres` + `order=vector` 的空才是
「按语义找过、确实没有相近的」。所以端点必须把后端档位与排序方式一起回出来——
这里守的就是条目、后端、序这三件事。

另外钉住删除的 404：没删到却回 200 + `removed: false`，运维脚本的 `if resp.ok`
就会把「这条 id 早就不存在」读成「清理完成了」。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import cast

import httpx
import pytest

from aegis.api.app import create_app
from aegis.config import Settings
from aegis.container import PlatformContainer, create_container

BASE = "http://testserver"
LESSON = "540121 蓝色预警后 2 小时泥位回落，重复升级同一条读数会造成误报"


def _settings() -> Settings:
    return Settings(
        env="test",
        bus_backend="memory",
        store_backend="memory",
        simulator_enabled=False,
        delivery_mode="mock",
        llm_api_key="",
    )


@pytest.fixture
async def memory_pair() -> AsyncIterator[tuple[httpx.AsyncClient, PlatformContainer]]:
    """同时给出接口出口与容器：有几条事实（后端档位、重写语义）只在容器上看得到，
    只测响应会放过"响应对、容器里根本没有这一档"的实现。"""
    container = create_container(_settings(), with_simulator=False)
    await container.start()
    app = create_app(container.settings, container=container)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as client:
        yield client, container
    await container.shutdown()


async def test_空库读出来要说清是哪一档后端(memory_pair: tuple[httpx.AsyncClient, PlatformContainer]) -> None:
    client, _ = memory_pair
    body = (await client.get("/api/v1/memory/shared")).json()
    assert body["items"] == [] and body["count"] == 0
    assert body["driver"] == "memory", "后端档位读不出来，空结果就分不清「没人写过」与「写过但这一档重启归零了」"
    assert body["order"] == "valid_at_desc"


async def test_写一条再读回来是同一条内容与时刻(memory_pair: tuple[httpx.AsyncClient, PlatformContainer]) -> None:
    client, _ = memory_pair
    created = await client.post("/api/v1/memory/shared", json={"kind": "lesson", "content": LESSON, "agent_id": "duty_review"})
    assert created.status_code == 201, created.text
    assert created.json()["embedded"] is False, "没配嵌入腿时不能把这条写成「已向量化」"
    record_id = created.json()["record_id"]

    listed = (await client.get("/api/v1/memory/shared", params={"kind": "lesson"})).json()
    assert listed["count"] == 1
    item = listed["items"][0]
    assert item["record_id"] == record_id and item["content"] == LESSON and item["agent_id"] == "duty_review"
    assert item["valid_at"].endswith("Z") or "+00:00" in item["valid_at"], "缺时区的时刻会让后面的窗口检索整体平移"


async def test_请求了语义检索但没有嵌入腿时如实说退到时序(memory_pair: tuple[httpx.AsyncClient, PlatformContainer]) -> None:
    client, _ = memory_pair
    await client.post("/api/v1/memory/shared", json={"kind": "assessment", "content": LESSON})
    body = (await client.get("/api/v1/memory/shared", params={"q": "泥位回落"})).json()
    assert body["order"] == "valid_at_desc_no_embedding", "给了查询词就回 vector，等于把「按时间捞了几条」说成「最相关的几条」"


@pytest.mark.asyncio
async def test_有嵌入腿时同一个查询词才走语义序(memory_pair: tuple[httpx.AsyncClient, PlatformContainer]) -> None:
    """反例：门面接了嵌入腿之后，`order` 必须换档——否则"退到时序"那一句会被永远报出去。"""
    client, container = memory_pair
    assert container.memory is not None
    container.memory.set_embedder(_StubEmbedder())  # 走装配点用的同一个入口，不去拨私有字段
    await client.post("/api/v1/memory/shared", json={"kind": "lesson", "content": LESSON})
    body = (await client.get("/api/v1/memory/shared", params={"q": "泥位回落"})).json()
    assert body["order"] == "vector" and body["count"] == 1


class _StubEmbedder:
    """确定性 1024 维嵌入替身：只关心"有没有向量可用"，不关心语义质量。"""

    model_id = "stub-1024"

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[1.0] + [0.0] * 1_023 for _ in texts]


async def test_删掉之后列表里就没有了而再删一次是404(memory_pair: tuple[httpx.AsyncClient, PlatformContainer]) -> None:
    client, _ = memory_pair
    record_id = (await client.post("/api/v1/memory/shared", json={"kind": "lesson", "content": LESSON})).json()["record_id"]
    removed = await client.delete(f"/api/v1/memory/shared/{record_id}")
    assert removed.status_code == 200 and removed.json()["removed"] is True
    assert (await client.get("/api/v1/memory/shared")).json()["count"] == 0
    again = await client.delete(f"/api/v1/memory/shared/{record_id}")
    assert again.status_code == 404, "没删到回 200 会让脚本把「id 早就不存在」读成「清理完成」"


async def test_坏形状在门口就挡住(memory_pair: tuple[httpx.AsyncClient, PlatformContainer]) -> None:
    client, _ = memory_pair
    assert (await client.post("/api/v1/memory/shared", json={"kind": "guess", "content": LESSON})).status_code == 422
    assert (await client.post("/api/v1/memory/shared", json={"kind": "lesson", "content": "短"})).status_code == 422
    assert (await client.post("/api/v1/memory/shared", json={"kind": "lesson", "content": LESSON, "author": "李四"})).status_code == 422, (
        "多余键必须拒：否则「这条是谁写的」会在拼错键时静默丢掉"
    )
    assert (await client.get("/api/v1/memory/shared", params={"limit": 500})).status_code == 422


async def test_同一id重写就是修正而不攒出两条(memory_pair: tuple[httpx.AsyncClient, PlatformContainer]) -> None:
    """端点每次生成新 id，治理侧（复盘改一条经验）走的是同一个门面：这里直接从门面重写的口对账。"""
    client, container = memory_pair
    memory = cast(PlatformContainer, container).memory
    assert memory is not None
    first = await memory.write_shared(
        kind="lesson", agent_id="duty_review", content=LESSON, record_id="mem_fixed_id", valid_at="2026-10-11T01:02:03.000Z"
    )
    await memory.write_shared(
        kind="lesson", agent_id="duty_review", content="改过措辞的同一条经验", record_id=first, valid_at="2026-10-11T01:02:03.000Z"
    )
    items = (await client.get("/api/v1/memory/shared", params={"kind": "lesson"})).json()["items"]
    assert [item["record_id"] for item in items].count(first) == 1, "同一 id 攒两条，就等于有两句互相矛盾的「最后一句」"
    assert items[0]["content"] == "改过措辞的同一条经验"


async def test_状态面里有记忆这一行(memory_pair: tuple[httpx.AsyncClient, PlatformContainer]) -> None:
    """`/api/v1/integrations` 是"这条腿到底接没接"的唯一对外说法：漏一行就等于让读的人去猜。"""
    _, container = memory_pair
    rows = {row.name: row for row in container.integration_status()}
    assert "memory" in rows, "记忆门面不在状态面上，部署方就无法知道它是内存档还是库档"
    assert rows["memory"].driver == "memory"
