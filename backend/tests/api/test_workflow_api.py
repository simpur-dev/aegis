"""工作流端点的 HTTP 层验证。

这个文件此前不存在——工作流那十来条路由只有压测脚本引用过，没有任何 API 级用例。
后果是"画布能存不能开"这类缺口一路活到真机巡检：`GET /definitions` 只报计数，
而根本没有"取一份完整定义"的出口，前端想打开也无从下手。

这里要钉住的是三件事：
1. 列表只给计数（够挑一条），详情给全量（够改一条）——两者不能互相顶替；
2. **取出来 → 原样存回去**必须逐字段等价：画布的"保存"走的就是这条路径，
   任何字段在映射中丢失，症状都是"我明明没改，存完却不一样了"；
3. 不存在的 workflow_id 要 404，而不是返回一个空定义让前端画出空白画布。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest

from aegis.api.app import create_app
from aegis.config import Settings
from aegis.container import PlatformContainer, create_container


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
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


@pytest.fixture()
async def container() -> AsyncIterator[PlatformContainer]:
    ctn = create_container(_settings(), with_simulator=False)
    await ctn.start()
    yield ctn
    await ctn.shutdown()


@pytest.mark.asyncio
async def test_列表只报计数不报内容(container: PlatformContainer) -> None:
    async with _client(container) as client:
        body = (await client.get("/api/v1/workflow/definitions")).json()
        assert body["count"] >= 5, f"内置模板应有 5 灾种 + 核签流程，实得 {body['count']}"
        row = body["items"][0]
        assert {"workflow_id", "name", "version", "node_count", "edge_count", "status"} <= set(row)
        assert "nodes" not in row, "列表里塞全量节点会让每次刷新都拖着一整张图"


@pytest.mark.asyncio
async def test_详情给出可编辑的完整定义(container: PlatformContainer) -> None:
    async with _client(container) as client:
        listed = (await client.get("/api/v1/workflow/definitions")).json()["items"]
        target = next(row for row in listed if row["node_count"] > 1)
        detail = await client.get(f"/api/v1/workflow/definitions/{target['workflow_id']}")
        assert detail.status_code == 200, detail.text
        definition = detail.json()
        assert definition["workflow_id"] == target["workflow_id"]
        assert len(definition["nodes"]) == target["node_count"], "计数与内容对不上，画布会少画节点"
        assert len(definition["edges"]) == target["edge_count"]
        node = definition["nodes"][0]
        assert {"node_id", "type", "config", "sla_ms", "timeout_ms", "on_failure", "retry"} <= set(node)


@pytest.mark.asyncio
async def test_取出来原样存回去不丢字段(container: PlatformContainer) -> None:
    """画布"打开→保存"这条路径的等价性保证。

    前端把定义映射成图、再映射回定义；任何一映射漏字段，症状都是"我没改什么，
    存完版本却变了内容"。这里用后端自己的读写口做往返，把字段逐个比一遍。
    """
    async with _client(container) as client:
        listed = (await client.get("/api/v1/workflow/definitions")).json()["items"]
        target = next(row for row in listed if row["node_count"] > 1)
        original = (await client.get(f"/api/v1/workflow/definitions/{target['workflow_id']}")).json()

        created = await client.post(
            "/api/v1/workflow/definitions",
            json={
                "name": f"往返副本-{original['workflow_id'][-6:]}",
                "description": original["description"],
                "nodes": original["nodes"],
                "edges": original["edges"],
            },
        )
        assert created.status_code == 201, created.text  # 创建返回 201，不是 200
        new_id = created.json()["workflow_id"]
        round_tripped = (await client.get(f"/api/v1/workflow/definitions/{new_id}")).json()

        assert len(round_tripped["nodes"]) == len(original["nodes"])
        for before, after in zip(original["nodes"], round_tripped["nodes"], strict=True):
            assert after == before, f"节点 {before['node_id']} 往返后变了：{after}"
        assert round_tripped["edges"] == original["edges"], "连线（含条件）往返后变了"


@pytest.mark.asyncio
async def test_不存在的定义给404而不是空白定义(container: PlatformContainer) -> None:
    async with _client(container) as client:
        response = await client.get("/api/v1/workflow/definitions/wf_000000000000")
        assert response.status_code == 404
        # 空 body 会让前端画出"一张空白画布"，看起来像定义本身是空的
        assert "nodes" not in response.json()


@pytest.mark.asyncio
async def test_节点类型出口与后端注册表同源(container: PlatformContainer) -> None:
    async with _client(container) as client:
        body = (await client.get("/api/v1/workflow/node-types")).json()
        names = {str(row["type"]) for row in body["items"]}
        assert {"hazard_identify", "risk_assess", "situation_simulate", "human_review", "degrade_to_rule"} <= names
        assert body["count"] == len(names) == 16, f"节点类型数变了（{len(names)}），前端镜像与文档都要跟着改"
